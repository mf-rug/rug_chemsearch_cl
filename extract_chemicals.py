#!/usr/bin/env python3
"""
Chemical Data Extractor: Browser HTML → PubChem

Extracts chemical data from a browser-saved jqGrid table webpage,
looks up CAS numbers in PubChem, and opens a browser search.
"""

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Fix for PyInstaller Windows builds with console=False
# Redirect output to a log file so users can check progress
if sys.stdout is None:
    _log_path = os.path.join(os.path.dirname(sys.executable), "chemical_extractor.log")
    _log_file = open(_log_path, 'a', encoding='utf-8', buffering=1)  # line buffered
    sys.stdout = _log_file
    sys.stderr = _log_file

import aiohttp
import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# Snappy decompression for Firefox localStorage (try multiple backends)
def _snappy_decompress(data: bytes) -> bytes:
    """Decompress snappy data using available backend."""
    try:
        import snappy
        return snappy.decompress(data)
    except ImportError:
        pass
    try:
        import cramjam
        return bytes(cramjam.snappy.decompress_raw(data))
    except ImportError:
        pass
    raise ImportError("No snappy decompression available. Install python-snappy or cramjam.")

CTS_API_URL = "https://cts.fiehnlab.ucdavis.edu/rest/convert/CAS/PubChem%20CID"

# Constants
PUBCHEM_BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_SEARCH_URL = "https://pubchem.ncbi.nlm.nih.gov/#query="
CAS_PATTERN = re.compile(r"^\d{1,7}-\d{2}-\d$")
RATE_LIMIT_DELAY = 0.25  # 4 requests per second (under PubChem's 5/sec limit)
MAX_URL_LENGTH = 8000  # Safe browser URL limit

# ============================================================================
# Path Configuration (handles both regular Python and PyInstaller bundles)
# ============================================================================

def _get_bundle_dir() -> Path:
    """Get the directory containing bundled data files (for PyInstaller)."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running as a PyInstaller bundle
        return Path(sys._MEIPASS)
    else:
        # Running as a regular Python script
        return Path(__file__).parent


def _get_app_dir() -> Path:
    """Get the application directory for user data (writable location)."""
    if getattr(sys, 'frozen', False):
        # Running as a PyInstaller bundle - use directory containing the exe
        return Path(sys.executable).parent
    else:
        # Running as a regular Python script
        return Path(__file__).parent


# Bundle directory (read-only, contains static data like the TSV lookup file)
BUNDLE_DIR = _get_bundle_dir()

# Application directory (writable, for user data like snapshots and cache)
APP_DIR = _get_app_dir()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger = logging.getLogger("chemical_extractor")


def setup_logging() -> None:
    """Configure file + console logging for the application."""
    if logger.handlers:
        return  # already configured

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Always write to a log file
    file_handler = logging.FileHandler(
        APP_DIR / "chemical_extractor.log", encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Console handler (only when stdout exists)
    if sys.stdout is not None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)


setup_logging()

# Data directory structure (user data - writable)
DATA_DIR = APP_DIR / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LATEST_POINTER = DATA_DIR / "latest.txt"  # Text file containing path to current snapshot
CID_CACHE_FILE = DATA_DIR / "cid_cache.json"
RUG_TABLE_FILE = DATA_DIR / "rug_table.json"
FILTER_RESULTS_FILE = DATA_DIR / "filter_results.json"
COMPOUND_INFO_FILE = DATA_DIR / "compound_info.json"
APP_SEARCHES_FILE = DATA_DIR / "app_searches.json"
STALE_SEARCHES_FILE = DATA_DIR / "stale_searches.json"

# Legacy cache configuration (for CAS→CID API lookups)
CACHE_DIR = Path.home() / ".cache" / "cas_to_cid"
CACHE_FILE = CACHE_DIR / "cache.json"

# PubChem dump file (CID→CAS TSV, we reverse to CAS→CID)
# This is a static file bundled with the application (gzipped to save space)
PUBCHEM_DUMP_FILE = BUNDLE_DIR / "pubchem_dump_cid_to_cas.tsv.gz"

# Global cache for the PubChem dump (loaded once)
_pubchem_dump_cache: dict[str, int] | None = None

# Global storage for active browser sessions (for web UI two-phase flow)
_browser_sessions: dict[str, object] = {}

# Firefox localStorage path for PubChem history (platform-specific)
def _get_firefox_profiles_dir() -> Path:
    """Get the Firefox profiles directory for the current platform."""
    if sys.platform == "win32":
        # Windows: %APPDATA%\Mozilla\Firefox\Profiles
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
        return Path.home() / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    elif sys.platform == "darwin":
        # macOS: ~/Library/Application Support/Firefox/Profiles
        return Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
    else:
        # Linux: ~/.mozilla/firefox
        return Path.home() / ".mozilla" / "firefox"


FIREFOX_PROFILES_DIR = _get_firefox_profiles_dir()
PUBCHEM_LOCALSTORAGE_SUBPATH = "storage/default/https+++pubchem.ncbi.nlm.nih.gov/ls/data.sqlite"


def _get_chrome_local_storage_dir() -> Path | None:
    """Get the Chrome Local Storage LevelDB directory for the current platform."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            p = Path(local) / "Google" / "Chrome" / "User Data" / "Default" / "Local Storage" / "leveldb"
        else:
            p = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Local Storage" / "leveldb"
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Local Storage" / "leveldb"
    else:
        p = Path.home() / ".config" / "google-chrome" / "Default" / "Local Storage" / "leveldb"
    logger.debug("Chrome Local Storage path: %s", p)
    if p.exists():
        logger.debug("Chrome Local Storage directory exists")
        return p
    logger.debug("Chrome Local Storage directory does not exist")
    return None


def _parse_history_entries(history: list) -> list[dict]:
    """Parse raw PubChem history JSON into standardised entry dicts."""
    results = []
    for entry in history:
        details = entry.get("details", {})
        cache_key = details.get("cachekey")
        if not cache_key:
            continue
        timestamp_ms = entry.get("timestamp", 0)
        timestamp_dt = datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else None
        results.append({
            "name": details.get("name", "Unknown search"),
            "cachekey": cache_key,
            "timestamp": timestamp_dt.isoformat() if timestamp_dt else None,
            "timestamp_display": timestamp_dt.strftime("%Y-%m-%d %H:%M:%S") if timestamp_dt else "Unknown",
            "list_size": details.get("listsize"),
            "type": details.get("type", "compound"),
            "domain": details.get("domain", "compound"),
            "url": f"https://pubchem.ncbi.nlm.nih.gov/#query={cache_key}",
        })
    return results


def _get_firefox_pubchem_history() -> list[dict]:
    """Read PubChem search history from Firefox localStorage."""
    logger.info("Attempting to read Firefox localStorage")
    db_path = find_firefox_pubchem_db()
    if not db_path:
        logger.info("Firefox PubChem localStorage DB not found")
        return []

    logger.debug("Found Firefox DB: %s", db_path)
    temp_db = tempfile.mktemp(suffix=".sqlite")
    try:
        shutil.copy(db_path, temp_db)
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        cursor.execute("SELECT value, compression_type FROM data WHERE key='history'")
        row = cursor.fetchone()
        conn.close()

        if not row:
            logger.info("No 'history' key found in Firefox localStorage")
            return []

        value, compression_type = row
        if compression_type == 1:
            logger.debug("Decompressing snappy-compressed value")
            value = _snappy_decompress(value)

        history = json.loads(value)
        entries = _parse_history_entries(history) if history else []
        logger.info("Firefox: parsed %d history entries", len(entries))
        return entries
    except Exception as e:
        logger.exception("Error reading Firefox localStorage: %s", e)
        return []
    finally:
        if Path(temp_db).exists():
            Path(temp_db).unlink()


def _get_chrome_pubchem_history() -> list[dict]:
    """Read PubChem search history from Chrome localStorage (LevelDB)."""
    logger.info("Attempting to read Chrome localStorage")
    ls_dir = _get_chrome_local_storage_dir()
    if not ls_dir:
        logger.info("Chrome Local Storage directory not found")
        return []

    try:
        from ccl_chromium_reader import ccl_chromium_localstorage
    except ImportError:
        logger.warning("ccl_chromium_reader not installed — cannot read Chrome localStorage")
        return []

    try:
        logger.debug("Opening LevelDB at %s", ls_dir)
        lsdb = ccl_chromium_localstorage.LocalStoreDb(ls_dir)
        for record in lsdb.iter_records_for_storage_key("https://pubchem.ncbi.nlm.nih.gov"):
            logger.debug("Record: storage_key=%s  script_key=%s", record.storage_key, record.script_key)
            if record.script_key == "history":
                history = json.loads(record.value)
                entries = _parse_history_entries(history) if history else []
                logger.info("Chrome: found 'history' key with %d entries", len(entries))
                return entries
        logger.info("Chrome: no 'history' key found for pubchem.ncbi.nlm.nih.gov")
        return []
    except Exception as e:
        logger.exception("Error reading Chrome localStorage: %s", e)
        return []


def find_firefox_pubchem_db() -> Path | None:
    """Find the Firefox localStorage database for PubChem."""
    if not FIREFOX_PROFILES_DIR.exists():
        return None

    # Try to find profiles.ini to get the default profile
    profiles_ini = FIREFOX_PROFILES_DIR.parent / "profiles.ini"
    default_profile = None

    if profiles_ini.exists():
        content = profiles_ini.read_text()
        # Look for Default= in [Install...] section
        for line in content.split("\n"):
            if line.startswith("Default="):
                default_profile = line.split("=", 1)[1].strip()
                break

    # Search for PubChem localStorage in profiles
    candidates = []
    for profile_dir in FIREFOX_PROFILES_DIR.iterdir():
        if profile_dir.is_dir():
            db_path = profile_dir / PUBCHEM_LOCALSTORAGE_SUBPATH
            if db_path.exists():
                # Prioritize default profile
                if default_profile and default_profile in str(profile_dir):
                    return db_path
                candidates.append(db_path)

    return candidates[0] if candidates else None


def get_history_fingerprint() -> str:
    """Return a cheap fingerprint based on browser storage file mtimes.

    For Firefox the localStorage file is site-specific (only changes when
    PubChem writes to it).  For Chrome the LevelDB dir is shared across all
    sites, so its mtime changes frequently — we still include it so Chrome-
    only users get updates, but it will cause more frequent (harmless) full
    fetches.
    """
    parts: list[str] = []

    # Firefox: site-specific sqlite file
    ff_db = find_firefox_pubchem_db()
    if ff_db:
        try:
            parts.append(f"ff:{ff_db.stat().st_mtime_ns}")
        except OSError:
            parts.append("ff:err")
    else:
        parts.append("ff:none")

    # Chrome: shared LevelDB directory
    chrome_dir = _get_chrome_local_storage_dir()
    if chrome_dir:
        try:
            parts.append(f"cr:{chrome_dir.stat().st_mtime_ns}")
        except OSError:
            parts.append("cr:err")
    else:
        parts.append("cr:none")

    return "|".join(parts)


def get_latest_pubchem_history_cachekey() -> str | None:
    """
    Get the latest PubChem search cache key from browser history (Firefox + Chrome).

    Returns:
        The cache key of the most recent PubChem search, or None if not found.
    """
    history = get_pubchem_history_details()
    if not history:
        print("Warning: No PubChem history found in any browser")
        return None

    cache_key = history[0]["cachekey"]
    name = history[0]["name"]
    print(f"Found latest PubChem search: '{name}' (key: {cache_key[:20]}...)")
    return cache_key


def get_pubchem_history_details() -> list[dict]:
    """
    Read PubChem search history from all supported browsers (Firefox + Chrome).

    Returns:
        List of dicts sorted by timestamp (newest first), deduplicated by cachekey.
    """
    firefox_history = _get_firefox_pubchem_history()
    for entry in firefox_history:
        entry["browser"] = "Firefox"
    chrome_history = _get_chrome_pubchem_history()
    for entry in chrome_history:
        entry["browser"] = "Chrome"

    logger.info("History counts — Firefox: %d, Chrome: %d", len(firefox_history), len(chrome_history))

    # Merge and deduplicate by cachekey (keep newest)
    seen: dict[str, dict] = {}
    for entry in firefox_history + chrome_history:
        key = entry["cachekey"]
        if key not in seen or (entry["timestamp"] or "") > (seen[key]["timestamp"] or ""):
            seen[key] = entry

    results = list(seen.values())
    results.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    logger.info("Merged/deduplicated history: %d entries", len(results))
    return results


def get_default_browser() -> str | None:
    """Return the name of the user's default browser, or None if unknown."""
    try:
        if sys.platform == "darwin":
            import subprocess
            result = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True,
            )
            # Fallback: read the http handler via shell
            result2 = subprocess.run(
                ["defaults", "read",
                 "~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True,
            )
            # More reliable approach on macOS
            from urllib.request import urlopen  # noqa: F811
            import plistlib
            result3 = subprocess.run(
                ["plutil", "-convert", "xml1", "-o", "-",
                 os.path.expanduser(
                     "~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
                 )],
                capture_output=True,
            )
            if result3.returncode == 0:
                plist = plistlib.loads(result3.stdout)
                for handler in plist.get("LSHandlers", []):
                    scheme = handler.get("LSHandlerURLScheme", "")
                    if scheme in ("http", "https"):
                        bundle_id = handler.get("LSHandlerRoleAll", "").lower()
                        if "chrome" in bundle_id:
                            return "Chrome"
                        elif "firefox" in bundle_id:
                            return "Firefox"
                        elif "safari" in bundle_id:
                            return "Safari"
                        elif "brave" in bundle_id:
                            return "Brave"
                        elif "opera" in bundle_id:
                            return "Opera"
                        elif "edge" in bundle_id:
                            return "Edge"
                        else:
                            return bundle_id
        elif sys.platform == "win32":
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
            ) as key:
                prog_id = winreg.QueryValueEx(key, "ProgId")[0].lower()
                if "chrome" in prog_id:
                    return "Chrome"
                elif "firefox" in prog_id:
                    return "Firefox"
                elif "edge" in prog_id:
                    return "Edge"
                elif "brave" in prog_id:
                    return "Brave"
                elif "opera" in prog_id:
                    return "Opera"
                else:
                    return prog_id
        else:
            # Linux: check xdg-settings
            import subprocess
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                browser = result.stdout.strip().lower()
                if "chrome" in browser or "chromium" in browser:
                    return "Chrome"
                elif "firefox" in browser:
                    return "Firefox"
                elif "brave" in browser:
                    return "Brave"
                elif "opera" in browser:
                    return "Opera"
                else:
                    return result.stdout.strip()
    except Exception:
        logger.debug("Could not detect default browser", exc_info=True)
    return None


def combine_pubchem_cache_keys(
    key1: str, key2: str, operation: str = "AND"
) -> str | None | tuple[str, int]:
    """
    Combine two PubChem cache keys with a boolean operation.

    Args:
        key1: First cache key
        key2: Second cache key
        operation: Boolean operation (AND, OR, NOT)

    Returns:
        Tuple of (combined_cache_key, list_size), or None if failed
    """
    operation = operation.upper()
    if operation not in ("AND", "OR", "NOT"):
        print(f"Error: Invalid operation '{operation}'. Use AND, OR, or NOT.")
        return None

    query = {
        "Query": {
            "Action": [
                {"List": {"CacheKey": key1}},
                {"List": {"CacheKey": key2}},
                {"Operation": operation}
            ],
            "Return": "CacheKey"
        }
    }

    try:
        url = (
            "https://pubchem.ncbi.nlm.nih.gov/list_gateway/list_refinement.cgi"
            f"?format=json&query={quote(json.dumps(query))}"
        )
        resp = requests.get(url, timeout=60)

        if resp.status_code == 200:
            data = resp.json()
            if "Response" in data and "List" in data["Response"]:
                combined_key = data["Response"]["List"]["CacheKey"]
                list_size = data["Response"].get("ListSize", 0)
                if isinstance(list_size, str):
                    try:
                        list_size = int(list_size)
                    except ValueError:
                        list_size = 0
                print(f"Combined searches ({operation}): {list_size} results")
                return (combined_key, list_size)
            elif "Error" in data.get("Response", {}):
                print(f"PubChem combine error: {data['Response']['Error']}")
    except Exception as e:
        print(f"Error combining cache keys: {e}")

    return None


def load_pubchem_dump() -> dict[str, int]:
    """Load the PubChem CAS→CID mapping from the gzipped TSV dump file."""
    import gzip

    global _pubchem_dump_cache

    if _pubchem_dump_cache is not None:
        return _pubchem_dump_cache

    if not PUBCHEM_DUMP_FILE.exists():
        print(f"Warning: PubChem dump file not found: {PUBCHEM_DUMP_FILE}")
        _pubchem_dump_cache = {}
        return _pubchem_dump_cache

    print(f"Loading PubChem dump from {PUBCHEM_DUMP_FILE}...")
    start_time = time.time()
    cas_to_cid = {}

    with gzip.open(PUBCHEM_DUMP_FILE, "rt") as f:  # "rt" = read text mode
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 2:
                cid, cas = parts[0], parts[1]
                try:
                    cas_to_cid[cas] = int(cid)
                except ValueError:
                    continue

    elapsed = time.time() - start_time
    print(f"Loaded {len(cas_to_cid):,} CAS→CID mappings in {elapsed:.1f}s")
    _pubchem_dump_cache = cas_to_cid
    return _pubchem_dump_cache


def load_cache() -> dict[str, int | None]:
    """Load the CAS→CID cache from disk."""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(cache: dict[str, int | None]) -> None:
    """Save the CAS→CID cache to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA256 hash of a file's contents."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return f"sha256:{sha256.hexdigest()}"


def load_cid_cache() -> dict | None:
    """
    Load the CID cache from disk.

    Returns:
        Cache dict with source_html, source_hash, created, stats, results
        or None if cache doesn't exist or is invalid
    """
    if not CID_CACHE_FILE.exists():
        return None
    try:
        return json.loads(CID_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_cid_cache(
    source_html: Path,
    source_hash: str,
    results: dict[str, dict],
) -> None:
    """
    Save CID lookup results to cache.

    Args:
        source_html: Path to the HTML file that was parsed
        source_hash: SHA256 hash of the HTML file
        results: Dictionary mapping CAS numbers to {status, cid}
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Compute stats
    found = sum(1 for r in results.values() if r.get("cid") is not None)
    not_found = len(results) - found

    cache_data = {
        "source_html": str(source_html),
        "source_hash": source_hash,
        "created": datetime.now().isoformat(),
        "stats": {
            "total_cas": len(results),
            "found_cids": found,
            "not_found": not_found,
        },
        "results": results,
    }

    CID_CACHE_FILE.write_text(json.dumps(cache_data, indent=2))
    print(f"Saved CID cache: {found} found, {not_found} not found")


def is_cid_cache_valid(html_path: Path) -> tuple[bool, dict | None]:
    """
    Check if CID cache is valid for the given HTML file.

    Args:
        html_path: Path to the HTML file

    Returns:
        Tuple of (is_valid, cache_data or None)
    """
    cache = load_cid_cache()
    if not cache:
        return False, None

    # Compute current file hash
    current_hash = compute_file_hash(html_path)

    # Compare with cached hash
    if cache.get("source_hash") == current_hash:
        return True, cache

    print(f"Cache invalid: HTML content changed (hash mismatch)")
    return False, None


async def fetch_single_cas_cts(
    session: aiohttp.ClientSession,
    cas: str,
    semaphore: asyncio.Semaphore
) -> tuple[str, int | None]:
    """Fetch a single CAS→CID from CTS API."""
    url = f"{CTS_API_URL}/{cas}"

    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and len(data) > 0:
                        results = data[0].get("results", [])
                        if results:
                            return (cas, int(results[0]))
                return (cas, None)
        except Exception:
            return (cas, None)


async def lookup_via_cts_async(cas_numbers: list[str]) -> dict[str, int | None]:
    """
    Batch lookup CAS→CID via Chemical Translation Service REST API.

    Args:
        cas_numbers: List of CAS numbers to look up

    Returns:
        Dictionary mapping CAS numbers to CIDs (or None if not found)
    """
    if not cas_numbers:
        return {}

    semaphore = asyncio.Semaphore(10)  # CTS can handle more concurrent requests
    results = {}

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_single_cas_cts(session, cas, semaphore) for cas in cas_numbers]

        print(f"Looking up {len(cas_numbers)} CAS numbers via CTS...")
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="CTS lookup"):
            cas, cid = await coro
            results[cas] = cid

    return results




async def fetch_single_cas_async(
    session: aiohttp.ClientSession,
    cas: str,
    semaphore: asyncio.Semaphore
) -> tuple[str, int | None]:
    """
    Fetch a single CAS number from PubChem asynchronously.

    Args:
        session: aiohttp client session
        cas: CAS number to look up
        semaphore: Semaphore for rate limiting

    Returns:
        Tuple of (CAS number, CID or None)
    """
    url = f"{PUBCHEM_BASE_URL}/compound/name/{cas}/cids/JSON"

    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    data = await response.json()
                    cids = data.get("IdentifierList", {}).get("CID", [])
                    if cids:
                        return (cas, cids[0])
                return (cas, None)
        except Exception:
            return (cas, None)


async def lookup_pubchem_async(cas_numbers: list[str]) -> dict[str, int | None]:
    """
    Async fallback for CAS numbers not found in CTS.

    Args:
        cas_numbers: List of CAS numbers to look up

    Returns:
        Dictionary mapping CAS numbers to CIDs (or None if not found)
    """
    if not cas_numbers:
        return {}

    semaphore = asyncio.Semaphore(5)  # 5 requests/sec limit
    results = {}

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_single_cas_async(session, cas, semaphore) for cas in cas_numbers]

        print(f"Looking up {len(cas_numbers)} CAS numbers via PubChem async...")
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="PubChem async"):
            cas, cid = await coro
            results[cas] = cid
            await asyncio.sleep(0.2)  # Rate limit delay

    return results


async def lookup_cas_to_cid_async(cas_numbers: list[str], cache: dict) -> dict[str, int | None]:
    """
    Async multi-layer CAS→CID lookup with CTS and PubChem fallback.

    Args:
        cas_numbers: List of CAS numbers to look up (not in cache)
        cache: Existing cache dict

    Returns:
        Dictionary mapping CAS numbers to CIDs (or None)
    """
    # Layer 2: Batch lookup via CTS
    cts_results = await lookup_via_cts_async(cas_numbers)
    found_in_cts = {k: v for k, v in cts_results.items() if v is not None}
    still_missing = [cas for cas in cas_numbers if cts_results.get(cas) is None]

    print(f"CTS found: {len(found_in_cts)}, still missing: {len(still_missing)}")

    # Layer 3: Async PubChem fallback for missing
    pubchem_results = {}
    if still_missing:
        pubchem_results = await lookup_pubchem_async(still_missing)

    return {**found_in_cts, **pubchem_results}


def lookup_cas_to_cid_optimized(cas_numbers: list[str]) -> dict[str, dict]:
    """
    Multi-layer CAS→CID lookup:
      1. PubChem dump file (instant)
      2. Local cache (instant)
      3. CTS API (async)
      4. PubChem API (async fallback)

    Args:
        cas_numbers: List of CAS numbers to look up

    Returns:
        Dictionary mapping CAS numbers to results (status, cid)
    """
    # Layer 1: Check PubChem dump file
    pubchem_dump = load_pubchem_dump()
    from_dump = {cas: pubchem_dump[cas] for cas in cas_numbers if cas in pubchem_dump}
    remaining = [cas for cas in cas_numbers if cas not in pubchem_dump]

    print(f"PubChem dump hits: {len(from_dump)}, remaining: {len(remaining)}")

    if not remaining:
        return _format_results(from_dump)

    # Layer 2: Check local cache (for CAS not in dump)
    cache = load_cache()
    from_cache = {cas: cache[cas] for cas in remaining if cas in cache}
    remaining = [cas for cas in remaining if cas not in cache]

    print(f"Cache hits: {len(from_cache)}, remaining: {len(remaining)}")

    if not remaining:
        return _format_results({**from_dump, **from_cache})

    # Layers 3 & 4: CTS + PubChem async (single event loop)
    new_results = asyncio.run(lookup_cas_to_cid_async(remaining, cache))

    # Merge all results
    all_cids = {**from_dump, **from_cache, **new_results}

    # Update cache with new findings (only from API lookups)
    for cas, cid in new_results.items():
        cache[cas] = cid
    save_cache(cache)

    # Summary
    found = sum(1 for cid in all_cids.values() if cid is not None)
    not_found = len(all_cids) - found
    print(f"\nLookup complete: {found} found, {not_found} not found")

    return _format_results(all_cids)


def _format_results(cid_map: dict[str, int | None]) -> dict[str, dict]:
    """Convert CID map to the expected result format."""
    results = {}
    for cas, cid in cid_map.items():
        if cid is not None:
            results[cas] = {"status": "found", "cid": cid}
        else:
            results[cas] = {"status": "not_found", "cid": None}
    return results


async def repair_entry_by_text_search(
    entry_name: str,
    cas_number: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore
) -> dict | None:
    """
    Attempt to find CID by searching PubChem with the entry name.

    Args:
        entry_name: Chemical name from RUG table
        cas_number: Original CAS (for tracking)
        session: aiohttp session
        semaphore: Rate limiting semaphore

    Returns:
        {
            "cid": int,
            "repair_source": "text_search:{query}",
            "repair_timestamp": "2024-01-27T..."
        } or None if no match
    """
    if not entry_name or entry_name == "-":
        return None

    # Clean entry name for URL
    query = entry_name.strip()
    import urllib.parse
    url = f"{PUBCHEM_BASE_URL}/compound/name/{urllib.parse.quote(query)}/cids/JSON"

    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 404:
                    logger.info(f"Text search no match: {entry_name}")
                    return None

                resp.raise_for_status()
                data = await resp.json()
                cids = data.get("IdentifierList", {}).get("CID", [])

                if cids:
                    best_match_cid = cids[0]  # First = best match
                    logger.info(f"Text search match: {entry_name} → CID {best_match_cid}")

                    # Reverse-lookup real CAS from PubChem dump
                    real_cas = None
                    try:
                        cas_to_cid = load_pubchem_dump()
                        if cas_to_cid:
                            # Build CID→CAS reverse map (first match wins)
                            for dump_cas, dump_cid in cas_to_cid.items():
                                if dump_cid == best_match_cid:
                                    real_cas = dump_cas
                                    break
                            if real_cas:
                                logger.info(f"Reverse CAS lookup: CID {best_match_cid} → CAS {real_cas}")
                    except Exception as e:
                        logger.warning(f"CAS reverse lookup failed for CID {best_match_cid}: {e}")

                    return {
                        "cid": best_match_cid,
                        "real_cas": real_cas,
                        "repair_source": f"text_search:{query}",
                        "repair_timestamp": datetime.now().isoformat()
                    }
                return None

        except Exception as e:
            logger.warning(f"Text search error for '{entry_name}': {e}")
            return None
        finally:
            # Rate limiting delay
            await asyncio.sleep(0.25)


async def repair_unmatched_entries(
    progress_callback=None,
    skip_repaired=True
) -> dict:
    """
    Repair unmatched entries by searching PubChem with entry names.
    Always operates in review mode — results are returned for user approval.

    Args:
        progress_callback: Optional callback(processed, total, current_name)
        skip_repaired: If True, skip entries already repaired or failed

    Returns:
        {
            "total_attempts": int,
            "successful_repairs": int,
            "failed_repairs": int,
            "skipped": int,
            "repaired_entries": [{"row_index": int, "cas": str, "name": str, "cid": int, "real_cas": str|None}, ...],
            "failed_indices": [int, ...]
        }
    """
    # Load current data
    rug_table = load_rug_table()
    cid_cache = load_cid_cache()

    if not rug_table or not cid_cache:
        logger.error("Cannot repair: missing RUG table or CID cache")
        return {"error": "Missing data"}

    # Find unmatched entries (track row_index)
    unmatched = []
    for row_index, row in enumerate(rug_table.get("rows", [])):
        cid_val = row.get("CID")
        # Check for None or NaN
        if cid_val is None or (isinstance(cid_val, float) and cid_val != cid_val):
            cas = row.get("Casnr", "")
            name = row.get("Name", "")

            # Skip if already repaired or failed (check row metadata)
            if skip_repaired:
                row_status = row.get("_repair_status")
                if row_status in ("repaired", "failed"):
                    continue

            if name and name != "-":
                unmatched.append({"cas": cas, "name": name, "row": row, "row_index": row_index})

    total = len(unmatched)
    logger.info(f"Starting repair for {total} unmatched entries (review-only)")

    results = {
        "total_attempts": total,
        "successful_repairs": 0,
        "failed_repairs": 0,
        "skipped": 0,
        "repaired_entries": [],
        "failed_indices": []
    }

    async with aiohttp.ClientSession() as session:
        semaphore = asyncio.Semaphore(5)  # 5 concurrent max (PubChem limit)

        for idx, entry in enumerate(unmatched):
            if progress_callback:
                progress_callback(idx + 1, total, entry["name"])

            repair_result = await repair_entry_by_text_search(
                entry["name"],
                entry["cas"],
                session,
                semaphore
            )

            if repair_result:
                results["successful_repairs"] += 1
                results["repaired_entries"].append({
                    "row_index": entry["row_index"],
                    "cas": entry["cas"],
                    "name": entry["name"],
                    "cid": repair_result["cid"],
                    "real_cas": repair_result.get("real_cas"),
                    "repair_source": repair_result["repair_source"]
                })
            else:
                results["failed_repairs"] += 1
                results["failed_indices"].append(entry["row_index"])

    logger.info(f"Repair complete: {results['successful_repairs']} repaired, {results['failed_repairs']} failed")
    return results


def parse_html_table(html_path: Path) -> pd.DataFrame:
    """
    Parse the jqGrid table from the HTML file.

    Args:
        html_path: Path to the HTML file

    Returns:
        DataFrame with all extracted table data
    """
    print(f"Loading HTML file: {html_path}")

    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    soup = BeautifulSoup(content, "lxml")
    rows = soup.select("tr.jqgrow")

    if not rows:
        raise ValueError("No table rows found with class 'jqgrow'")

    print(f"Found {len(rows)} table rows")

    # Extract data from each row
    data = []
    columns = None

    for row in rows:
        row_data = {}
        cells = row.find_all("td")

        for cell in cells:
            aria_describedby = cell.get("aria-describedby", "")
            # Extract column name from aria-describedby (e.g., "chemList_Casnr" → "Casnr")
            col_name = aria_describedby.replace("chemList_", "") if aria_describedby else None

            if col_name:
                # Decode HTML entities and strip whitespace
                text = html.unescape(cell.get_text(strip=True))
                # Handle &nbsp; which becomes \xa0 after unescape
                text = text.replace("\xa0", "").strip()
                row_data[col_name] = text

        if row_data:
            data.append(row_data)
            if columns is None:
                columns = list(row_data.keys())

    df = pd.DataFrame(data, columns=columns)
    print(f"Extracted {len(df)} rows with {len(df.columns)} columns")
    print(f"Columns: {', '.join(df.columns)}")

    return df


def validate_cas_number(cas: str) -> bool:
    """
    Validate a CAS number format.

    Args:
        cas: CAS number string

    Returns:
        True if valid format, False otherwise
    """
    if not cas or cas == "00-00-0":
        return False
    return bool(CAS_PATTERN.match(cas))


def extract_cas_numbers(df: pd.DataFrame, cas_column: str = "Casnr") -> list[str]:
    """
    Extract unique valid CAS numbers from the DataFrame.

    Args:
        df: DataFrame with chemical data
        cas_column: Name of the CAS number column

    Returns:
        List of unique valid CAS numbers
    """
    if cas_column not in df.columns:
        raise ValueError(f"Column '{cas_column}' not found in DataFrame")

    cas_numbers = df[cas_column].astype(str).tolist()

    # Filter and deduplicate
    valid_cas = []
    seen = set()
    invalid_count = 0

    for cas in cas_numbers:
        cas = cas.strip()
        if validate_cas_number(cas):
            if cas not in seen:
                valid_cas.append(cas)
                seen.add(cas)
        else:
            invalid_count += 1

    print(f"Found {len(valid_cas)} unique valid CAS numbers ({invalid_count} invalid/empty entries)")

    return valid_cas


def lookup_cas_in_pubchem(cas_numbers: list[str]) -> dict[str, dict]:
    """
    Look up CAS numbers in PubChem API.

    Args:
        cas_numbers: List of CAS numbers to look up

    Returns:
        Dictionary mapping CAS numbers to results (status, cid)
    """
    results = {}

    print(f"\nLooking up {len(cas_numbers)} CAS numbers in PubChem...")

    for cas in tqdm(cas_numbers, desc="PubChem lookups"):
        url = f"{PUBCHEM_BASE_URL}/compound/name/{cas}/cids/JSON"

        try:
            response = requests.get(url, timeout=30)

            if response.status_code == 200:
                data = response.json()
                cids = data.get("IdentifierList", {}).get("CID", [])
                if cids:
                    results[cas] = {"status": "found", "cid": cids[0]}
                else:
                    results[cas] = {"status": "no_cid", "cid": None}

            elif response.status_code == 404:
                results[cas] = {"status": "not_found", "cid": None}

            elif response.status_code == 429:
                # Rate limit hit - exponential backoff
                print(f"\nRate limited, waiting...")
                time.sleep(5)
                # Retry once
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    cids = data.get("IdentifierList", {}).get("CID", [])
                    if cids:
                        results[cas] = {"status": "found", "cid": cids[0]}
                    else:
                        results[cas] = {"status": "no_cid", "cid": None}
                else:
                    results[cas] = {"status": f"error_{response.status_code}", "cid": None}
            else:
                results[cas] = {"status": f"error_{response.status_code}", "cid": None}

        except requests.exceptions.RequestException as e:
            results[cas] = {"status": f"error_{type(e).__name__}", "cid": None}

        # Rate limiting
        time.sleep(RATE_LIMIT_DELAY)

    # Summary
    found = sum(1 for r in results.values() if r["status"] == "found")
    not_found = sum(1 for r in results.values() if r["status"] == "not_found")
    errors = len(results) - found - not_found

    print(f"\nPubChem lookup complete:")
    print(f"  Found: {found}")
    print(f"  Not found: {not_found}")
    print(f"  Errors: {errors}")

    return results


def save_outputs(
    df: pd.DataFrame,
    cas_numbers: list[str],
    pubchem_results: dict[str, dict],
    output_dir: Path
) -> dict[str, Path]:
    """
    Save all output files.

    Args:
        df: Full DataFrame
        cas_numbers: List of valid CAS numbers
        pubchem_results: PubChem lookup results
        output_dir: Output directory

    Returns:
        Dictionary mapping output type to file path
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_files = {}

    # 1. Full table CSV
    table_path = output_dir / "chemicals_table.csv"
    df.to_csv(table_path, index=False)
    output_files["table"] = table_path
    print(f"Saved full table to: {table_path}")

    # 2. CAS numbers list
    cas_path = output_dir / "cas_numbers.txt"
    with open(cas_path, "w") as f:
        f.write("\n".join(cas_numbers))
    output_files["cas"] = cas_path
    print(f"Saved CAS numbers to: {cas_path}")

    # 3. CAS to PubChem mapping
    mapping_data = [
        {"cas": cas, "status": result["status"], "cid": result["cid"]}
        for cas, result in pubchem_results.items()
    ]
    mapping_df = pd.DataFrame(mapping_data)
    mapping_path = output_dir / "cas_to_pubchem.csv"
    mapping_df.to_csv(mapping_path, index=False)
    output_files["mapping"] = mapping_path
    print(f"Saved CAS→CID mapping to: {mapping_path}")

    # 4. PubChem CIDs list (only found ones)
    cids = [str(r["cid"]) for r in pubchem_results.values() if r["cid"]]
    cids_path = output_dir / "pubchem_cids.txt"
    with open(cids_path, "w") as f:
        f.write("\n".join(cids))
    output_files["cids"] = cids_path
    print(f"Saved PubChem CIDs to: {cids_path}")

    return output_files


def upload_cids_to_pubchem_cache(cids: list[str]) -> str | None:
    """
    Upload CIDs to PubChem's cache and get a cache_key for browser search.

    Args:
        cids: List of CID strings

    Returns:
        cache_key string if successful, None if failed
    """
    try:
        print(f"Uploading {len(cids)} CIDs to PubChem cache...")
        resp = requests.post(
            "https://pubchem.ncbi.nlm.nih.gov/list_gateway/list_gateway.cgi"
            "?format=json&action=post_to_cache&id_type=cid",
            data={"ids": ",".join(cids)},
            timeout=60
        )
        print(f"PubChem cache response: status={resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            if "Response" in data and "cache_key" in data["Response"]:
                cache_key = data["Response"]["cache_key"]
                list_size = data["Response"].get("list_size", len(cids))
                print(f"Cached {list_size} CIDs, key: {cache_key[:20]}...")
                return cache_key
            elif "error" in data.get("Response", {}):
                print(f"PubChem cache error: {data['Response']['error']}")
            else:
                print(f"Unexpected PubChem response structure: {data}")
        else:
            print(f"PubChem cache HTTP error: {resp.status_code}, body: {resp.text[:500]}")
    except Exception as e:
        print(f"Failed to upload to PubChem cache: {e}")
    return None


def save_html_snapshot(html_content: str) -> Path:
    """
    Save HTML content as a timestamped snapshot.

    Args:
        html_content: HTML content to save

    Returns:
        Path to the saved snapshot file
    """
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = SNAPSHOTS_DIR / f"Search_{timestamp}.html"

    snapshot_path.write_text(html_content, encoding="utf-8")
    print(f"Saved snapshot: {snapshot_path}")

    # Update pointer
    update_latest_pointer(snapshot_path)

    return snapshot_path


def update_latest_pointer(snapshot_path: Path) -> None:
    """
    Update the latest.txt pointer to the given snapshot.

    Args:
        snapshot_path: Path to the snapshot file
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Store relative path as text
    relative_path = snapshot_path.relative_to(DATA_DIR)
    LATEST_POINTER.write_text(str(relative_path))
    print(f"Updated pointer: {LATEST_POINTER.name} -> {relative_path}")


def get_latest_snapshot() -> Path | None:
    """
    Get the path to the current snapshot, or None if not set.

    Returns:
        Path to the latest snapshot file, or None if not found
    """
    # Check new pointer file first
    if LATEST_POINTER.exists():
        relative = LATEST_POINTER.read_text().strip()
        snapshot = DATA_DIR / relative
        if snapshot.exists():
            return snapshot

    # Fallback: check for old symlink (migration path)
    old_symlink = DATA_DIR / "latest.html"
    if old_symlink.is_symlink() or old_symlink.exists():
        try:
            resolved = old_symlink.resolve()
            if resolved.exists() and resolved.suffix == ".html":
                # Migrate to new system
                update_latest_pointer(resolved)
                try:
                    old_symlink.unlink()
                except Exception:
                    pass
                return resolved
        except Exception:
            pass

    return None


def list_snapshots() -> list[dict]:
    """
    List all available HTML snapshots.

    Returns:
        List of dicts with path, timestamp, and size info, sorted newest first
    """
    if not SNAPSHOTS_DIR.exists():
        return []

    latest_path = get_latest_snapshot()
    snapshots = []
    for path in SNAPSHOTS_DIR.glob("Search_*.html"):
        # Parse timestamp from filename
        try:
            ts_str = path.stem.replace("Search_", "")
            timestamp = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
        except ValueError:
            timestamp = datetime.fromtimestamp(path.stat().st_mtime)

        snapshots.append({
            "path": path,
            "timestamp": timestamp,
            "size": path.stat().st_size,
            "is_latest": latest_path is not None and latest_path.resolve() == path.resolve(),
        })

    # Sort by timestamp, newest first
    snapshots.sort(key=lambda x: x["timestamp"], reverse=True)
    return snapshots


def print_snapshots() -> None:
    """Print a formatted list of available snapshots."""
    snapshots = list_snapshots()

    if not snapshots:
        print("No HTML snapshots found.")
        print(f"  Snapshot directory: {SNAPSHOTS_DIR}")
        return

    print(f"\nAvailable HTML snapshots ({len(snapshots)} total):")
    print("-" * 60)

    for snap in snapshots:
        ts_str = snap["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        size_kb = snap["size"] / 1024
        latest_marker = " <- latest" if snap["is_latest"] else ""
        print(f"  {ts_str}  {size_kb:7.1f} KB  {snap['path'].name}{latest_marker}")

    print("-" * 60)
    print(f"  Snapshot directory: {SNAPSHOTS_DIR}")


def refresh_html_from_browser() -> Path | None:
    """
    Open browser, wait for login, run bookmarklet, and save HTML snapshot.

    Returns:
        Path to the new snapshot, or None if failed
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        print("Error: selenium is required for --refresh-html")
        print("Install with: pip install selenium")
        return None

    # Configuration (same as get_all_chemicals.py)
    URL = "https://www.rug.nl/gai/fwncs/Account/LogOn?ReturnUrl=%2fgaipub%2ffwncs%2fManage%2fChemicals%2fSearch"
    POST_LOGIN_SELECTOR = (By.CSS_SELECTOR, "#btnDeptChemicals")

    BOOKMARKLET_JS = r"""
    (function(){
      document.getElementById("gs_Name").value = "";
      document.getElementById("gs_Formula").value = "";
      document.getElementById("gs_Casnr").value = "";

      const btn = document.getElementById("btnDeptChemicals");
      if (!btn.classList.contains("ui-state-active")) btn.click();

      const option = document.querySelector('td[dir="ltr"] select option[value="15"]');
      option.value = "2500";

      const select = option.parentElement;
      select.value = "2500";
      select.dispatchEvent(new Event('change', { bubbles: true }));
    })();
    """

    print("\n=== HTML Refresh from Browser ===")

    # Chrome is generally easiest for Selenium
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 30)

    try:
        driver.get(URL)

        # Let user log in manually
        print("\nBrowser opened.")
        print("Log in manually (including MFA). When you're fully logged in and on the target page,")
        input("press Enter here to continue... ")

        # Wait for post-login element
        if POST_LOGIN_SELECTOR is not None:
            wait.until(EC.presence_of_element_located(POST_LOGIN_SELECTOR))

        # Run bookmarklet JS
        driver.execute_script(BOOKMARKLET_JS)

        # Wait until the JS changes have taken effect
        wait.until(lambda d: d.execute_script("""
            const sel = document.querySelector('td[dir="ltr"] select');
            return sel && sel.value === "2500";
        """) is True)

        # Wait until table has enough rows
        def table_has_enough_rows(driver):
            return driver.execute_script("""
                const table = document.getElementById("chemList");
                if (!table) return false;
                const rows = table.querySelectorAll('tbody > tr:not(.jqgfirstrow)');
                return rows.length > 20;
            """)

        max_attempts = 3
        for attempt in range(max_attempts):
            print("\nWaiting for the table to have more than 20 rows...", end="", flush=True)
            start = time.time()
            found = False
            while time.time() - start < 60:
                if table_has_enough_rows(driver):
                    found = True
                    print(" success.")
                    break
                print(".", end="", flush=True)
                time.sleep(1)
            if found:
                break
            else:
                print("\nTimeout waiting for table rows (>20).")
                input("Please try reloading/applying again in the browser, then press Enter here to retry...")
        else:
            print("\nFailed to detect table with >20 rows after several attempts.")
            return None

        # Save as timestamped snapshot
        snapshot_path = save_html_snapshot(driver.page_source)
        return snapshot_path

    except Exception as e:
        print(f"Error during browser refresh: {e}")
        return None

    finally:
        time.sleep(1)
        driver.quit()


def start_browser_session() -> str:
    """
    Start a browser session for the two-phase web UI flow.
    Opens Chrome and navigates to the RUG login page, returns immediately.

    Returns:
        Session ID string to use with complete_browser_session()

    Raises:
        ImportError: If selenium is not installed
        Exception: If browser fails to start
    """
    from selenium import webdriver

    # Configuration
    URL = "https://www.rug.nl/gai/fwncs/Account/LogOn?ReturnUrl=%2fgaipub%2ffwncs%2fManage%2fChemicals%2fSearch"

    # Create Chrome driver
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")

    driver = webdriver.Chrome(options=options)
    driver.get(URL)

    # Generate session ID and store driver
    session_id = str(uuid.uuid4())
    _browser_sessions[session_id] = driver

    return session_id


def complete_browser_session(session_id: str) -> Path | None:
    """
    Complete a browser session: wait for login, run bookmarklet, save snapshot.

    Args:
        session_id: The session ID returned by start_browser_session()

    Returns:
        Path to the saved snapshot, or None if failed

    Raises:
        KeyError: If session_id is not found (browser was closed or invalid ID)
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    if session_id not in _browser_sessions:
        raise KeyError(f"Session not found: {session_id}")

    driver = _browser_sessions[session_id]

    # Configuration
    POST_LOGIN_SELECTOR = (By.CSS_SELECTOR, "#btnDeptChemicals")

    BOOKMARKLET_JS = r"""
    (function(){
      document.getElementById("gs_Name").value = "";
      document.getElementById("gs_Formula").value = "";
      document.getElementById("gs_Casnr").value = "";

      const btn = document.getElementById("btnDeptChemicals");
      if (!btn.classList.contains("ui-state-active")) btn.click();

      const option = document.querySelector('td[dir="ltr"] select option[value="15"]');
      option.value = "2500";

      const select = option.parentElement;
      select.value = "2500";
      select.dispatchEvent(new Event('change', { bubbles: true }));
    })();
    """

    try:
        wait = WebDriverWait(driver, 30)

        # Wait for post-login element (user should have logged in by now)
        wait.until(EC.presence_of_element_located(POST_LOGIN_SELECTOR))

        # Run bookmarklet JS
        driver.execute_script(BOOKMARKLET_JS)

        # Wait until the JS changes have taken effect
        wait.until(lambda d: d.execute_script("""
            const sel = document.querySelector('td[dir="ltr"] select');
            return sel && sel.value === "2500";
        """) is True)

        # Wait until table has enough rows
        def table_has_enough_rows(driver):
            return driver.execute_script("""
                const table = document.getElementById("chemList");
                if (!table) return false;
                const rows = table.querySelectorAll('tbody > tr:not(.jqgfirstrow)');
                return rows.length > 20;
            """)

        max_attempts = 3
        for attempt in range(max_attempts):
            print("\nWaiting for the table to have more than 20 rows...", end="", flush=True)
            start = time.time()
            found = False
            while time.time() - start < 60:
                if table_has_enough_rows(driver):
                    found = True
                    print(" success.")
                    break
                print(".", end="", flush=True)
                time.sleep(1)
            if found:
                break
            else:
                print(f"\nTimeout waiting for table rows (>20), attempt {attempt + 1}/{max_attempts}.")
                if attempt < max_attempts - 1:
                    print("Retrying...")
        else:
            print("\nFailed to detect table with >20 rows after several attempts.")
            return None

        # Save as timestamped snapshot
        snapshot_path = save_html_snapshot(driver.page_source)
        return snapshot_path

    except Exception as e:
        print(f"Error during browser session completion: {e}")
        return None

    finally:
        # Clean up: close driver, remove from dict
        time.sleep(1)
        try:
            driver.quit()
        except Exception:
            pass
        _browser_sessions.pop(session_id, None)


def open_pubchem_search(pubchem_results: dict[str, dict], cids_file: Path, force: bool = False) -> bool:
    """
    Open PubChem search in browser.

    Args:
        pubchem_results: PubChem lookup results
        cids_file: Path to saved CIDs file
        force: If True, force open even if cache upload fails

    Returns:
        True if browser was opened, False otherwise
    """
    cids = [str(r["cid"]) for r in pubchem_results.values() if r["cid"]]

    if not cids:
        print("\nNo CIDs found to search.")
        return False

    print(f"\nFound {len(cids)} CIDs for PubChem search")

    # Build direct URL
    cids_str = ",".join(cids)
    direct_url = f"{PUBCHEM_SEARCH_URL}{cids_str}"
    print(f"Direct URL length: {len(direct_url)} characters")

    # If URL is short enough, use it directly
    if len(direct_url) <= MAX_URL_LENGTH:
        print("Opening PubChem search in browser...")
        webbrowser.open(direct_url)
        return True

    # URL too long - upload to PubChem cache and get cache_key
    print(f"URL exceeds limit ({MAX_URL_LENGTH} chars), uploading to PubChem cache...")
    cache_key = upload_cids_to_pubchem_cache(cids)

    if cache_key:
        url = f"https://pubchem.ncbi.nlm.nih.gov/#query={cache_key}"
        print("Opening PubChem search in browser...")
        webbrowser.open(url)
        return True

    # Fallback if cache upload failed
    if force:
        print("Cache upload failed, forcing direct URL anyway...")
        webbrowser.open(direct_url)
        return True

    print(f"\nCould not upload to PubChem cache. CIDs saved to: {cids_file}")
    print("Use --force-browser to try the long URL anyway.")
    return False


def save_rug_table(df: pd.DataFrame, cid_results: dict[str, dict]) -> None:
    """Save the full RUG table with CID column as JSON for later filtering."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    table_df = df.copy()
    table_df["CID"] = table_df["Casnr"].map(
        lambda cas: cid_results.get(cas, {}).get("cid")
    )
    data = {
        "created": datetime.now().isoformat(),
        "columns": list(table_df.columns),
        "rows": table_df.to_dict(orient="records"),
    }
    RUG_TABLE_FILE.write_text(json.dumps(data, indent=2, default=str))
    logger.info("Saved RUG table: %d rows, %d columns", len(table_df), len(table_df.columns))


def load_rug_table() -> dict | None:
    """Load the RUG table from disk."""
    if not RUG_TABLE_FILE.exists():
        return None
    try:
        return json.loads(RUG_TABLE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def fetch_cids_from_listkey(cache_key: str) -> list[int] | None:
    """
    Fetch the actual CID list from a PubChem *cache_key* using the SDQ endpoint.

    This mirrors what the web UI does when you download results for a search
    identified by a cache_key (see network call to sdq/sphinxql.cgi).

    Returns list of CID ints, or None on total failure.
    """
    url = "https://pubchem.ncbi.nlm.nih.gov/sdq/sphinxql.cgi"

    query = {
        "download": "*",
        "collection": "compound",
        "order": ["relevancescore,desc"],
        "start": 1,
        "limit": 10_000_000,
        "downloadfilename": f"PubChem_compound_CID_{cache_key}",
        "where": {
            "ands": [
                {
                    "input": {
                        "type": "netcachekey",
                        "idtype": "cid",
                        "key": cache_key,
                    }
                }
            ]
        },
    }

    params = {
        "infmt": "json",
        "outfmt": "json",
        "query": json.dumps(query),
    }

    logger.info(
        "Starting CID fetch from cache_key %s... via SDQ",
        cache_key[:20] + ("..." if len(cache_key) > 20 else ""),
    )

    def _extract_cids_from_sdq_payload(payload) -> list[int]:
        """
        SDQ can return a few shapes. We try the common ones:
        - dict with "result": [ {"cid": ...}, ... ]
        - dict with "result": [ ["cid"], ["123"], ... ] (table with header)
        - dict with "result": [ ["123", ...], ... ] (table without header)
        - top-level list containing one of the above dicts
        - top-level list of dicts each having a "cid" field
        """
        if isinstance(payload, list):
            # Case 0: top-level list of dict rows with "cid"
            if payload and isinstance(payload[0], dict) and "cid" in payload[0]:
                cids: list[int] = []
                for row in payload:
                    if not isinstance(row, dict):
                        continue
                    cid = row.get("cid")
                    if cid is None:
                        continue
                    try:
                        cids.append(int(cid))
                    except (TypeError, ValueError):
                        continue
                return cids

            # Otherwise: sometimes the response is a list of sections; pick the first dict-like item
            for item in payload:
                if isinstance(item, dict):
                    return _extract_cids_from_sdq_payload(item)
            return []

        if not isinstance(payload, dict):
            return []

        rows = payload.get("result") or payload.get("Result") or []
        if not isinstance(rows, list):
            return []

        cids: list[int] = []
        if not rows:
            return cids

        # Case A: list of dicts
        if isinstance(rows[0], dict):
            for row in rows:
                cid = row.get("cid") if isinstance(row, dict) else None
                if cid is None:
                    continue
                try:
                    cids.append(int(cid))
                except (TypeError, ValueError):
                    continue
            return cids

        # Case B/C: list of lists
        if isinstance(rows[0], list):
            # Header detection: first row contains "cid"
            header = rows[0]
            cid_idx = None
            if all(isinstance(x, str) for x in header):
                for i, col in enumerate(header):
                    if str(col).strip().lower() == "cid":
                        cid_idx = i
                        break

            start_i = 1 if cid_idx is not None else 0
            for row in rows[start_i:]:
                if not isinstance(row, list) or not row:
                    continue
                val = row[cid_idx] if cid_idx is not None and cid_idx < len(row) else row[0]
                try:
                    cids.append(int(val))
                except (TypeError, ValueError):
                    continue
            return cids

        return cids

    try:
        resp = requests.get(url, params=params, timeout=120)

        if resp.status_code != 200:
            body_snippet = ""
            try:
                text = resp.text or ""
                body_snippet = text[:500].replace("\n", " ")
            except Exception:
                body_snippet = "<unable to read response body>"

            logger.error(
                "SDQ fetch HTTP %d for cache_key %s... (url=%s, body_snippet=%r)",
                resp.status_code,
                cache_key[:20],
                resp.url,
                body_snippet,
            )
            return None

        data = resp.json()
        cids = _extract_cids_from_sdq_payload(data)

        if not cids:
            # High-signal debug to understand why we got 0 CIDs for a key that should have results.
            try:
                payload_type = type(data).__name__
                keys = list(data.keys()) if isinstance(data, dict) else None
                result_len = None
                header = None
                if isinstance(data, dict):
                    rows = data.get("result") or data.get("Result") or None
                    if isinstance(rows, list):
                        result_len = len(rows)
                        if rows and isinstance(rows[0], list):
                            header = rows[0][:20]
                logger.warning(
                    "SDQ returned 0 CIDs for cache_key %s... (payload_type=%s, keys=%s, result_len=%s, header=%r, url=%s)",
                    cache_key[:20],
                    payload_type,
                    keys,
                    result_len,
                    header,
                    resp.url,
                )
                snippet = json.dumps(data, ensure_ascii=False)[:2000].replace("\n", " ")
                logger.debug("SDQ payload snippet (first 2000 chars): %s", snippet)
            except Exception:
                logger.debug("Failed to log SDQ debug payload details", exc_info=True)

        logger.info(
            "Fetched %d CIDs from cache_key %s...",
            len(cids),
            cache_key[:20],
        )
        return cids or None

    except Exception as e:
        # Log response shape hints if we got a response
        try:
            body_snippet = (resp.text or "")[:500].replace("\n", " ")
            logger.error(
                "Error fetching CIDs from cache_key %s...: %s (http_status=%s, body_snippet=%r)",
                cache_key[:20],
                e,
                getattr(resp, "status_code", None),
                body_snippet,
            )
        except Exception:
            logger.error("Error fetching CIDs from cache_key %s...: %s", cache_key[:20], e)
        return None


def save_filter_result(search_name: str, operation: str, matching_cids: list[int], pubchem_url: str = "") -> str:
    """Save a filter result and return its short ID."""
    filter_id = str(uuid.uuid4())[:8]
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    results = load_filter_results()
    results.insert(0, {
        "id": filter_id,
        "search_name": search_name,
        "operation": operation,
        "matching_cids": matching_cids,
        "match_count": len(matching_cids),
        "pubchem_url": pubchem_url,
        "created": datetime.now().isoformat(),
    })
    # Keep all saved + last 10 unsaved
    saved = [r for r in results if r.get("saved")]
    unsaved = [r for r in results if not r.get("saved")]
    results = saved + unsaved[:10]
    FILTER_RESULTS_FILE.write_text(json.dumps(results, indent=2))
    logger.info("Saved filter: '%s' (%s) → %d matches", search_name, operation, len(matching_cids))
    return filter_id


def load_filter_results() -> list[dict]:
    """Load saved filter results."""
    if not FILTER_RESULTS_FILE.exists():
        return []
    try:
        return json.loads(FILTER_RESULTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def toggle_saved_filter(filter_id: str, saved: bool) -> bool:
    """Set the saved flag on a filter result. Returns True if found."""
    results = load_filter_results()
    for r in results:
        if r["id"] == filter_id:
            r["saved"] = saved
            FILTER_RESULTS_FILE.write_text(json.dumps(results, indent=2))
            return True
    return False


def delete_filter_result(filter_id: str) -> bool:
    """Remove a filter result entirely. Returns True if found."""
    results = load_filter_results()
    new_results = [r for r in results if r["id"] != filter_id]
    if len(new_results) == len(results):
        return False
    FILTER_RESULTS_FILE.write_text(json.dumps(new_results, indent=2))
    return True


def load_app_searches() -> set[str]:
    """Load app-generated cache keys from disk."""
    if not APP_SEARCHES_FILE.exists():
        return set()
    try:
        data = json.loads(APP_SEARCHES_FILE.read_text())
        return set(data.get("cache_keys", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_app_search(cache_key: str) -> None:
    """Record a cache key as app-generated."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing data to preserve the searches field
    existing = {}
    if APP_SEARCHES_FILE.exists():
        try:
            existing = json.loads(APP_SEARCHES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    app_searches = load_app_searches()
    app_searches.add(cache_key)
    # Keep last 50 to avoid unbounded growth
    app_searches_list = list(app_searches)[-50:]

    data = {
        "version": 2,
        "cache_keys": app_searches_list,
        "searches": existing.get("searches", []),
    }
    APP_SEARCHES_FILE.write_text(json.dumps(data, indent=2))
    logger.info("Recorded app-generated search: %s", cache_key[:20])


def save_app_search_with_metadata(cache_key: str, query: str, count: int) -> None:
    """Save an app-initiated direct search with full metadata.

    Note: Direct searches are NOT added to the cache_keys list because they
    are meant to be combined (unlike combined search results which shouldn't
    be combined again).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing data
    data = {"version": 2, "cache_keys": [], "searches": []}
    if APP_SEARCHES_FILE.exists():
        try:
            existing = json.loads(APP_SEARCHES_FILE.read_text())
            data["cache_keys"] = existing.get("cache_keys", [])
            data["searches"] = existing.get("searches", [])
        except (json.JSONDecodeError, OSError):
            pass

    # Note: We intentionally do NOT add to cache_keys here.
    # cache_keys is for combined searches that shouldn't be combined again.
    # Direct searches should appear in the normal list for combining.

    # Add to searches list with metadata
    search_entry = {
        "cache_key": cache_key,
        "query": query,
        "count": count,
        "timestamp": datetime.now().isoformat(),
        "source": "direct_search",
    }
    data["searches"].append(search_entry)
    # Keep last 50 searches
    data["searches"] = data["searches"][-50:]

    APP_SEARCHES_FILE.write_text(json.dumps(data, indent=2))
    logger.info("Recorded direct search '%s' with %d results: %s", query, count, cache_key[:20])


def load_app_search_metadata() -> dict[str, dict]:
    """Load app search metadata as dict of cache_key -> metadata."""
    if not APP_SEARCHES_FILE.exists():
        return {}
    try:
        data = json.loads(APP_SEARCHES_FILE.read_text())
        searches = data.get("searches", [])
        return {s["cache_key"]: s for s in searches if "cache_key" in s}
    except (json.JSONDecodeError, OSError):
        return {}


def load_stale_searches() -> set[str]:
    """Load blacklisted stale cache keys from disk."""
    if not STALE_SEARCHES_FILE.exists():
        return set()
    try:
        data = json.loads(STALE_SEARCHES_FILE.read_text())
        return set(data.get("cache_keys", []))
    except (json.JSONDecodeError, OSError):
        return set()


def mark_search_as_stale(cache_key: str) -> None:
    """Blacklist a cache key as stale/expired."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stale = load_stale_searches()
    stale.add(cache_key)
    # Keep last 100 to avoid unbounded growth
    stale_list = list(stale)[-100:]
    data = {"version": 1, "cache_keys": stale_list}
    STALE_SEARCHES_FILE.write_text(json.dumps(data, indent=2))
    logger.info("Blacklisted stale search: %s", cache_key[:20])


def load_compound_info() -> dict:
    """Load compound info cache from disk."""
    if not COMPOUND_INFO_FILE.exists():
        return {"version": 1, "compounds": {}}
    try:
        data = json.loads(COMPOUND_INFO_FILE.read_text())
        if "compounds" not in data:
            return {"version": 1, "compounds": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "compounds": {}}


def save_compound_info(data: dict) -> None:
    """Save compound info cache to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COMPOUND_INFO_FILE.write_text(json.dumps(data, indent=2))


async def _fetch_bulk_properties(
    session: aiohttp.ClientSession,
    cids: list[int],
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Fetch properties for a chunk of CIDs via PUG REST (max 200)."""
    url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/property/CanonicalSMILES,MolecularFormula,MolecularWeight,IUPACName,Title/JSON"
    cid_str = ",".join(str(c) for c in cids)
    async with semaphore:
        try:
            async with session.post(
                url,
                data={"cid": cid_str},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("PropertyTable", {}).get("Properties", [])
                else:
                    logger.warning("Bulk properties HTTP %d for %d CIDs", resp.status, len(cids))
                    return []
        except Exception as e:
            logger.warning("Bulk properties error: %s", e)
            return []


async def _fetch_ghs_for_cid(
    session: aiohttp.ClientSession,
    cid: int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[str]]:
    """Fetch GHS pictogram codes for a single CID via PUG View."""
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON/?heading=GHS+Classification"
    async with semaphore:
        try:
            await asyncio.sleep(1.0)  # rate limit: 5 concurrent × 1s = 5 req/sec
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pictograms = _extract_ghs_pictograms(data)
                    return (cid, pictograms)
                logger.debug("GHS fetch HTTP %d for CID %d", resp.status, cid)
                return (cid, [])
        except Exception:
            return (cid, [])


def _extract_ghs_pictograms(data: dict) -> list[str]:
    """Extract GHS pictogram codes from PUG View JSON response."""
    codes = set()
    try:
        sections = data.get("Record", {}).get("Section", [])
        _walk_sections_for_pictograms(sections, codes)
    except Exception:
        pass
    return sorted(codes)


def _walk_sections_for_pictograms(sections: list, codes: set) -> None:
    """Recursively walk PUG View sections to find pictogram URLs."""
    for section in sections:
        if "Section" in section:
            _walk_sections_for_pictograms(section["Section"], codes)
        for info in section.get("Information", []):
            value = info.get("Value", {})
            for markup in value.get("StringWithMarkup", []):
                for m in markup.get("Markup", []):
                    url = m.get("URL", "")
                    # e.g. https://pubchem.ncbi.nlm.nih.gov/images/ghs/GHS07.svg
                    match = re.search(r"(GHS\d{2})", url)
                    if match:
                        codes.add(match.group(1))


async def fetch_compound_properties(
    cids: list[int],
    existing_info: dict,
    progress_cb=None,
) -> dict:
    """
    Fetch compound properties and GHS data from PubChem for the given CIDs.

    Args:
        cids: List of CID integers to fetch
        existing_info: Existing compound info dict (compounds section)
        progress_cb: Optional callback(fetched_count, total_count)

    Returns:
        Dict mapping CID string to compound info dict
    """
    result = dict(existing_info)
    cids_need_props = [c for c in cids if str(c) not in result]
    cids_need_ghs = [c for c in cids if str(c) not in result or "ghs_pictograms" not in result.get(str(c), {})]

    total = len(cids_need_props) + len(cids_need_ghs)
    fetched = 0

    if progress_cb:
        progress_cb(fetched, total)

    semaphore = asyncio.Semaphore(5)

    async with aiohttp.ClientSession() as session:
        # Phase A: Bulk properties in chunks of 200
        for i in range(0, len(cids_need_props), 200):
            chunk = cids_need_props[i : i + 200]
            props_list = await _fetch_bulk_properties(session, chunk, semaphore)
            for prop in props_list:
                cid_key = str(prop.get("CID", ""))
                if not cid_key:
                    continue
                result[cid_key] = {
                    "smiles": prop.get("CanonicalSMILES") or prop.get("ConnectivitySMILES", ""),
                    "formula": prop.get("MolecularFormula", ""),
                    "mw": str(prop.get("MolecularWeight", "")),
                    "iupac": prop.get("IUPACName", ""),
                    "title": prop.get("Title", ""),
                }
            fetched += len(chunk)
            if progress_cb:
                progress_cb(fetched, total)

        # Phase B: GHS pictograms per CID
        tasks = [_fetch_ghs_for_cid(session, cid, semaphore) for cid in cids_need_ghs]
        save_counter = 0
        for coro in asyncio.as_completed(tasks):
            cid, pictograms = await coro
            cid_key = str(cid)
            if cid_key not in result:
                result[cid_key] = {}
            result[cid_key]["ghs_pictograms"] = pictograms
            fetched += 1
            save_counter += 1
            if progress_cb:
                progress_cb(fetched, total)
            # Incremental save every 50 CIDs
            if save_counter >= 50:
                save_counter = 0
                save_compound_info({"version": 1, "compounds": result})

    return result


def main():
    epilog = """\
Examples:
  %(prog)s                        Use cached CIDs from data/latest.html
  %(prog)s Search.html            Process specific HTML file
  %(prog)s --refresh-cids         Force re-lookup all CIDs
  %(prog)s --refresh-html         Fetch fresh HTML via browser (Selenium)
  %(prog)s --refresh              Refresh both HTML and CIDs
  %(prog)s --list-snapshots       Show available HTML snapshots
  %(prog)s --combine AND          Intersect with your latest Firefox PubChem search

Workflow:
  1. HTML snapshots are stored in data/snapshots/ with timestamps
  2. data/latest.html symlinks to the most recent snapshot
  3. CID lookups are cached in data/cid_cache.json (invalidated when HTML changes)
  4. Lookups use: PubChem dump file -> local cache -> CTS API -> PubChem API

Data sources (in order of priority):
  - pubchem_dump_cid_to_cas.tsv   Local TSV dump (~4M CAS mappings, instant)
  - ~/.cache/cas_to_cid/          Persistent cache for API results
  - CTS API                       Chemical Translation Service (batch, fast)
  - PubChem API                   Direct PubChem lookup (rate-limited fallback)
"""

    parser = argparse.ArgumentParser(
        description="Extract chemical data from a jqGrid HTML table, look up CAS numbers "
                    "in PubChem, and open the results in a browser.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Positional argument
    parser.add_argument(
        "html_file",
        type=Path,
        nargs="?",
        default=None,
        metavar="HTML_FILE",
        help="Path to HTML file with chemical table (default: data/latest.html)"
    )

    # Refresh options
    refresh_group = parser.add_argument_group(
        "refresh options",
        "Control when to fetch new data"
    )
    refresh_group.add_argument(
        "--refresh-html",
        action="store_true",
        help="Fetch new HTML snapshot via Selenium browser automation"
    )
    refresh_group.add_argument(
        "--refresh-cids",
        action="store_true",
        help="Force re-lookup CIDs (ignore cache, re-parse HTML)"
    )
    refresh_group.add_argument(
        "--refresh",
        action="store_true",
        help="Shorthand for --refresh-html --refresh-cids"
    )

    # Output options
    output_group = parser.add_argument_group(
        "output options",
        "Control output behavior"
    )
    output_group.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Output directory for CSV/TXT files (default: same as HTML file)"
    )
    output_group.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open PubChem search in browser"
    )
    output_group.add_argument(
        "--skip-pubchem",
        action="store_true",
        help="Skip CID lookups entirely (only extract table and CAS list)"
    )

    # Browser/search options
    browser_group = parser.add_argument_group(
        "browser options",
        "Control PubChem browser search"
    )
    browser_group.add_argument(
        "--force-browser",
        action="store_true",
        help="Open browser even if URL exceeds safe length limit"
    )
    browser_group.add_argument(
        "--combine",
        choices=["AND", "OR", "NOT"],
        metavar="OP",
        help="Combine results with your latest Firefox PubChem search (AND/OR/NOT)"
    )

    # Info options
    info_group = parser.add_argument_group(
        "info options"
    )
    info_group.add_argument(
        "--list-snapshots",
        action="store_true",
        help="List available HTML snapshots and exit"
    )

    args = parser.parse_args()

    # Handle --list-snapshots
    if args.list_snapshots:
        print_snapshots()
        return

    # Handle --refresh flag (shorthand for both)
    if args.refresh:
        args.refresh_html = True
        args.refresh_cids = True

    # Handle --refresh-html first
    if args.refresh_html:
        html_path = refresh_html_from_browser()
        if html_path is None:
            print("Failed to refresh HTML from browser")
            sys.exit(1)
    else:
        # Use provided path or get latest snapshot
        if args.html_file:
            html_path = args.html_file
        else:
            html_path = get_latest_snapshot()
            if not html_path:
                print("Error: No HTML file found.")
                print("Run with --refresh-html to fetch from browser, or provide a path to an HTML file.")
                print(f"\nExample: python3 {sys.argv[0]} Search.html")
                sys.exit(1)

    # Validate input file
    if not html_path.exists():
        print(f"Error: File not found: {html_path}")
        sys.exit(1)

    print(f"Using HTML: {html_path}")
    resolved_path = html_path

    # Set output directory
    output_dir = args.output_dir or html_path.parent

    # Check CID cache (unless --refresh-cids or --skip-pubchem)
    use_cached_cids = False
    cached_results = None

    if not args.skip_pubchem and not args.refresh_cids:
        is_valid, cache_data = is_cid_cache_valid(resolved_path)
        if is_valid and cache_data:
            print(f"Using cached CIDs from {cache_data['created']}")
            stats = cache_data.get("stats", {})
            print(f"  {stats.get('found_cids', '?')} found, {stats.get('not_found', '?')} not found")
            cached_results = cache_data["results"]
            use_cached_cids = True

    # Step 1: Parse HTML table
    df = parse_html_table(resolved_path)

    # Step 2: Extract CAS numbers
    cas_numbers = extract_cas_numbers(df)

    if args.skip_pubchem:
        # Save only table and CAS numbers
        output_dir.mkdir(parents=True, exist_ok=True)

        table_path = output_dir / "chemicals_table.csv"
        df.to_csv(table_path, index=False)
        print(f"Saved full table to: {table_path}")

        cas_path = output_dir / "cas_numbers.txt"
        with open(cas_path, "w") as f:
            f.write("\n".join(cas_numbers))
        print(f"Saved CAS numbers to: {cas_path}")

        print("\nSkipped PubChem lookups (--skip-pubchem flag)")
        return

    # Step 3: Look up CAS numbers (use cache or do fresh lookup)
    if use_cached_cids and cached_results:
        # Filter cached results to only include CAS numbers from current HTML
        pubchem_results = {cas: cached_results[cas] for cas in cas_numbers if cas in cached_results}
        # Check if any CAS numbers are missing from cache
        missing = [cas for cas in cas_numbers if cas not in cached_results]
        if missing:
            print(f"Cache missing {len(missing)} CAS numbers, looking them up...")
            new_results = lookup_cas_to_cid_optimized(missing)
            pubchem_results.update(new_results)
            # Update cache with new results
            all_results = {**cached_results, **new_results}
            save_cid_cache(resolved_path, compute_file_hash(resolved_path), all_results)
    else:
        # Fresh lookup
        pubchem_results = lookup_cas_to_cid_optimized(cas_numbers)
        # Save to CID cache
        save_cid_cache(resolved_path, compute_file_hash(resolved_path), pubchem_results)

    # Step 4: Save outputs
    output_files = save_outputs(df, cas_numbers, pubchem_results, output_dir)

    # Step 5: Open browser (if not disabled)
    if not args.no_browser:
        if args.combine:
            # Combine with latest Firefox PubChem search
            cids = [str(r["cid"]) for r in pubchem_results.values() if r["cid"]]
            if not cids:
                print("\nNo CIDs found to combine.")
            else:
                print(f"\nCombining {len(cids)} CIDs with latest Firefox search ({args.combine})...")

                # Step 5a: Upload program's CIDs to cache
                prog_key = upload_cids_to_pubchem_cache(cids)
                if not prog_key:
                    print("Failed to upload CIDs to PubChem cache")
                else:
                    # Step 5b: Get latest search from Firefox
                    user_key = get_latest_pubchem_history_cachekey()
                    if not user_key:
                        print("Could not get latest search from Firefox. Opening regular search...")
                        open_pubchem_search(pubchem_results, output_files["cids"], force=args.force_browser)
                    else:
                        # Step 5c: Combine the cache keys
                        combine_result = combine_pubchem_cache_keys(user_key, prog_key, args.combine)
                        if combine_result:
                            combined_key, list_size = combine_result
                            url = f"https://pubchem.ncbi.nlm.nih.gov/#query={combined_key}"
                            print(f"Opening combined search in browser...")
                            webbrowser.open(url)
                        else:
                            print("Failed to combine searches. Opening regular search...")
                            open_pubchem_search(pubchem_results, output_files["cids"], force=args.force_browser)
        else:
            open_pubchem_search(pubchem_results, output_files["cids"], force=args.force_browser)
    else:
        print("\nSkipped browser launch (--no-browser flag)")

    print("\nDone!")


if __name__ == "__main__":
    main()
