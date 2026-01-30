#!/usr/bin/env python3
"""
Web UI for Chemical Data Extractor.

A simple Flask app providing a browser-based interface to:
- View and manage HTML snapshots
- Run CID lookups
- View results and open PubChem searches
"""

import os
import sys
from datetime import datetime

# Fix for PyInstaller Windows builds with console=False
# Redirect output to a log file so users can check progress
if sys.stdout is None:
    _log_path = os.path.join(os.path.dirname(sys.executable), "chemical_extractor.log")
    _log_file = open(_log_path, 'a', encoding='utf-8', buffering=1)  # line buffered
    sys.stdout = _log_file
    sys.stderr = _log_file
    print(f"\n{'='*50}")
    print(f"Session started: {datetime.now().isoformat()}")
    print(f"{'='*50}")

APP_VERSION = "1.0.3"

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template_string, jsonify, request, redirect, url_for, Response

# Import functions from the main script
import logging

import requests as _requests

from extract_chemicals import (
    DATA_DIR,
    SNAPSHOTS_DIR,
    CID_CACHE_FILE,
    RUG_TABLE_FILE,
    COMPOUND_INFO_FILE,
    LATEST_POINTER,
    list_snapshots,
    load_cid_cache,
    is_cid_cache_valid,
    compute_file_hash,
    save_cid_cache,
    parse_html_table,
    extract_cas_numbers,
    lookup_cas_to_cid_optimized,
    upload_cids_to_pubchem_cache,
    refresh_html_from_browser,
    start_browser_session,
    complete_browser_session,
    get_latest_pubchem_history_cachekey,
    get_pubchem_history_details,
    combine_pubchem_cache_keys,
    get_latest_snapshot,
    update_latest_pointer,
    get_default_browser,
    get_history_fingerprint,
    save_rug_table,
    load_rug_table,
    fetch_cids_from_listkey,
    save_filter_result,
    load_filter_results,
    toggle_saved_filter,
    delete_filter_result,
    load_compound_info,
    save_compound_info,
    fetch_compound_properties,
    load_app_searches,
    save_app_search,
    save_app_search_with_metadata,
    load_app_search_metadata,
    load_stale_searches,
    mark_search_as_stale,
)

logger = logging.getLogger("chemical_extractor")

app = Flask(__name__)

@app.context_processor
def inject_version():
    return dict(version=APP_VERSION)

# ============================================================================
# Background compound info fetch
# ============================================================================
import asyncio
import threading

_compound_info_status = {"status": "idle", "fetched": 0, "total": 0}
_compound_info_lock = threading.Lock()
_compound_info_force = False

# Repair task state
_repair_status = {"status": "idle", "processed": 0, "total": 0, "current_name": ""}
_repair_lock = threading.Lock()


def _bg_fetch_compound_info():
    """Background thread: fetch compound properties + GHS for all CIDs."""
    global _compound_info_force
    with _compound_info_lock:
        _compound_info_status["status"] = "running"
        _compound_info_status["fetched"] = 0
        _compound_info_status["total"] = 0
        force = _compound_info_force
        _compound_info_force = False

    try:
        existing = {} if force else load_compound_info().get("compounds", {})
        cache = load_cid_cache()
        if not cache or "results" not in cache:
            logger.info("compound-info bg: no CID cache, nothing to do")
            return

        all_cids = [
            int(r["cid"])
            for r in cache["results"].values()
            if r.get("cid") is not None
        ]
        if not all_cids:
            return

        def progress_cb(fetched, total):
            _compound_info_status["fetched"] = fetched
            _compound_info_status["total"] = total

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(
            fetch_compound_properties(all_cids, existing, progress_cb)
        )
        loop.close()

        save_compound_info({"version": 1, "compounds": result})
        logger.info("compound-info bg: done, %d compounds", len(result))
    except Exception:
        logger.exception("compound-info bg: error")
    finally:
        _compound_info_status["status"] = "done"


def start_compound_info_fetch(force=False):
    """Kick off background compound info fetch if not already running."""
    global _compound_info_force
    if _compound_info_status["status"] == "running":
        return
    if force:
        _compound_info_force = True
    t = threading.Thread(target=_bg_fetch_compound_info, daemon=True)
    t.start()


def _bg_repair_unmatched():
    """Background thread: repair unmatched entries via text search (always review mode)."""
    from extract_chemicals import repair_unmatched_entries, load_rug_table, save_cid_cache, load_cid_cache
    import json as _json

    with _repair_lock:
        _repair_status["status"] = "running"
        _repair_status["processed"] = 0
        _repair_status["total"] = 0
        _repair_status["current_name"] = ""

    try:
        def progress_cb(processed, total, current_name):
            with _repair_lock:
                _repair_status["processed"] = processed
                _repair_status["total"] = total
                _repair_status["current_name"] = current_name

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(
            repair_unmatched_entries(progress_cb)
        )
        loop.close()

        logger.info(f"Repair complete: {result}")

        # Mark failed entries on rug_table rows
        failed_indices = result.get("failed_indices", [])
        if failed_indices:
            rug_table = load_rug_table()
            if rug_table:
                rows = rug_table.get("rows", [])
                for ri in failed_indices:
                    if 0 <= ri < len(rows):
                        rows[ri]["_repair_status"] = "failed"
                # Save updated rug_table
                from extract_chemicals import RUG_TABLE_FILE
                RUG_TABLE_FILE.write_text(_json.dumps(rug_table, indent=2, default=str))

        # Always save pending repairs for review
        if result.get("repaired_entries"):
            pending_file = DATA_DIR / "pending_repairs.json"
            with open(pending_file, 'w') as f:
                _json.dump(result, f, indent=2)

    except Exception:
        logger.exception("Repair task error")
    finally:
        with _repair_lock:
            _repair_status["status"] = "done"


def start_repair_task():
    """Start background repair task if not already running."""
    with _repair_lock:
        if _repair_status["status"] == "running":
            return False
        _repair_status["status"] = "running"
        _repair_status["processed"] = 0
        _repair_status["total"] = 0

    t = threading.Thread(target=_bg_repair_unmatched, daemon=True)
    t.start()
    return True


def is_setup_complete():
    """Check if initial setup is complete (has valid CID cache)."""
    cache = load_cid_cache()
    if not cache or "results" not in cache:
        return False
    cids = [r for r in cache["results"].values() if r.get("cid")]
    return len(cids) > 0


# ============================================================================
# HTML Templates (inline for simplicity)
# ============================================================================

BASE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <script>
        (function(){var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t);})();
    </script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - RUG Chemical Search</title>
    <style>
        :root, [data-theme="dark"] {
            --bg: #1a1a2e;
            --bg-light: #16213e;
            --accent: #0f3460;
            --highlight: #e94560;
            --text: #eee;
            --text-dim: #888;
            --success: #4ecca3;
            --warning: #ffc107;
        }
        [data-theme="light"] {
            --bg: #f0f2f5;
            --bg-light: #ffffff;
            --accent: #d8dde6;
            --highlight: #d63031;
            --text: #1a1a2e;
            --text-dim: #636e72;
            --success: #00b894;
            --warning: #e17055;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        header {
            background: var(--bg-light);
            padding: 15px 0;
            margin-bottom: 30px;
            border-bottom: 2px solid var(--accent);
        }
        header .container {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        h1 { font-size: 1.5rem; }
        h1 span { color: var(--highlight); }
        nav { display: flex; align-items: center; gap: 10px; }
        nav a {
            color: var(--text);
            text-decoration: none;
            padding: 8px 16px;
            border-radius: 4px;
            transition: background 0.2s;
        }
        nav a:hover { background: var(--accent); }
        nav a.active { background: var(--highlight); }
        .nav-divider { width: 1px; height: 24px; background: var(--accent); margin: 0 5px; }
        .btn-quit {
            background: transparent;
            border: 1px solid var(--text-dim);
            color: var(--text-dim);
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.2s;
        }
        .btn-quit:hover { border-color: var(--highlight); color: var(--highlight); }

        .card {
            background: var(--bg-light);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .card h2 {
            font-size: 1.1rem;
            margin-bottom: 15px;
            color: var(--highlight);
            border-bottom: 1px solid var(--accent);
            padding-bottom: 10px;
        }

        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat {
            background: var(--accent);
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }
        .stat-value {
            font-size: 2rem;
            font-weight: bold;
            color: var(--success);
        }
        .stat-label { font-size: 0.85rem; color: var(--text-dim); }

        /* Tooltip styles */
        .info {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 16px;
            height: 16px;
            background: var(--accent);
            color: var(--text-dim);
            border-radius: 50%;
            font-size: 11px;
            cursor: help;
            margin-left: 6px;
            position: relative;
            vertical-align: middle;
        }
        .info:hover { color: var(--text); background: var(--highlight); }
        .info .tip {
            display: none;
            position: absolute;
            bottom: calc(100% + 8px);
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg);
            border: 1px solid var(--accent);
            padding: 10px 14px;
            border-radius: 6px;
            font-size: 0.85rem;
            width: 280px;
            text-align: left;
            color: var(--text);
            font-weight: normal;
            line-height: 1.5;
            z-index: 100;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        .info .tip::after {
            content: '';
            position: absolute;
            top: 100%;
            left: 50%;
            transform: translateX(-50%);
            border: 6px solid transparent;
            border-top-color: var(--accent);
        }
        .info:hover .tip { display: block; }
        .card-header {
            display: flex;
            align-items: center;
            font-size: 1.1rem;
            margin-bottom: 15px;
            color: var(--highlight);
            border-bottom: 1px solid var(--accent);
            padding-bottom: 10px;
        }
        .card-header h2 { margin: 0; border: none; padding: 0; }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
        }
        th, td {
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid var(--accent);
        }
        th { color: var(--text-dim); font-weight: 500; }
        #results-data-table tbody tr:nth-child(odd) { background: rgba(128,128,128,0.04); }
        tr:hover { background: var(--accent); }
        tr.selected { background: var(--accent); border-left: 3px solid var(--highlight); }

        .btn {
            display: inline-block;
            padding: 10px 20px;
            background: var(--highlight);
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            text-decoration: none;
            font-size: 0.9rem;
            transition: opacity 0.2s;
        }
        .btn:hover { opacity: 0.9; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-secondary { background: var(--accent); color: var(--text); }
        .btn-success { background: var(--success); color: #000; }
        .btn-large {
            padding: 16px 32px;
            font-size: 1.1rem;
            font-weight: 500;
        }

        .badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 500;
        }
        .badge-success { background: var(--success); color: #000; }
        .badge-warning { background: var(--warning); color: #000; }
        .badge-dim { background: var(--accent); }

        .alert {
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 20px;
        }
        .alert-info { background: var(--accent); border-left: 4px solid var(--highlight); }
        .alert-success { background: rgba(78, 204, 163, 0.2); border-left: 4px solid var(--success); }

        .actions { display: flex; gap: 10px; flex-wrap: wrap; }

        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 2px solid var(--text-dim);
            border-top-color: var(--highlight);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        #results-table { max-height: 400px; overflow-y: auto; }

        .mono { font-family: 'SF Mono', Monaco, monospace; font-size: 0.85rem; }
        .text-dim { color: var(--text-dim); }
        .text-success { color: var(--success); }
        .text-warning { color: var(--warning); }

        select, input[type="file"] {
            background: var(--accent);
            color: var(--text);
            border: 1px solid var(--bg);
            padding: 8px 12px;
            border-radius: 4px;
            font-size: 0.9rem;
        }
        select:focus, input:focus { outline: 2px solid var(--highlight); }

        /* Collapsible sections */
        .collapsible-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            padding: 10px 0;
        }
        .collapsible-header:hover { opacity: 0.8; }
        .collapsible-content { display: none; padding-top: 15px; }
        .collapsible-content.open { display: block; }
        .toggle-icon { font-size: 0.8rem; color: var(--text-dim); }

        /* Search page specific */
        .search-hero {
            padding: 24px;
            background: linear-gradient(135deg, color-mix(in srgb, var(--highlight) 15%, var(--bg-light)) 0%, color-mix(in srgb, var(--highlight) 6%, var(--bg-light)) 100%);
            border: 1.5px solid color-mix(in srgb, var(--highlight) 40%, transparent);
            border-radius: 12px;
            margin-bottom: 25px;
            box-shadow: 0 0 20px color-mix(in srgb, var(--highlight) 15%, transparent), 0 2px 8px rgba(0,0,0,0.2);
        }
        .search-hero h2 {
            font-size: 1.15rem;
            margin-bottom: 15px;
            color: var(--highlight);
            border-bottom: 1px solid color-mix(in srgb, var(--highlight) 30%, transparent);
            padding-bottom: 10px;
        }
        .search-status {
            font-size: 0.9rem;
            color: var(--text-dim);
            margin-top: 15px;
        }

        /* Search input with inline draw button */
        .search-input-wrapper {
            position: relative;
            display: flex;
            flex: 1;
        }
        .search-input-wrapper input {
            width: 100%;
            padding: 14px 80px 14px 16px;
            border-radius: 8px;
            border: 1px solid var(--accent);
            background: var(--bg);
            color: var(--text);
            font-size: 1.05rem;
        }
        .draw-btn {
            position: absolute;
            right: 8px;
            top: 50%;
            transform: translateY(-50%);
            background: transparent;
            border: none;
            opacity: 0.5;
            transition: opacity 0.2s;
            cursor: pointer;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1px;
            padding: 4px 6px;
            color: var(--text);
        }
        .draw-btn:hover { opacity: 0.85; }
        .draw-btn .draw-label { font-size: 0.6rem; }

        /* Mode selector pills */
        .mode-selector {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            margin-top: 10px;
        }
        .mode-pill {
            padding: 6px 14px;
            border: 1px solid var(--accent);
            border-radius: 6px;
            background: transparent;
            cursor: pointer;
            color: var(--text);
            font-size: 0.85rem;
            transition: all 0.2s;
        }
        .mode-pill:hover { border-color: var(--highlight); }
        .mode-pill.active {
            background: var(--highlight);
            color: white;
            border-color: var(--highlight);
        }

        /* Structure drawer modal */
        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal-content {
            background: var(--bg-light);
            border-radius: 8px;
            max-width: 600px;
            width: 90%;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px 20px;
            border-bottom: 1px solid var(--accent);
        }
        .modal-header h3 { margin: 0; }
        .modal-close {
            background: none; border: none;
            font-size: 1.5rem; cursor: pointer;
            color: var(--text-dim);
        }
        .modal-close:hover { color: var(--highlight); }
        .modal-footer {
            padding: 15px 20px;
            border-top: 1px solid var(--accent);
            display: flex;
            gap: 10px;
            justify-content: flex-end;
            align-items: center;
            flex-wrap: wrap;
        }
        #kekule-composer-container {
            padding: 10px;
            background: white;
            min-height: 400px;
        }
    </style>
    <!-- DataTables CSS -->
    <link rel="stylesheet" href="https://cdn.datatables.net/2.2.1/css/dataTables.dataTables.min.css">
    <link rel="stylesheet" href="https://cdn.datatables.net/buttons/3.2.0/css/buttons.dataTables.min.css">
    <link rel="stylesheet" href="https://cdn.datatables.net/colreorder/2.0.4/css/colReorder.dataTables.min.css">
    <!-- DataTables JS -->
    <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
    <script src="https://cdn.datatables.net/2.2.1/js/dataTables.min.js"></script>
    <script src="https://cdn.datatables.net/buttons/3.2.0/js/dataTables.buttons.min.js"></script>
    <script src="https://cdn.datatables.net/buttons/3.2.0/js/buttons.html5.min.js"></script>
    <script src="https://cdn.datatables.net/buttons/3.2.0/js/buttons.colVis.min.js"></script>
    <script src="https://cdn.datatables.net/colreorder/2.0.4/js/dataTables.colReorder.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
    <!-- noUiSlider for range filtering -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/noUiSlider/15.8.1/nouislider.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/noUiSlider/15.8.1/nouislider.min.js"></script>
    <!-- Kekule.js for chemical structure drawing -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/kekule/dist/themes/default/kekule.css">
    <script src="https://cdn.jsdelivr.net/npm/kekule/dist/kekule.min.js"></script>
</head>
<body>
    <header>
        <div class="container">
            <h1 style="display: flex; align-items: center; gap: 8px;">
                <svg id="logo-icon" width="28" height="28" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" style="flex-shrink: 0; cursor: pointer; transition: transform 0.3s, filter 0.3s;" onmouseenter="this.style.transform='rotate(-15deg) scale(1.2)';this.style.filter='drop-shadow(0 0 6px var(--highlight))'" onmouseleave="if(!this.dataset.spinning){this.style.transform='';this.style.filter='';}" onclick="(function(el){el.dataset.spinning='1';el.style.transition='transform 0.8s cubic-bezier(0.2,0.8,0.2,1), filter 0.3s';el.style.transform='rotate(720deg) scale(1)';el.style.filter='drop-shadow(0 0 12px var(--highlight))';var liquid=el.querySelector('#flask-liquid');if(liquid){liquid.style.transition='fill-opacity 0.4s';liquid.style.fillOpacity='0.6';}var bubbles=el.querySelectorAll('.bubble');bubbles.forEach(function(b,i){b.style.transition='opacity 0.3s '+(i*0.15)+'s, transform 0.6s '+(i*0.15)+'s';b.style.opacity='1';b.style.transform='translateY(-6px)';});setTimeout(function(){el.dataset.spinning='';el.style.transition='transform 0.3s, filter 0.3s';el.style.transform='';el.style.filter='';if(liquid){liquid.style.fillOpacity='0';}bubbles.forEach(function(b){b.style.opacity='0';b.style.transform='';});},1000);})(this)">
                    <path d="M12 4V12L6 24C5.2 25.6 6.4 28 8 28H20C21.6 28 22.8 25.6 22 24L16 12V4" stroke="var(--highlight)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    <path id="flask-liquid" d="M8 23C8 23 10 20 14 20C18 20 20 23 20 23L18.5 26C18 27 17 27.5 16 27.5H12C11 27.5 10 27 9.5 26L8 23Z" fill="var(--highlight)" fill-opacity="0"/>
                    <circle class="bubble" cx="11" cy="19" r="1.2" fill="var(--highlight)" opacity="0"/>
                    <circle class="bubble" cx="14" cy="17" r="0.9" fill="var(--highlight)" opacity="0"/>
                    <circle class="bubble" cx="16.5" cy="19.5" r="1" fill="var(--highlight)" opacity="0"/>
                    <line x1="10" y1="4" x2="18" y2="4" stroke="var(--highlight)" stroke-width="2" stroke-linecap="round"/>
                    <path d="M9 20H19" stroke="var(--highlight)" stroke-width="1.5" stroke-linecap="round" opacity="0.5"/>
                    <circle cx="24" cy="12" r="5" stroke="var(--text-dim)" stroke-width="2"/>
                    <line x1="27.5" y1="15.5" x2="30" y2="18" stroke="var(--text-dim)" stroke-width="2" stroke-linecap="round"/>
                </svg>
                RUG Chemical <span>Search</span>
            </h1>
            <nav>
                <a href="{{ url_for('search') }}" class="{{ 'active' if active_page == 'search' else '' }}">Search</a>
                <a href="{{ url_for('results_page') }}" class="{{ 'active' if active_page == 'results' else '' }}">Results</a>
                <a href="{{ url_for('setup') }}" class="{{ 'active' if active_page == 'setup' else '' }}">Setup</a>
                <span class="nav-divider"></span>
                <button class="btn-quit" onclick="document.getElementById('about-modal').style.display='flex'" title="About this app" style="font-size: 1.1rem; font-weight: bold; width: 28px; height: 28px; border-radius: 50%; border: 1.5px solid var(--text-dim); display: inline-flex; align-items: center; justify-content: center; padding: 0;">?</button>
                <button class="btn-quit" onclick="toggleTheme()" id="theme-toggle" title="Toggle light/dark mode" style="font-size: 1.2rem;">☀️</button>
                <button class="btn-quit" onclick="quitApp()">Quit App</button>
            </nav>
        </div>
    </header>
    <main class="container">
        {% block content %}{% endblock %}
    </main>
    <script>
        // Helper for async actions with loading state
        async function runAction(url, btn, options = {}) {
            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> ' + (options.loadingText || 'Processing...');

            try {
                const resp = await fetch(url, { method: 'POST' });
                const data = await resp.json();
                if (data.pubchem_url) {
                    window.open(data.pubchem_url, '_blank');
                } else if (data.redirect) {
                    window.location.href = data.redirect;
                } else if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    window.location.reload();
                }
            } catch (e) {
                alert('Error: ' + e.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = originalText;
            }
        }

        function toggleTheme() {
            const html = document.documentElement;
            const current = html.getAttribute('data-theme') || 'dark';
            const next = current === 'dark' ? 'light' : 'dark';
            html.setAttribute('data-theme', next);
            localStorage.setItem('theme', next);
            updateThemeButton();
        }
        function updateThemeButton() {
            const btn = document.getElementById('theme-toggle');
            if (!btn) return;
            const current = document.documentElement.getAttribute('data-theme') || 'dark';
            btn.textContent = current === 'dark' ? '☀️' : '🌙';
        }
        document.addEventListener('DOMContentLoaded', updateThemeButton);

        async function quitApp() {
            if (confirm('Quit the Chemical Search application?')) {
                try {
                    await fetch('/api/quit', { method: 'POST' });
                } catch (e) {
                    // Expected - server shuts down
                }
                document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#888;"><p>Application closed. You can close this tab.</p></div>';
            }
        }
    </script>
    <script>
    function checkForUpdates() {
        const el = document.getElementById('update-result');
        el.innerHTML = 'Checking...';
        fetch('/api/check-update')
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    el.innerHTML = 'Could not check for updates: ' + data.error;
                } else if (!data.update_available) {
                    el.innerHTML = data.message || '\u2714 You\u2019re on the latest version (v' + data.current + ').';
                } else {
                    let notes = data.release_notes ? '<p style="margin:8px 0;white-space:pre-wrap;max-height:120px;overflow:auto;font-size:0.8rem;background:var(--bg-card);padding:8px;border-radius:4px;">' + data.release_notes.replace(/</g,'&lt;') + '</p>' : '';
                    let action = '';
                    if (data.is_git) {
                        action = '<button onclick="gitPullUpdate(this)" class="btn" style="font-size:0.85rem;padding:6px 14px;margin-top:6px;">Update via git pull</button>' +
                            '<p style="margin-top:8px;font-size:0.8rem;color:var(--text-dim);">This will run <code>git pull</code> to update your local copy. Restart the app afterwards.</p>';
                    } else {
                        action = '<a href="' + data.download_url + '" target="_blank" class="btn" style="font-size:0.85rem;padding:6px 14px;margin-top:6px;display:inline-block;">Download</a>' +
                            '<p style="margin-top:8px;font-size:0.8rem;color:var(--text-dim);">Download the zip, close this app, replace your ChemicalExtractor folder contents with the new files, and relaunch.</p>';
                    }
                    el.innerHTML = '<strong>v' + data.latest + ' is available!</strong> (you have v' + data.current + ')' +
                        notes + action;
                }
            })
            .catch(() => { el.innerHTML = 'Could not check for updates (no internet?).'; });
    }
    function gitPullUpdate(btn) {
        btn.disabled = true;
        btn.textContent = 'Updating...';
        fetch('/api/git-pull', {method:'POST'})
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    btn.textContent = 'Updated!';
                    btn.parentElement.querySelector('p').innerHTML = 'Update complete. <strong>Restart the app</strong> to use the new version.';
                } else {
                    btn.textContent = 'Update failed';
                    btn.parentElement.querySelector('p').textContent = data.error || 'Unknown error';
                }
            })
            .catch(() => { btn.textContent = 'Update failed'; });
    }
    </script>
    {% block scripts %}{% endblock %}

<!-- About modal -->
<div id="about-modal" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); z-index:1000; align-items:center; justify-content:center;" onclick="if(event.target===this)this.style.display='none'">
    <div style="background:var(--bg-light); border-radius:8px; max-width:480px; width:90%; padding:24px; position:relative;">
        <button onclick="document.getElementById('about-modal').style.display='none'" style="position:absolute; top:12px; right:16px; background:none; border:none; font-size:1.4rem; cursor:pointer; color:var(--text-dim);">&times;</button>
        <h2 style="margin-top:0;">About RUG Chemical Search</h2>
        <p style="color:var(--text-dim); font-size:0.85rem; margin-bottom:12px;">Version <strong>v{{ version }}</strong></p>
        <p style="color:var(--text-dim); line-height:1.6; font-size:0.9rem;">
            This app lets you cross-reference your lab's chemical inventory with
            <a href="https://pubchem.ncbi.nlm.nih.gov/" target="_blank" style="color:var(--link);">PubChem</a>.
            Search by name, CAS number, structure, or keyword to find which of your chemicals match,
            and explore their properties.
        </p>
        <p style="color:var(--text-dim); line-height:1.6; font-size:0.9rem;">
            You can also search PubChem directly in your browser &mdash; the app detects those searches
            automatically and makes them available for combining with your inventory.
        </p>
        <hr style="border:none; border-top:1px solid var(--accent); margin:16px 0;">
        <div id="update-section" style="margin-bottom:12px;">
            <button onclick="checkForUpdates()" class="btn" style="font-size:0.85rem; padding:6px 14px;">Check for updates</button>
            <div id="update-result" style="margin-top:10px; font-size:0.85rem; color:var(--text-dim);"></div>
        </div>
        <hr style="border:none; border-top:1px solid var(--accent); margin:16px 0;">
        <p style="color:var(--text-dim); font-size:0.85rem; margin-bottom:0;">
            Bug reports &amp; feature requests:
            <a href="https://github.com/mf-rug/rug_chemsearch_cl/issues" target="_blank" style="color:var(--link);">GitHub Issues</a>
            or email <a href="mailto:m.j.l.j.furst@rug.nl" style="color:var(--link);">Max</a>.
        </p>
    </div>
</div>
</body>
</html>
"""

SEARCH_TEMPLATE = """
{% extends "base" %}
{% block content %}
{% if not has_cids %}
<div class="alert alert-info">
    <p><strong>Setup Required</strong></p>
    <p style="margin-top: 8px;">Before you can search, you need to import your chemicals database and look them up in PubChem.</p>
    <a href="{{ url_for('setup') }}" class="btn" style="margin-top: 15px;">Go to Setup</a>
</div>
{% else %}

<div class="search-hero">
    <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 4px;">
        <h2 style="margin: 0;">Search Your {{ cid_count }} Chemicals</h2>
        <a href="{{ url_for('results_page', filter_id='all') }}" style="color: var(--text-dim); font-size: 0.8rem; text-decoration: underline; opacity: 0.8;">View All</a>
    </div>
    <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 10px;">
        <div class="search-input-wrapper">
            <input type="text" id="direct-search-input" placeholder="Enter name, CAS, or keyword..."
                   onkeydown="if(event.key==='Enter'){directPubchemSearch(document.getElementById('direct-search-btn'), false);}">
            <button class="draw-btn" onclick="openStructureDrawer()" title="Draw structure">
                <svg width="20" height="22" viewBox="0 0 20 22" fill="none" stroke="currentColor" stroke-width="1.5">
                    <path d="M10 1L18.66 5.5V14.5L10 19L1.34 14.5V5.5L10 1Z"/>
                </svg>
                <span class="draw-label">Draw</span>
            </button>
        </div>
        <button class="btn btn-success" id="direct-search-btn" onclick="directPubchemSearch(this, false)">Search</button>
        <input type="checkbox" id="exclude-toggle" style="display:none;">
        <button class="mode-pill" id="exclude-pill" onclick="let c=document.getElementById('exclude-toggle');c.checked=!c.checked;this.classList.toggle('active',c.checked);">Exclude</button>
    </div>
    <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
        <div class="mode-selector" style="margin-bottom: 0;">
            <button class="mode-pill active" data-mode="name" onclick="selectMode('name', this)">Name / CAS</button>
            <button class="mode-pill" data-mode="smiles" onclick="selectMode('smiles', this)">SMILES</button>
            <button class="mode-pill" data-mode="substructure" onclick="selectMode('substructure', this)">Substructure</button>
            <button class="mode-pill" data-mode="superstructure" onclick="selectMode('superstructure', this)">Superstructure</button>
            <button class="mode-pill" data-mode="similarity" onclick="selectMode('similarity', this)">Similarity</button>
        </div>
        <a href="#" onclick="directPubchemSearch(document.getElementById('direct-search-btn'), true); return false;" style="font-size: 0.8rem; color: var(--text-dim); text-decoration: underline;">Add to history only</a>
    </div>
    <p class="text-dim" style="font-size: 0.85rem; margin-top: 10px;" id="search-mode-hint">
        Search by name, CAS number, or keyword. Results will be combined with your lab chemicals.
    </p>
</div>

<div class="card">
    <div class="collapsible-header" onclick="toggleSection('history-section')">
        <h2 style="margin: 0; border: none; padding: 0;">Search History</h2>
        <span class="toggle-icon" id="history-section-icon">+ expand</span>
    </div>
    <div class="collapsible-content" id="history-section">
        <p class="text-dim" style="font-size: 0.85rem; margin-bottom: 12px;">
            Searches you make above appear here, along with any PubChem searches you perform directly in your browser.
            You can also <a href="#" onclick="window.open('https://pubchem.ncbi.nlm.nih.gov/', '_blank'); return false;" style="color: var(--link);">open PubChem</a> and search there — results will show up here automatically.
        </p>

        <div style="display: flex; justify-content: flex-end; align-items: center; gap: 10px; margin-bottom: 10px;">
            <span class="text-dim" style="font-size: 0.8rem;" id="auto-refresh-status"></span>
            <button class="btn btn-secondary" style="padding: 5px 12px; font-size: 0.8rem;" onclick="refreshHistory()">
                Refresh
            </button>
        </div>

        <div id="browser-warning" class="text-warning" style="display: none; margin-bottom: 15px; padding: 10px; background: rgba(255,193,7,0.15); border-radius: 6px; font-size: 0.9rem;"></div>

        <div id="history-container" style="max-height: 350px; overflow-y: auto; margin-bottom: 20px;">
            <table id="history-table">
                <thead>
                    <tr>
                        <th style="width: 30px;"></th>
                        <th>Search Query</th>
                        <th>Browser</th>
                        <th>When</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody id="history-body">
                    <tr><td colspan="5" class="text-dim" style="text-align: center; padding: 20px;">
                        <span class="loading" style="width: 14px; height: 14px;"></span> Loading...
                    </td></tr>
                </tbody>
            </table>
        </div>

        <div class="actions" style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <button class="btn btn-success" onclick="combineSelectedSearch('AND', this)" disabled id="btn-combine-and">
                Find in My Chemicals (AND)
            </button>
            <button class="btn btn-secondary" onclick="combineSelectedSearch('NOT', this)" disabled id="btn-combine-not">
                Exclude from My Chemicals (NOT)
            </button>
            <span style="margin-left: auto; display: flex; align-items: center; gap: 6px;">
                <span id="toggle-app-searches" onclick="toggleAppSearches()" style="cursor: pointer; font-size: 0.8rem; padding: 4px 10px; border-radius: 12px; border: 1px solid var(--accent); color: var(--text-dim); user-select: none;">App searches</span>
                <span class="info">i
                    <span class="tip">App-generated searches are created when you combine your chemicals with a PubChem search. They usually aren't useful to combine again.</span>
                </span>
            </span>
        </div>
        <p class="text-dim" style="margin-top: 15px; font-size: 0.85rem;">
            <strong>AND:</strong> Which of my chemicals match this search? &nbsp;
            <strong>NOT:</strong> Which of my chemicals do NOT match this search?
        </p>
    </div>
</div>

<!-- Structure Drawer Modal -->
<div id="structure-drawer-modal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            <h3>Draw Structure</h3>
            <button class="modal-close" onclick="closeStructureDrawer()">&times;</button>
        </div>
        <div id="kekule-composer-container"></div>
        <div class="modal-footer">
            <select id="structure-search-type" style="padding: 8px 12px; border-radius: 4px; border: 1px solid var(--accent); background: var(--bg); color: var(--text); font-size: 0.9rem;">
                <option value="substructure">Substructure search</option>
                <option value="superstructure">Superstructure search</option>
                <option value="similarity">Similarity search</option>
                <option value="smiles">Exact match</option>
            </select>
            <button class="btn btn-success" onclick="useDrawnStructure()">Use Structure</button>
            <button class="btn btn-secondary" onclick="closeStructureDrawer()">Cancel</button>
        </div>
    </div>
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
let selectedCacheKey = null;
let searchHistory = [];
let allHistory = [];  // Stores both filtered and unfiltered
let autoRefreshInterval = null;

function getShowAppSearches() {
    try {
        const stored = localStorage.getItem('show_app_searches');
        return stored === 'true';
    } catch(e) { return false; }
}

function saveShowAppSearches(show) {
    localStorage.setItem('show_app_searches', show ? 'true' : 'false');
}

function toggleAppSearches() {
    const toggle = document.getElementById('toggle-app-searches');
    const wasActive = toggle.dataset.active === '1';
    const show = !wasActive;
    toggle.dataset.active = show ? '1' : '0';
    toggle.style.background = show ? 'var(--highlight)' : 'transparent';
    toggle.style.color = show ? '#fff' : 'var(--text-dim)';
    saveShowAppSearches(show);

    // Filter and re-render
    searchHistory = show ? allHistory : allHistory.filter(entry => !entry._is_app_search);
    renderHistoryTable();
}

async function loadHistory() {
    const tbody = document.getElementById('history-body');
    if (!tbody) return;

    try {
        const resp = await fetch('/api/pubchem-history');
        const data = await resp.json();

        // Store both lists
        allHistory = data.all_history || [];
        const filteredHistory = data.history || [];

        // Mark app searches
        const filteredKeys = new Set(filteredHistory.map(e => e.cachekey));
        allHistory.forEach(entry => {
            entry._is_app_search = !filteredKeys.has(entry.cachekey);
        });

        // Browser warning
        const warningEl = document.getElementById('browser-warning');
        if (data.browser_warning && warningEl) {
            warningEl.textContent = data.browser_warning;
            warningEl.style.display = 'block';
        } else if (warningEl) {
            warningEl.style.display = 'none';
        }

        // Apply user preference
        const showAppSearches = getShowAppSearches();
        const toggle = document.getElementById('toggle-app-searches');
        if (toggle) {
            toggle.dataset.active = showAppSearches ? '1' : '0';
            toggle.style.background = showAppSearches ? 'var(--highlight)' : 'transparent';
            toggle.style.color = showAppSearches ? '#fff' : 'var(--text-dim)';
        }

        searchHistory = showAppSearches ? allHistory : filteredHistory;

        renderHistoryTable();
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-warning" style="text-align: center;">
            Error loading search history: ${e.message}
        </td></tr>`;
    }
}

function renderHistoryTable() {
    const tbody = document.getElementById('history-body');
    if (!tbody) return;

    if (searchHistory.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-dim" style="text-align: center; padding: 30px;">
            No recent PubChem searches found.<br><br>
            <button class="btn" onclick="window.open('https://pubchem.ncbi.nlm.nih.gov/', '_blank')">
                Search on PubChem
            </button>
        </td></tr>`;
        updateButtons();
        return;
    }

    tbody.innerHTML = searchHistory.map((entry, idx) => `
        <tr onclick="selectEntry('${entry.cachekey}')" data-cachekey="${entry.cachekey}"
            style="cursor: pointer;" class="${idx === 0 ? 'selected' : ''}">
            <td style="text-align: center;">
                <input type="radio" name="search-select" value="${entry.cachekey}"
                       ${idx === 0 ? 'checked' : ''}
                       onclick="event.stopPropagation(); selectEntry('${entry.cachekey}')">
            </td>
            <td style="max-width: 350px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                title="${entry.name}">
                ${entry.name}${entry._is_app_search ? ' <span class="badge badge-dim">APP</span>' : ''}
            </td>
            <td>${entry.browser || '?'}</td>
            <td class="text-dim" style="font-size: 0.85rem;">${formatTime(entry.timestamp)}</td>
            <td style="white-space: nowrap;">
                <a href="${entry.url}" target="_blank" onclick="event.stopPropagation();"
                   style="color: var(--highlight); font-size: 0.85rem;">PubChem</a>
            </td>
        </tr>
    `).join('');

    // Select first entry by default
    if (searchHistory.length > 0) {
        selectedCacheKey = searchHistory[0].cachekey;
    }
    updateButtons();
}

function formatTime(isoTimestamp) {
    if (!isoTimestamp) return 'Unknown';
    const date = new Date(isoTimestamp);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
}

function selectEntry(cachekey) {
    selectedCacheKey = cachekey;

    document.querySelectorAll('#history-body tr').forEach(row => {
        const isSelected = row.dataset.cachekey === cachekey;
        row.classList.toggle('selected', isSelected);
        const radio = row.querySelector('input[type="radio"]');
        if (radio) radio.checked = isSelected;
    });

    updateButtons();
}

function updateButtons() {
    const hasSelection = selectedCacheKey !== null;
    const btnAnd = document.getElementById('btn-combine-and');
    const btnNot = document.getElementById('btn-combine-not');
    const btnMain = document.getElementById('btn-main-search');
    if (btnAnd) btnAnd.disabled = !hasSelection;
    if (btnNot) btnNot.disabled = !hasSelection;
    if (btnMain) btnMain.disabled = !hasSelection;
}

async function combineSelectedSearch(operation, btn) {
    if (!selectedCacheKey) {
        alert('Please select a search from the list first.');
        return;
    }

    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="loading"></span> Searching...';

    try {
        const resp = await fetch('/api/combine-pubchem/' + operation +
                                 '?cachekey=' + encodeURIComponent(selectedCacheKey),
                                 { method: 'POST' });
        const data = await resp.json();

        if (data.error === 'stale_search') {
            // Handle stale search specially
            alert(data.message || 'This search has expired and is no longer available.');

            // Mark as stale server-side (blacklist it)
            await fetch('/api/mark-stale-search', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({cache_key: data.cache_key})
            });

            // Refresh history to remove it
            await refreshHistory();
        } else if (data.error) {
            alert('Error: ' + data.error);
        } else if (data.filter_id) {
            window.location.href = '/results?filter_id=' + data.filter_id;
            return;
        }
    } catch (e) {
        alert('Error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
        updateButtons();
    }
}

async function refreshHistory() {
    const tbody = document.getElementById('history-body');
    if (tbody) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-dim" style="text-align: center; padding: 20px;">
            <span class="loading" style="width: 14px; height: 14px;"></span> Refreshing...
        </td></tr>`;
    }
    const previousKey = selectedCacheKey;
    selectedCacheKey = null;
    await loadHistory();
    // Try to re-select previous selection
    if (previousKey && searchHistory.find(h => h.cachekey === previousKey)) {
        selectEntry(previousKey);
    }
}

function toggleSection(id) {
    const content = document.getElementById(id);
    const icon = document.getElementById(id + '-icon');
    if (content.classList.contains('open')) {
        content.classList.remove('open');
        icon.textContent = '+ expand';
    } else {
        content.classList.add('open');
        icon.textContent = '- collapse';
    }
}

// --- Direct PubChem search ---
const SEARCH_MODE_INFO = {
    'name': {placeholder: 'Enter name, CAS, or keyword...', hint: 'Search by name, CAS number, or keyword. Results will be combined with your lab chemicals.'},
    'smiles': {placeholder: 'Enter SMILES or draw...', hint: 'Find exact compound match by SMILES notation.'},
    'substructure': {placeholder: 'Enter SMILES or draw...', hint: 'Find compounds containing this structure as a substructure.'},
    'superstructure': {placeholder: 'Enter SMILES or draw...', hint: 'Find compounds where query is a superstructure (contains the target).'},
    'similarity': {placeholder: 'Enter SMILES or draw...', hint: 'Find compounds with similar 2D structure (Tanimoto similarity).'},
};

let currentSearchMode = 'name';

function selectMode(mode, pill) {
    currentSearchMode = mode;
    document.querySelectorAll('.mode-pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    updateSearchPlaceholder();
}

function updateSearchPlaceholder() {
    const mode = currentSearchMode;
    const input = document.getElementById('direct-search-input');
    const hint = document.getElementById('search-mode-hint');
    const info = SEARCH_MODE_INFO[mode] || SEARCH_MODE_INFO['name'];
    input.placeholder = info.placeholder;
    hint.textContent = info.hint;
}

async function directPubchemSearch(btn, historyOnly) {
    const input = document.getElementById('direct-search-input');
    const query = input.value.trim();
    const mode = currentSearchMode;

    if (!query) {
        alert('Enter a search query');
        return;
    }

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.innerHTML = '<span class="loading"></span> Searching...';

    try {
        const resp = await fetch('/api/pubchem-search', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({query, mode})
        });
        const data = await resp.json();

        if (data.success) {
            if (historyOnly) {
                input.value = '';
                await refreshHistory();
                selectEntry(data.cache_key);
            } else {
                // Combine and navigate to results
                btn.innerHTML = '<span class="loading"></span> Combining...';
                const excludeOn = document.getElementById('exclude-toggle').checked;
                const operation = excludeOn ? 'NOT' : 'AND';
                const combResp = await fetch('/api/combine-pubchem/' + operation +
                    '?cachekey=' + encodeURIComponent(data.cache_key),
                    { method: 'POST' });
                const combData = await combResp.json();

                if (combData.error === 'stale_search') {
                    alert(combData.message || 'This search has expired.');
                    await fetch('/api/mark-stale-search', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({cache_key: combData.cache_key})
                    });
                    input.value = '';
                    await refreshHistory();
                } else if (combData.error) {
                    alert('Error: ' + combData.error);
                    // Fall back to showing in history
                    input.value = '';
                    await refreshHistory();
                    selectEntry(data.cache_key);
                } else if (combData.filter_id) {
                    input.value = '';
                    window.location.href = '/results?filter_id=' + combData.filter_id;
                    return;
                }
            }
        } else {
            alert(data.error || 'Search failed');
        }
    } catch (e) {
        alert('Error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// --- Smart auto-refresh: poll file fingerprint every 50s, full fetch only on change ---
let lastFingerprint = null;
const POLL_INTERVAL_MS = 50000;

async function pollForChanges() {
    try {
        const resp = await fetch('/api/pubchem-history/check');
        const data = await resp.json();
        const fp = data.fingerprint;
        if (lastFingerprint !== null && fp !== lastFingerprint) {
            // Storage files changed — do a full refresh
            const statusEl = document.getElementById('auto-refresh-status');
            if (statusEl) statusEl.textContent = 'Updating...';
            await refreshHistory();
        }
        lastFingerprint = fp;
    } catch (e) {
        // Network error — ignore, will retry next tick
    }
    const statusEl = document.getElementById('auto-refresh-status');
    if (statusEl) statusEl.textContent = 'Watching for changes';
}

function startAutoRefresh() {
    autoRefreshInterval = setInterval(pollForChanges, POLL_INTERVAL_MS);
}

document.addEventListener('DOMContentLoaded', () => {
    loadHistory();
    // Capture initial fingerprint, then start polling
    pollForChanges().then(() => startAutoRefresh());
});

// Cleanup on page leave
window.addEventListener('beforeunload', () => {
    if (autoRefreshInterval) clearInterval(autoRefreshInterval);
});

// --- Structure Drawer (Kekule.js) ---
let composer = null;

function openStructureDrawer() {
    const modal = document.getElementById('structure-drawer-modal');
    modal.style.display = 'flex';

    // Initialize composer if not already done
    if (!composer) {
        const container = document.getElementById('kekule-composer-container');
        composer = new Kekule.Editor.Composer(container);
        composer.setDimension('100%', '400px');
    }
}

function closeStructureDrawer() {
    document.getElementById('structure-drawer-modal').style.display = 'none';
}

function useDrawnStructure() {
    if (!composer) return;

    const mol = composer.getChemObj();
    if (!mol) {
        alert('Please draw a structure first');
        return;
    }

    let smiles;
    try {
        smiles = Kekule.IO.saveFormatData(mol, 'smi');
    } catch (e) {
        alert('Could not convert structure to SMILES: ' + e.message);
        return;
    }

    if (!smiles) {
        alert('Could not convert structure to SMILES');
        return;
    }

    // Populate the search input
    document.getElementById('direct-search-input').value = smiles;

    // Set the search mode based on drawer dropdown selection
    const searchType = document.getElementById('structure-search-type').value;
    currentSearchMode = searchType;
    document.querySelectorAll('.mode-pill').forEach(p => {
        p.classList.toggle('active', p.dataset.mode === searchType);
    });
    updateSearchPlaceholder();

    closeStructureDrawer();
}

// Close modal on escape key or clicking outside
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeStructureDrawer();
});

document.addEventListener('click', (e) => {
    const modal = document.getElementById('structure-drawer-modal');
    if (e.target === modal) closeStructureDrawer();
});
</script>
{% endblock %}
"""

GHS_NAMES = {
    "GHS01": "Explosive",
    "GHS02": "Flammable",
    "GHS03": "Oxidizer",
    "GHS04": "Compressed Gas",
    "GHS05": "Corrosive",
    "GHS06": "Toxic",
    "GHS07": "Irritant",
    "GHS08": "Health Hazard",
    "GHS09": "Environment",
}

# Default columns for the results table (order matters)
DEFAULT_RESULTS_COLUMNS = [
    "Structure", "Name", "CAS", "Formula", "MW", "Hazards", "Location",
]

# All available columns (default + hidden RUG originals)
ALL_RESULTS_COLUMNS = DEFAULT_RESULTS_COLUMNS + [
    "GROSname", "Pot", "Owner", "OwnerRegNumber", "IUPAC", "SMILES", "CID",
]

RESULTS_TEMPLATE = """
{% extends "base" %}
{% block content %}
<style>
    .structure-cell { position: relative; }
    .structure-thumb { width: 50px; height: 50px; background: white; border-radius: 2px; }
    .structure-large { display: none; position: absolute; z-index: 100; width: 250px; height: 250px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); background: white; border-radius: 4px; top: 0; left: 0; }
    .structure-cell:hover .structure-large { display: block; }
    .structure-cell:hover .structure-thumb { visibility: hidden; }
    .ghs-icon { width: 24px; height: 24px; margin-right: 2px; vertical-align: middle; background: white; }
    .smiles-cell { font-family: 'SF Mono', Monaco, monospace; font-size: 0.75rem; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .col-picker { background: var(--bg-light); border: 1px solid var(--accent); border-radius: 6px; padding: 10px 15px; margin-bottom: 15px; display: none; }
    .col-picker.open { display: flex; flex-wrap: wrap; gap: 8px 16px; }
    .col-picker label { font-size: 0.85rem; cursor: pointer; display: flex; align-items: center; gap: 4px; }

    /* DataTables dark theme customization */
    div.dt-container div.dt-layout-row { margin-top: 0.2em; margin-bottom: 0; }
    .dataTables_wrapper { color: var(--text); }
    .dataTables_wrapper .dataTables_length,
    .dataTables_wrapper .dataTables_filter,
    .dataTables_wrapper .dataTables_info,
    .dataTables_wrapper .dataTables_paginate { color: var(--text-dim); }

    .dataTables_wrapper input[type="search"] {
        background: var(--accent);
        color: var(--text);
        border: 1px solid var(--bg);
        padding: 4px 8px;
        border-radius: 4px;
    }

    .dataTables_wrapper .dataTables_paginate .paginate_button {
        background: var(--accent) !important;
        color: var(--text) !important;
        border: 1px solid var(--bg) !important;
    }

    .dataTables_wrapper .dataTables_paginate .paginate_button.current {
        background: var(--highlight) !important;
    }

    .dt-toolbar {
        display: flex !important;
        flex-wrap: wrap;
        align-items: center;
        gap: 12px;
        margin-bottom: 12px;
        width: 100%;
    }
    .dt-toolbar .dt-buttons { margin-bottom: 0; }
    .dt-toolbar .dataTables_length { margin: 0; }
    .dt-toolbar .dataTables_filter { margin: 0; margin-left: auto; flex: 1; }
    .dt-toolbar .dataTables_filter label { display: flex; align-items: center; gap: 6px; }
    .dt-toolbar .dataTables_filter input { flex: 1; }

    .dt-buttons {
        display: flex;
        gap: 5px;
    }

    .dt-button {
        background: var(--accent) !important;
        color: var(--text) !important;
        border: 1px solid var(--bg) !important;
        padding: 6px 12px !important;
        border-radius: 4px !important;
        font-size: 0.85rem !important;
    }

    .dt-button:hover {
        background: var(--highlight) !important;
    }

    .table-responsive {
        overflow-x: auto;
        width: 100%;
    }

    .print-header { display: none; }

    #mw-slider { margin: 0 8px; }
    #mw-slider .noUi-connect { background: #8c8c8c; }
    #mw-slider .noUi-handle { border-color: var(--highlight); background: var(--card-bg); }
    #mw-slider .noUi-tooltip { background: var(--card-bg); color: var(--text); border-color: var(--border); font-size: 0.75rem; }
    #mw-slider .noUi-pips { color: var(--text-dim); }
    #mw-slider .noUi-marker { background: var(--border); }
    #mw-slider .noUi-value { color: var(--text-dim); font-size: 0.7rem; }
    #mw-slider .noUi-pips { top: 100%; padding-top: 2px; }

    @media print {
        @page {
            size: landscape;
            margin: 0.5cm;
        }

        body {
            font-size: 10pt;
        }

        /* Hide page header/footer elements */
        nav, .dt-buttons, #advanced-filters, .dataTables_length, .dataTables_filter,
        .dataTables_info, .dataTables_paginate, .btn, .btn-secondary, select,
        label.text-dim, #compound-info-badge, .alert, .advanced-filters, .filter-info { display: none !important; }

        .print-header { display: block !important; }
        body, .card { background: white !important; color: black !important; }
        .structure-large { display: none !important; }
        .structure-cell:hover .structure-large { display: none !important; }
        .structure-cell:hover .structure-thumb { visibility: visible !important; }
        tr { page-break-inside: avoid; }
        * { color: black !important; border-color: #ccc !important; }

        /* Default: normal size for tables with few columns */
        .table-responsive {
            overflow: visible !important;
        }

        #results-data-table th,
        #results-data-table td {
            padding: 4px 6px !important;
            white-space: nowrap;
        }

        /* Scale down only when many columns are visible */
        .table-responsive.print-scale-many {
            width: fit-content !important;
            max-width: none !important;
            transform-origin: top left;
            transform: scale(0.6);
        }

        .print-scale-many #results-data-table {
            font-size: 8pt;
        }

        .print-scale-many #results-data-table th,
        .print-scale-many #results-data-table td {
            padding: 2px 3px !important;
        }
    }
</style>

{% if not rug_table %}
<div class="alert alert-info">
    <p><strong>RUG table not loaded.</strong> Complete Setup first to import your chemicals and look them up in PubChem.</p>
    <a href="{{ url_for('setup') }}" class="btn" style="margin-top: 10px;">Go to Setup</a>
</div>
{% elif not current_filter %}
<div class="alert alert-info">
    <p><strong>No results yet.</strong> Use the Search tab to combine a PubChem search with your chemicals.</p>
    <a href="{{ url_for('search') }}" class="btn" style="margin-top: 10px;">Go to Search</a>
</div>
{% else %}
<div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
        <div>
            <h2 style="color: var(--highlight); margin-bottom: 5px;">{{ current_filter.search_name }}</h2>
            <p class="text-dim" style="font-size: 0.85rem;">
                Operation: <strong>{{ current_filter.operation }}</strong> &bull;
                <span class="text-success">{{ current_filter.match_count }} matches</span> &bull;
                {{ current_filter.created[:19] }}
            </p>
        </div>
        <div style="display: flex; gap: 10px; align-items: center;">
            <span id="compound-info-badge" class="text-dim" style="font-size: 0.8rem;"></span>
            {% if current_filter.id and current_filter.id != 'all' %}
            <button id="save-toggle-btn" class="btn btn-secondary" style="font-size: 0.85rem;" onclick="toggleSave()">
                {% if current_filter.get('saved') %}&#9733; Saved{% else %}&#9734; Save{% endif %}
            </button>
            {% endif %}
            {% if current_filter.pubchem_url %}
            <a href="{{ current_filter.pubchem_url }}" target="_blank" class="btn btn-secondary" style="font-size: 0.85rem;">Open on PubChem</a>
            {% endif %}
        </div>
    </div>

    <div class="print-header">
        <h2>Chemical Report: {{ current_filter.search_name }}</h2>
        <p>{{ current_filter.match_count }} chemicals | {{ current_filter.created[:10] if current_filter.created else '' }}</p>
        <hr>
    </div>

    <div class="card" style="margin-bottom: 5px; background: var(--accent); padding: 15px 10px 5px 15px;">
    {% if filter_results|length > 0 %}
    <div style="margin-bottom: 15px;">
        <label class="text-dim" style="font-size: 1rem;">Search: </label>
        <select onchange="if(this.value) window.location.href='/results?filter_id='+this.value;" style="font-size: 1rem; background: var(--highlight); margin: 5px; width: 80%;">
            <option value="all" {{ 'selected' if current_filter and current_filter.id == 'all' else '' }}>
                All Chemicals ({{ rug_table.rows|length if rug_table else 0 }} total)
            </option>
            {% set saved_filters = filter_results|selectattr('saved', 'defined')|selectattr('saved')|list %}
            {% set recent_filters = filter_results|rejectattr('saved', 'defined')|list + filter_results|selectattr('saved', 'defined')|rejectattr('saved')|list %}
            {% if saved_filters %}
            <optgroup label="Saved">
                {% for fr in saved_filters %}
                <option value="{{ fr.id }}" {{ 'selected' if current_filter and fr.id == current_filter.id else '' }}>
                    &#9733; {{ fr.search_name }} ({{ fr.operation }}) — {{ fr.match_count }} matches
                </option>
                {% endfor %}
            </optgroup>
            {% endif %}
            {% if recent_filters %}
            <optgroup label="Recent">
                {% for fr in recent_filters %}
                <option value="{{ fr.id }}" {{ 'selected' if current_filter and fr.id == current_filter.id else '' }}>
                    {{ fr.search_name }} ({{ fr.operation }}) — {{ fr.match_count }} matches
                </option>
                {% endfor %}
            </optgroup>
            {% endif %}
        </select>
    </div>
    {% endif %}

    <div id="advanced-filters" style="padding: 12px 15px;">
        <div style="display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start;">
            <div style="min-width: 280px; flex: 1;">
                <label class="text-dim" style="font-size: 0.8rem; display: block; margin-bottom: 8px; text-align: center;">Molecular Weight</label>
                <div id="mw-slider" style="margin-bottom: 20px;"></div>
            </div>
            <div>
                <label class="text-dim" style="font-size: 0.8rem; display: block; margin-bottom: 4px;">Formula</label>
                <input type="text" id="formula-filter" placeholder="e.g. C6" style="width: 100px; font-size: 0.85rem; padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text);">
            </div>
            {% if unique_locations %}
            <div>
                <label class="text-dim" style="font-size: 0.8rem; display: block; margin-bottom: 4px;">Location</label>
                <select id="location-filter" style="font-size: 0.85rem; padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text);">
                    <option value="">All locations</option>
                    {% for loc in unique_locations %}
                    <option value="{{ loc }}">{{ loc }}</option>
                    {% endfor %}
                </select>
            </div>
            {% endif %}
            <div style="display: flex; gap: 6px; align-self: center;">
                <button class="btn" style="font-size: 0.8rem;" id="apply-filters-btn">Apply Filters</button>
                <button class="btn btn-secondary" style="font-size: 0.8rem; background: var(--bg-light);" id="clear-filters-btn">Clear</button>
            </div>
        </div>
    </div>
    </div>

    {% if filtered_rows %}
    {% macro render_cell(row, col, grouped=false) %}
        {% if col == 'Structure' %}
        <div class="structure-cell">
            {% if row._cid_int %}
            <img class="structure-thumb" src="https://pubchem.ncbi.nlm.nih.gov/image/imgsrv.fcgi?cid={{ row._cid_int }}&amp;t=s" alt="structure" loading="lazy">
            <img class="structure-large" src="https://pubchem.ncbi.nlm.nih.gov/image/imgsrv.fcgi?cid={{ row._cid_int }}&amp;t=l" alt="structure" loading="lazy">
            {% else %}-{% endif %}
        </div>
        {% elif col == 'CID' %}
            {% if row._cid_int %}
            <a href="https://pubchem.ncbi.nlm.nih.gov/compound/{{ row._cid_int }}" target="_blank" style="color: var(--success);" class="mono">{{ row._cid_int }}</a>
            {% else %}-{% endif %}
        {% elif col == 'Name' %}
            {{ row._ci.get('title', '') or '-' }}{% if row._repair_status == 'repaired' %} <span class="badge badge-warning" title="Found via text search (repaired)" style="margin-left: 4px; font-size: 0.7rem;">&#128295;</span>{% endif %}{% if grouped and row._group_count > 1 %} <span class="badge" style="background: var(--accent); color: var(--bg); font-size: 0.7rem; padding: 1px 5px; border-radius: 8px; margin-left: 4px;">&times;{{ row._group_count }}</span>{% endif %}
        {% elif col == 'GROSname' %}
            {% if grouped and col in row._group_varying %}
                <span class="vary-pop text-dim" style="font-size:0.75rem;cursor:pointer;text-decoration:underline dotted;" title="Click to view">{{ row._group_varying[col]|length }} values</span><span class="vary-pop-content" style="display:none;">{{ row._group_varying[col]|join(', ') }}</span>
            {% else %}{{ row.get('Name', '') or '-' }}{% endif %}
        {% elif col == 'CAS' %}
            {% if row._cid_int %}
                {% if row.get('_original_cas') %}
                    <s>{{ row._original_cas }}</s> <a href="https://pubchem.ncbi.nlm.nih.gov/compound/{{ row._cid_int }}" target="_blank" style="color: var(--success);">{{ row.get('Casnr', '') }}</a>
                {% else %}
                    <a href="https://pubchem.ncbi.nlm.nih.gov/compound/{{ row._cid_int }}" target="_blank" style="color: var(--success);">{{ row.get('Casnr', '') }}</a>
                {% endif %}
            {% else %}
                {% if row.get('_original_cas') %}<s>{{ row._original_cas }}</s> {{ row.get('Casnr', '') }}{% else %}{{ row.get('Casnr', '') or '-' }}{% endif %}
            {% endif %}
        {% elif col == 'Formula' %}
            {% set formula = row._ci.get('formula', '') or row.get('Formula', '') or '-' %}
            {% if formula != '-' %}
                <span data-plaintext="{{ formula }}">{{ formula|replace('0', '<sub>0</sub>')|replace('1', '<sub>1</sub>')|replace('2', '<sub>2</sub>')|replace('3', '<sub>3</sub>')|replace('4', '<sub>4</sub>')|replace('5', '<sub>5</sub>')|replace('6', '<sub>6</sub>')|replace('7', '<sub>7</sub>')|replace('8', '<sub>8</sub>')|replace('9', '<sub>9</sub>')|safe }}
                </span>
            {% else %}
                -
            {% endif %}
        {% elif col == 'MW' %}{{ row._ci.get('mw', '') or '-' }}
        {% elif col == 'SMILES' %}<span class="smiles-cell" title="{{ row._ci.get('smiles', '') }}">{{ row._ci.get('smiles', '') or '-' }}</span>
        {% elif col == 'IUPAC' %}{{ row._ci.get('iupac', '') or '-' }}
        {% elif col == 'Hazards' %}
            {% for code in row._ci.get('ghs_pictograms', []) %}
            <img class="ghs-icon" src="https://pubchem.ncbi.nlm.nih.gov/images/ghs/{{ code }}.svg" title="{{ ghs_names.get(code, code) }}" alt="{{ code }}" loading="lazy">
            {% endfor %}
            {% if not row._ci.get('ghs_pictograms') %}-{% endif %}
        {% elif col == 'Location' %}
            {% if grouped and col in row._group_varying %}
                <span class="vary-pop text-dim" style="font-size:0.75rem;cursor:pointer;text-decoration:underline dotted;" title="Click to view">{{ row._group_varying[col]|length }} values</span><span class="vary-pop-content" style="display:none;">{{ row._group_varying[col]|join(', ') }}</span>
            {% else %}{{ row.get('Location', '') or '-' }}{% endif %}
        {% else %}
            {% if grouped and col in row.get('_group_varying', {}) %}
                <span class="vary-pop text-dim" style="font-size:0.75rem;cursor:pointer;text-decoration:underline dotted;" title="Click to view">{{ row._group_varying[col]|length }} values</span><span class="vary-pop-content" style="display:none;">{{ row._group_varying[col]|join(', ') }}</span>
            {% else %}{{ row.get(col, '') or '-' }}{% endif %}
        {% endif %}
    {% endmacro %}

    <div class="table-responsive" id="individual-table-wrapper">
        <table id="results-data-table" class="display" style="width:100%">
            <thead>
                <tr>
                    {% for col in all_columns %}
                    <th>{{ col }}</th>
                    {% endfor %}
                </tr>
            </thead>
            <tbody>
                {% for row in filtered_rows %}
                <tr>
                    {% for col in all_columns %}
                    <td data-column="{{ col }}">{{ render_cell(row, col) }}</td>
                    {% endfor %}
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

    <div class="table-responsive" id="grouped-table-wrapper">
        <table id="results-grouped-table" class="display" style="width:100%">
            <thead>
                <tr>
                    {% for col in all_columns %}
                    <th>{{ col }}</th>
                    {% endfor %}
                </tr>
            </thead>
            <tbody>
                {% for row in grouped_rows %}
                <tr>
                    {% for col in all_columns %}
                    <td data-column="{{ col }}">{{ render_cell(row, col, grouped=true) }}</td>
                    {% endfor %}
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-dim" style="padding: 20px; text-align: center;">No matching chemicals found in your RUG table for this search.</p>
    {% endif %}
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
const DEFAULT_COLS = {{ default_columns|tojson }};
const ALL_COLS = {{ all_columns|tojson }};
const GHS_NAMES = {{ ghs_names|tojson }};

$(document).ready(function() {
    // Load visible columns from localStorage
    let visibleCols = DEFAULT_COLS;
    try {
        const stored = localStorage.getItem('results_visible_cols');
        if (stored) visibleCols = JSON.parse(stored);
    } catch(e) {}

    // Build column definitions
    function buildColumnDefs() {
        return ALL_COLS.map((col, idx) => {
            let def = {
                targets: idx,
                title: col,
                visible: visibleCols.includes(col),
                className: 'col-' + col
            };

            if (col === 'Structure') {
                def.orderable = false;
                def.searchable = false;
                def.exportOptions = { format: { body: function() { return ''; } } };
            } else if (col === 'Hazards') {
                def.orderable = false;
                def.exportOptions = {
                    format: {
                        body: function(data) {
                            const matches = data.match(/alt="(GHS\d+)"/g);
                            return matches ? matches.map(m => m.match(/GHS\d+/)[0]).join(', ') : '';
                        }
                    }
                };
            } else if (col === 'CAS') {
                def.exportOptions = {
                    format: {
                        body: function(data) {
                            const linkMatch = data.match(/>([^<]+)<\/a>/);
                            if (linkMatch) return linkMatch[1];
                            const plainMatch = data.match(/<\/s>\s*([^<]+)/);
                            if (plainMatch) return plainMatch[1];
                            return data.replace(/<[^>]*>/g, '').trim() || '';
                        }
                    }
                };
            } else if (col === 'Formula') {
                def.exportOptions = {
                    format: {
                        body: function(data) {
                            const match = data.match(/data-plaintext="([^"]*)"/);
                            return match ? match[1] : data.replace(/<[^>]*>/g, '').trim();
                        }
                    }
                };
            } else if (col === 'CID') {
                def.type = 'num';
                def.exportOptions = {
                    format: {
                        body: function(data) {
                            const match = data.match(/>\s*(\d+)\s*</);
                            return match ? match[1] : '';
                        }
                    }
                };
            } else if (col === 'MW') {
                def.type = 'num';
            } else if (col === 'SMILES') {
                def.render = function(data, type) {
                    if (type === 'display' && data.length > 50) {
                        return '<span class="smiles-cell" title="' + data + '">' + data.substring(0, 50) + '...</span>';
                    }
                    return data;
                };
            }

            return def;
        });
    }

    function buildDTConfig(tableId) {
        return {
            pageLength: 25,
            lengthMenu: [[10, 25, 50, 100, -1], [10, 25, 50, 100, "All"]],
            order: [[ALL_COLS.indexOf('Name'), 'asc']],
            columnDefs: buildColumnDefs(),
            layout: {
                topStart: ['buttons', 'pageLength'],
                topEnd: 'search'
            },
            buttons: [
                {
                    extend: 'colvis',
                    text: 'Columns',
                    columns: ':not(.no-export)'
                },
                {
                    extend: 'collection',
                    text: 'Export',
                    buttons: [
                        {
                            extend: 'csvHtml5',
                            text: 'CSV',
                            title: '{{ current_filter.search_name if current_filter else "Results" }} - {{ current_filter.created[:10] if current_filter else "" }}',
                            exportOptions: { columns: ':visible:not(.col-Structure)' }
                        },
                        {
                            extend: 'excelHtml5',
                            text: 'Excel',
                            title: '{{ current_filter.search_name if current_filter else "Results" }} - {{ current_filter.created[:10] if current_filter else "" }}',
                            exportOptions: { columns: ':visible:not(.col-Structure)' }
                        },
                        {
                            extend: 'copyHtml5',
                            text: 'Copy to Clipboard',
                            exportOptions: { columns: ':visible:not(.col-Structure)' }
                        },
                        {
                            text: 'Print / PDF',
                            action: function(e, dt) {
                                const visibleCount = dt.columns(':visible').count();
                                const $tableWrapper = $('.table-responsive:visible');
                                if (visibleCount > 7) {
                                    $tableWrapper.addClass('print-scale-many');
                                }
                                dt.page.len(-1).draw();
                                setTimeout(function() {
                                    window.print();
                                    setTimeout(function() {
                                        $tableWrapper.removeClass('print-scale-many');
                                    }, 1000);
                                }, 500);
                            }
                        }
                    ]
                }
            ],
            stateSave: true,
            stateSaveCallback: function(settings, data) {
                const visible = [];
                settings.aoColumns.forEach((col, idx) => {
                    if (col.bVisible) visible.push(ALL_COLS[idx]);
                });
                visibleCols = visible;
                localStorage.setItem('results_visible_cols', JSON.stringify(visible));
                localStorage.setItem('DataTables_' + tableId, JSON.stringify(data));
            },
            stateLoadCallback: function() {
                try {
                    return JSON.parse(localStorage.getItem('DataTables_' + tableId));
                } catch(e) {
                    return null;
                }
            },
            language: {
                search: "Search chemicals:",
                lengthMenu: "Show _MENU_ rows",
                info: "Showing _START_ to _END_ of _TOTAL_ chemicals",
                infoEmpty: "No chemicals found",
                infoFiltered: "(filtered from _MAX_ total)",
                zeroRecords: "No matching chemicals found"
            },
            initComplete: function() {
                var $btnGroup = $(this.api().table().container()).find('.dt-buttons');
                var active = localStorage.getItem('results_group_identical') !== 'false';
                var $pill = $('<span id="group-toggle" style="cursor:pointer;font-size:0.8rem;padding:4px 10px;border-radius:12px;border:1px solid var(--accent);user-select:none;margin-left:8px;align-content:center;"></span>').text('Group identical');
                function applyStyle() {
                    if (active) {
                        $pill.css({background:'var(--highlight)',color:'#fff'});
                    } else {
                        $pill.css({background:'transparent',color:'var(--text-dim)'});
                    }
                }
                applyStyle();
                $pill.on('click', function() {
                    grouped = !grouped;
                    active = grouped;
                    localStorage.setItem('results_group_identical', grouped ? 'true' : 'false');
                    if (activeTable) { activeTable.destroy(); activeTable = null; }
                    initActiveTable();
                });
                $btnGroup.append($pill);
            }
        };
    }

    // Group toggle logic
    var grouped = localStorage.getItem('results_group_identical') !== 'false';
    var activeTable = null;

    function initActiveTable() {
        if (grouped) {
            $('#individual-table-wrapper').hide();
            $('#grouped-table-wrapper').show();
            if ($.fn.dataTable.isDataTable('#results-data-table')) {
                $('#results-data-table').DataTable().destroy();
            }
            activeTable = $('#results-grouped-table').DataTable(buildDTConfig('results-grouped'));
            activeTable.on('column-visibility.dt', function(e, settings) {
                var visible = [];
                settings.aoColumns.forEach(function(col, idx) {
                    if (col.bVisible) visible.push(ALL_COLS[idx]);
                });
                visibleCols = visible;
                localStorage.setItem('results_visible_cols', JSON.stringify(visible));
            });
        } else {
            $('#grouped-table-wrapper').hide();
            $('#individual-table-wrapper').show();
            if ($.fn.dataTable.isDataTable('#results-grouped-table')) {
                $('#results-grouped-table').DataTable().destroy();
            }
            activeTable = $('#results-data-table').DataTable(buildDTConfig('results'));
            activeTable.on('column-visibility.dt', function(e, settings) {
                var visible = [];
                settings.aoColumns.forEach(function(col, idx) {
                    if (col.bVisible) visible.push(ALL_COLS[idx]);
                });
                visibleCols = visible;
                localStorage.setItem('results_visible_cols', JSON.stringify(visible));
            });
        }
        window._activeTable = activeTable;
    }

    initActiveTable();

    $(document).on('click', '.vary-pop', function(e) {
        $('.vary-popover').remove();
        var content = $(this).next('.vary-pop-content').text();
        var $pop = $('<div class="vary-popover"></div>').text(content)
            .css({position:'absolute', background:'var(--bg-card)', border:'1px solid var(--border)',
                  borderRadius:'6px', padding:'8px 12px', fontSize:'0.8rem', maxWidth:'300px',
                  zIndex:1000, boxShadow:'0 2px 8px rgba(0,0,0,0.15)', wordBreak:'break-word'});
        $('body').append($pop);
        var rect = this.getBoundingClientRect();
        $pop.css({top: rect.bottom + window.scrollY + 4, left: rect.left + window.scrollX});
    });
    $(document).on('click', function(e) {
        if (!$(e.target).hasClass('vary-pop')) $('.vary-popover').remove();
    });

});

// Keep compound info polling
async function pollCompoundInfo() {
    const badge = document.getElementById('compound-info-badge');
    if (!badge) return;
    try {
        const resp = await fetch('/api/compound-info-status');
        const data = await resp.json();
        if (data.status === 'running') {
            badge.innerHTML = '<span class="loading" style="width:12px;height:12px;"></span> Fetching compound info ' + data.progress;
            setTimeout(pollCompoundInfo, 3000);
        } else if (data.status === 'done') {
            badge.textContent = '';
        }
    } catch(e) {}
}
document.addEventListener('DOMContentLoaded', pollCompoundInfo);

// --- Save/Unsave toggle ---
console.log('[Script] toggleSave and filter code starting to parse');
function toggleSave() {
    const filterId = '{{ current_filter.id if current_filter else "" }}';
    if (!filterId || filterId === 'all') return;
    const isSaved = {{ 'true' if current_filter and current_filter.get('saved') else 'false' }};
    const action = isSaved ? 'unsave' : 'save';
    fetch('/api/filter-results/' + filterId + '/' + action, { method: 'POST' })
        .then(r => r.json())
        .then(data => { if (data.success) location.reload(); });
}

// --- Advanced property filters ---
console.log('[Filters] Script tag loaded');
console.log('[Filters] jQuery?', typeof $);
console.log('[Filters] noUiSlider?', typeof noUiSlider);
console.log('[Filters] DataTable?', typeof $.fn.dataTable);

window._filterState = { mwLo: null, mwHi: null, formula: '', location: '' };
window._fIdx = { mw: -1, formula: -1, loc: -1 };
window._mwSlider = null;

$(document).ready(function() {
    if (!window._activeTable) { console.log('[Filters] No results table found, skipping'); return; }

    var tbl = window._activeTable;
    var tbl = window._activeTable;
    var headers = [];
    tbl.columns().header().each(function(h) { headers.push($(h).text().trim()); });
    window._fIdx.mw = headers.indexOf('MW');
    window._fIdx.formula = headers.indexOf('Formula');
    window._fIdx.loc = headers.indexOf('Location');
    console.log('[Filters] Column indices — MW:', window._fIdx.mw, 'Formula:', window._fIdx.formula, 'Location:', window._fIdx.loc);

    // --- noUiSlider for MW ---
    var sliderEl = document.getElementById('mw-slider');
    if (sliderEl && window._fIdx.mw >= 0) {
        var dataMin = Infinity, dataMax = -Infinity;
        tbl.column(window._fIdx.mw).data().each(function(val) {
            var n = parseFloat(val);
            if (!isNaN(n)) {
                if (n < dataMin) dataMin = n;
                if (n > dataMax) dataMax = n;
            }
        });
        if (!isFinite(dataMin)) { dataMin = 0; dataMax = 1000; }
        dataMin = Math.floor(dataMin);
        dataMax = Math.ceil(dataMax);
        window._mwDataMin = dataMin;
        window._mwDataMax = dataMax;
        window._filterState.mwLo = dataMin;
        window._filterState.mwHi = dataMax;

        noUiSlider.create(sliderEl, {
            start: [dataMin, dataMax],
            connect: true,
            range: { 'min': dataMin, 'max': dataMax },
            step: 1,
            tooltips: [
                { to: function(v) { return Math.round(v); } },
                { to: function(v) { return Math.round(v); } }
            ],
            pips: {
                mode: 'count',
                values: 5,
                density: 4,
                format: { to: function(v) { return Math.round(v); } }
            }
        });
        window._mwSlider = sliderEl.noUiSlider;
        console.log('[Filters] MW slider created, range:', dataMin, '-', dataMax);
    } else {
        console.log('[Filters] MW slider skipped — element:', !!sliderEl, 'mwIdx:', window._fIdx.mw);
    }

    // --- Register DataTables custom search function ---
    $.fn.dataTable.ext.search.push(function(settings, data) {
        if (settings.nTable.id !== 'results-data-table' && settings.nTable.id !== 'results-grouped-table') return true;
        var fs = window._filterState;
        var idx = window._fIdx;

        if (idx.mw >= 0 && fs.mwLo !== null && fs.mwHi !== null) {
            if (fs.mwLo > window._mwDataMin || fs.mwHi < window._mwDataMax) {
                var mw = parseFloat(data[idx.mw]);
                if (isNaN(mw) || mw < fs.mwLo || mw > fs.mwHi) return false;
            }
        }
        if (idx.formula >= 0 && fs.formula) {
            const cellData = data[idx.formula];
            const plaintext = cellData && cellData.match(/data-plaintext="([^"]*)"/) ? cellData.match(/data-plaintext="([^"]*)"/)[1] : cellData;
            if ((plaintext || '').toLowerCase().indexOf(fs.formula) === -1) return false;
        }
        if (idx.loc >= 0 && fs.location) {
            if ((data[idx.loc] || '').trim() !== fs.location) return false;
        }
        return true;
    });
    console.log('[Filters] Custom search function registered');

    // --- Button handlers ---
    $('#apply-filters-btn').on('click', function() {
        console.log('[Filters] Apply clicked');
        if (window._mwSlider) {
            var vals = window._mwSlider.get();
            window._filterState.mwLo = parseFloat(vals[0]);
            window._filterState.mwHi = parseFloat(vals[1]);
            console.log('[Filters] MW range:', window._filterState.mwLo, '-', window._filterState.mwHi);
        }
        window._filterState.formula = ($('#formula-filter').val() || '').toLowerCase();
        window._filterState.location = $('#location-filter').val() || '';
        console.log('[Filters] Formula:', window._filterState.formula, 'Location:', window._filterState.location);
        if (window._activeTable) window._activeTable.draw();
        console.log('[Filters] Table redrawn');
    });

    $('#clear-filters-btn').on('click', function() {
        console.log('[Filters] Clear clicked');
        if (window._mwSlider) {
            window._mwSlider.set([window._mwDataMin, window._mwDataMax]);
        }
        $('#formula-filter').val('');
        $('#location-filter').val('');
        window._filterState = { mwLo: window._mwDataMin, mwHi: window._mwDataMax, formula: '', location: '' };
        if (window._activeTable) window._activeTable.draw();
        console.log('[Filters] Filters cleared, table redrawn');
    });

    console.log('[Filters] Button handlers bound');
});
</script>
{% endblock %}
"""

SETUP_TEMPLATE = """
{% extends "base" %}
{% block content %}
<div class="stats">
    <div class="stat">
        <div class="stat-value">{{ snapshot_count }}</div>
        <div class="stat-label">Database Exports</div>
    </div>
    <div class="stat">
        <div class="stat-value">{{ cache_stats.found_cids if cache_stats else '-' }}</div>
        <div class="stat-label">Matched in PubChem</div>
    </div>
    <div class="stat">
        <div class="stat-value">{{ no_match_count if cache_stats else '-' }}</div>
        <div class="stat-label">No PubChem Match</div>
    </div>
    <div class="stat">
        <div class="stat-value">{{ cache_stats.total_cas if cache_stats else '-' }}</div>
        <div class="stat-label">Total Chemicals</div>
    </div>
</div>

{% if setup_complete %}
<div class="alert alert-success">
    <p><strong>Setup Complete!</strong> You have {{ cache_stats.found_cids }} chemicals ready to search.
    {% if no_match_count > 0 %} {{ no_match_count }} entries had no PubChem match &mdash; you can try to <a href="#no-match" onclick="document.getElementById('no-match').classList.add('open'); document.getElementById('no-match-icon').textContent='- collapse';" style="color: inherit; text-decoration: underline;">repair them</a> below.{% endif %}</p>
    <a href="{{ url_for('search') }}" class="btn btn-success" style="margin-top: 10px;">Go to Search</a>
</div>
{% elif latest_snapshot and not cache_valid %}
<div class="alert" style="background: var(--accent); border: 1px solid var(--warning); border-radius: 6px; padding: 15px; margin-bottom: 20px;">
    <p><strong>Next step:</strong> Database imported. Now look up your chemicals in PubChem (Step 2 below).</p>
</div>
{% endif %}

<div class="card">
    <h2>Quick Import</h2>
    <p style="margin-bottom: 15px;">Import a pre-built database bundle. Skips Steps 1 &amp; 2.</p>
    <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 15px;">
        <input type="text" id="import-url" placeholder="Paste Google Drive or direct URL..."
               style="flex: 1; padding: 8px 12px; border-radius: 4px; border: 1px solid var(--accent); background: var(--bg); color: var(--text); font-size: 0.9rem;">
        <button class="btn" onclick="importFromUrl(this)">Import from URL</button>
    </div>
    <div style="display: flex; gap: 10px; align-items: center;">
        <span class="text-dim">Or import from file:</span>
        <input type="file" accept=".json" onchange="importFromFile(this)" style="font-size: 0.85rem;">
    </div>
    <p class="text-dim" style="margin-top: 10px; font-size: 0.85rem;">Or set up manually below (Steps 1 &amp; 2).</p>
    <div id="import-status" style="margin-top: 10px; display: none;">
        <p><span class="loading"></span> <span id="import-status-text">Importing...</span></p>
    </div>
</div>

<div class="card">
    <h2>Step 1: Import Chemicals Database</h2>
    {% if latest_snapshot %}
    <p><strong>Current database:</strong> <span class="mono">{{ latest_snapshot.name }}</span></p>
    <p class="text-dim">{{ latest_snapshot.timestamp.strftime('%Y-%m-%d %H:%M:%S') }} &bull; {{ (latest_snapshot.size / 1024)|round(1) }} KB</p>
    {% else %}
    <p class="text-warning">No database imported yet.</p>
    {% endif %}

    <div class="actions" style="margin-top: 15px;" id="rug-actions">
        <button class="btn" id="btn-open-login" onclick="openRugLogin(this)">
            Fetch from RUG System
        </button>
        <button class="btn btn-success" id="btn-continue" onclick="continueAfterLogin(this)" style="display: none;">
            Continue
        </button>
    </div>
    <p class="text-dim" style="margin-top: 10px; font-size: 0.85rem;" id="rug-instructions">
        Opens Chrome for you to log in. After login, click Continue to fetch all chemicals.
    </p>
    <div id="browser-refresh-status" style="margin-top: 15px; display: none;">
        <p><span class="loading" id="status-spinner"></span> <span id="refresh-status-text">Starting browser...</span></p>
    </div>
</div>

<div class="card">
    <h2>Step 2: Look Up in PubChem</h2>
    {% if not latest_snapshot %}
    <p class="text-dim">Import a database first (Step 1)</p>
    {% elif cache_valid %}
    <p class="text-success">PubChem lookups are cached and ready.</p>
    <p class="text-dim" style="margin-top: 5px;">Last lookup: {{ cache_created }}</p>
    <div class="actions" style="margin-top: 15px;">
        <button class="btn btn-secondary" onclick="runAction('{{ url_for('run_extraction') }}?refresh_cids=1', this, {loadingText: 'Looking up...'})">
            Re-lookup All (refresh cache)
        </button>
    </div>
    {% else %}
    <p class="text-warning">PubChem lookups needed.</p>
    <div class="actions" style="margin-top: 15px;">
        <button class="btn" onclick="runAction('{{ url_for('run_extraction') }}', this, {loadingText: 'Looking up (this may take a while)...'})">
            Look Up in PubChem
        </button>
    </div>
    {% endif %}

    <div id="compound-info-status" style="margin-top: 15px; display: none;">
        <p style="font-size: 0.85rem;">
            <span class="loading" style="width: 14px; height: 14px;" id="ci-spinner"></span>
            <span id="ci-status-text"></span>
        </p>
    </div>
</div>

<!-- Collapsible: Manage Exports -->
<div class="card">
    <div class="collapsible-header" onclick="toggleSection('manage-exports')">
        <h2 style="margin: 0; border: none; padding: 0;">Manage Exports</h2>
        <span class="toggle-icon" id="manage-exports-icon">+ expand</span>
    </div>
    <div class="collapsible-content" id="manage-exports">
        {% if setup_complete %}
        <div style="margin-bottom: 20px;">
            <h3 style="font-size: 0.95rem; color: var(--text-dim); margin-bottom: 10px;">Export Database Bundle</h3>
            <p class="text-dim" style="margin-bottom: 10px; font-size: 0.85rem;">Download a bundle containing all data. Share with others to skip Steps 1 &amp; 2.</p>
            <form action="{{ url_for('export_database') }}" method="post">
                <button type="submit" class="btn btn-secondary">Export Database</button>
            </form>
        </div>
        {% endif %}
        <div style="margin-bottom: 20px;">
            <h3 style="font-size: 0.95rem; color: var(--text-dim); margin-bottom: 10px;">Upload HTML File</h3>
            <form action="{{ url_for('upload_snapshot') }}" method="post" enctype="multipart/form-data" style="display: flex; gap: 10px; align-items: center;">
                <input type="file" name="file" accept=".html,.htm" required>
                <button type="submit" class="btn btn-secondary">Upload</button>
            </form>
        </div>

        {% if snapshots %}
        <table>
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Size</th>
                    <th>Status</th>
                    <th></th>
                </tr>
            </thead>
            <tbody>
            {% for snap in snapshots %}
                <tr>
                    <td>{{ snap.timestamp.strftime('%Y-%m-%d %H:%M') }}</td>
                    <td>{{ (snap.size / 1024)|round(1) }} KB</td>
                    <td>
                        {% if snap.is_latest %}
                        <span class="badge badge-success">Active</span>
                        {% else %}
                        <span class="badge badge-dim">Archived</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if not snap.is_latest %}
                        <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 0.75rem;"
                                onclick="runAction('{{ url_for('set_latest', filename=snap.path.name) }}', this)">
                            Use This
                        </button>
                        {% endif %}
                    </td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p class="text-dim">No exports yet.</p>
        {% endif %}
    </div>
</div>

<!-- Collapsible: View Results -->
<div class="card">
    <div class="collapsible-header" onclick="toggleSection('view-results')">
        <h2 style="margin: 0; border: none; padding: 0;">View PubChem Results</h2>
        <span class="toggle-icon" id="view-results-icon">+ expand</span>
    </div>
    <div class="collapsible-content" id="view-results">
        {% if cache_stats and cache_stats.found_cids > 0 %}
        <div class="actions" style="margin-bottom: 15px;">
            <button class="btn btn-success" onclick="runAction('{{ url_for('open_pubchem') }}', this)">
                Open All in PubChem
            </button>
            <a href="{{ url_for('download_cids') }}" class="btn btn-secondary">Download CIDs</a>
            <a href="{{ url_for('download_mapping') }}" class="btn btn-secondary">Download Full Mapping</a>
        </div>

        <div style="margin-bottom: 10px;">
            <label>Filter: </label>
            <select id="filter-select" onchange="filterTable()">
                <option value="all">All ({{ cache_stats.total_cas }})</option>
                <option value="found">Matched ({{ cache_stats.found_cids }})</option>
                <option value="not_found">No match ({{ cache_stats.not_found }})</option>
            </select>
        </div>
        <div id="results-table" style="max-height: 300px; overflow-y: auto;">
            <table>
                <thead>
                    <tr>
                        <th>CAS Number</th>
                        <th>Status</th>
                        <th>PubChem CID</th>
                    </tr>
                </thead>
                <tbody id="results-body">
                {% for cas, data in results[:200] %}
                    <tr data-status="{{ data.status }}">
                        <td class="mono">{{ cas }}</td>
                        <td>
                            {% if data.cid %}
                            <span class="badge badge-success">Matched</span>
                            {% else %}
                            <span class="badge badge-warning">No Match</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if data.cid %}
                            <a href="https://pubchem.ncbi.nlm.nih.gov/compound/{{ data.cid }}"
                               target="_blank" class="mono" style="color: var(--success);">{{ data.cid }}</a>
                            {% else %}
                            <span class="text-dim">-</span>
                            {% endif %}
                        </td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
        {% if cache_stats.total_cas > 200 %}
        <p class="text-dim" style="margin-top: 10px;">Showing first 200 of {{ cache_stats.total_cas }} results.</p>
        {% endif %}
        {% else %}
        <p class="text-dim">No results yet. Complete Steps 1 and 2 first.</p>
        {% endif %}
    </div>
</div>

<!-- Collapsible: No PubChem Match -->
<div class="card">
    <div class="collapsible-header" onclick="toggleSection('no-match')">
        <h2 style="margin: 0; border: none; padding: 0;">No PubChem Match ({{ no_match_count }})</h2>
        <span class="toggle-icon" id="no-match-icon">+ expand</span>
    </div>
    <div class="collapsible-content" id="no-match">
        {% if no_match_rows %}
        <p class="text-dim" style="margin-bottom: 15px;">
            These entries from the RUG database could not be matched to PubChem compounds.
        </p>

        <!-- Repair controls -->
        <div style="margin-bottom: 15px; padding: 12px; background: var(--accent); border-radius: 6px;">
            <p style="margin-bottom: 10px; font-size: 0.9rem;">
                <strong>Try Repair:</strong> Search PubChem using entry names to find matches that failed CAS lookup.
            </p>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                <button class="btn btn-success" id="btn-repair" onclick="startRepair()">
                    Repair Unmatched Entries
                </button>
            </div>
            <div id="repair-status-box" style="display: none; margin-top: 10px; display: none; align-items: center;">
                <span class="loading" id="repair-spinner" style="width: 14px; height: 14px;"></span>
                <span id="repair-status-text" style="margin-left: 8px; font-size: 0.85rem;"></span>
            </div>
        </div>

        <div style="max-height: 400px; overflow: auto;">
            <table>
                <thead>
                    <tr>
                        <th>Entry Name</th>
                        <th>CAS</th>
                        <th>Formula</th>
                        <th>Location</th>
                        <th>Pot</th>
                        <th>Repair Status</th>
                    </tr>
                </thead>
                <tbody>
                    {% for row in no_match_rows %}
                    <tr>
                        <td>{{ row.get('Name', '-') }}</td>
                        <td class="mono">{% if row.get('_original_cas') %}<s>{{ row._original_cas }}</s> {{ row.get('Casnr', '-') }}{% else %}{{ row.get('Casnr', '-') }}{% endif %}</td>
                        <td>{{ row.get('Formula', '-') }}</td>
                        <td>{{ row.get('Location', '-') }}</td>
                        <td>{{ row.get('Pot', '-') }}</td>
                        <td>
                            {% set rs = row.get('_repair_status', '') %}
                            {% if rs == 'repaired' %}
                                <span class="badge badge-success" title="Repaired via text search" style="font-size: 0.75rem;">&#10003; Repaired</span>
                            {% elif rs == 'failed' %}
                                <span class="badge badge-warning" title="Repair attempted, no match found" style="font-size: 0.75rem;">&#10007; No Match</span>
                            {% else %}
                                <span class="text-dim">Not attempted</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p class="text-dim">All entries matched successfully or no data available.</p>
        {% endif %}

        <!-- Review Pending Repairs -->
        <div class="card" id="review-repairs-card" style="display: none; margin-top: 15px; padding: 15px; border: 1px solid var(--success); border-radius: 6px;">
            <h3 style="margin-top: 0;">Review Pending Repairs</h3>
            <p class="text-dim" style="margin-bottom: 15px;">
                Select which repairs to accept. Unselected entries will remain unmatched.
            </p>
            <div style="margin-bottom: 15px; display: flex; gap: 10px; flex-wrap: wrap;">
                <button class="btn btn-secondary" onclick="toggleAllRepairs(true)">Select All</button>
                <button class="btn btn-secondary" onclick="toggleAllRepairs(false)">Deselect All</button>
                <button class="btn btn-success" id="btn-apply-repairs" onclick="applySelectedRepairs(this)">Apply Selected Repairs</button>
            </div>
            <div style="max-height: 400px; overflow: auto;">
                <table id="pending-repairs-table">
                    <thead>
                        <tr>
                            <th style="width: 30px;"></th>
                            <th>Entry Name</th>
                            <th>Old CAS</th>
                            <th>Real CAS</th>
                            <th>Found CID</th>
                            <th>Preview</th>
                        </tr>
                    </thead>
                    <tbody id="pending-repairs-body">
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
// Session state for two-phase browser refresh
let rugSessionId = null;

async function openRugLogin(btn) {
    if (!confirm('This will open a Chrome browser window.\\n\\nYou will need to:\\n1. Log in to the RUG system (including MFA)\\n2. Click Continue here when done\\n\\nContinue?')) {
        return;
    }

    const statusDiv = document.getElementById('browser-refresh-status');
    const statusText = document.getElementById('refresh-status-text');
    const btnContinue = document.getElementById('btn-continue');
    const instructions = document.getElementById('rug-instructions');
    const spinner = document.getElementById('status-spinner');

    btn.disabled = true;
    statusDiv.style.display = 'block';
    statusText.textContent = 'Opening browser... Check your desktop for Chrome window.';

    try {
        const resp = await fetch('{{ url_for("refresh_html_start") }}', { method: 'POST' });
        const data = await resp.json();

        if (data.error) {
            statusText.textContent = 'Error: ' + data.error;
            btn.disabled = false;
        } else if (data.session_id) {
            rugSessionId = data.session_id;
            spinner.style.display = 'none';
            statusText.textContent = 'Browser opened. Log in to RUG (including MFA), then click Continue.';
            instructions.textContent = 'Complete login in the Chrome window, then click Continue below.';
            btnContinue.style.display = 'inline-block';
        }
    } catch (e) {
        statusText.textContent = 'Error: ' + e.message;
        btn.disabled = false;
    }
}

async function continueAfterLogin(btn) {
    if (!rugSessionId) {
        alert('No active session. Please click "Fetch from RUG System" first.');
        return;
    }

    const statusText = document.getElementById('refresh-status-text');
    const spinner = document.getElementById('status-spinner');
    const btnOpen = document.getElementById('btn-open-login');

    btn.disabled = true;
    spinner.style.display = 'inline-block';
    statusText.textContent = 'Fetching chemicals data... This may take a moment.';

    try {
        const resp = await fetch('{{ url_for("refresh_html_continue", session_id="SESSION_ID_PLACEHOLDER") }}'.replace('SESSION_ID_PLACEHOLDER', rugSessionId), { method: 'POST' });
        const data = await resp.json();

        if (data.error) {
            spinner.style.display = 'none';
            statusText.textContent = 'Error: ' + data.error;
            btn.disabled = false;
            btnOpen.disabled = false;
            btn.style.display = 'none';
            rugSessionId = null;
        } else if (data.success) {
            statusText.textContent = 'Success! Saved: ' + data.snapshot;
            setTimeout(() => window.location.reload(), 1500);
        }
    } catch (e) {
        spinner.style.display = 'none';
        statusText.textContent = 'Error: ' + e.message;
        btn.disabled = false;
        btnOpen.disabled = false;
        btn.style.display = 'none';
        rugSessionId = null;
    }
}

function toggleSection(id) {
    const content = document.getElementById(id);
    const icon = document.getElementById(id + '-icon');
    if (content.classList.contains('open')) {
        content.classList.remove('open');
        icon.textContent = '+ expand';
    } else {
        content.classList.add('open');
        icon.textContent = '- collapse';
    }
}

// Poll compound-info background fetch status
async function pollCompoundInfoSetup() {
    const el = document.getElementById('compound-info-status');
    const text = document.getElementById('ci-status-text');
    const spinner = document.getElementById('ci-spinner');
    if (!el) return;
    try {
        const resp = await fetch('/api/compound-info-status');
        const data = await resp.json();
        if (data.status === 'running') {
            el.style.display = 'block';
            spinner.style.display = 'inline-block';
            text.textContent = 'Fetching compound info (structures, hazards)... ' + data.progress;
            setTimeout(pollCompoundInfoSetup, 3000);
        } else if (data.status === 'done') {
            el.style.display = 'block';
            spinner.style.display = 'none';
            text.innerHTML = '<span class="text-success">Compound info (structures, hazards) ready.</span>';
        } else {
            el.style.display = 'none';
        }
    } catch(e) {}
}
document.addEventListener('DOMContentLoaded', pollCompoundInfoSetup);

function filterTable() {
    const filter = document.getElementById('filter-select').value;
    const rows = document.querySelectorAll('#results-body tr');
    rows.forEach(row => {
        const status = row.dataset.status;
        if (filter === 'all') {
            row.style.display = '';
        } else if (filter === 'found' && status === 'found') {
            row.style.display = '';
        } else if (filter === 'not_found' && status === 'not_found') {
            row.style.display = '';
        } else {
            row.style.display = 'none';
        }
    });
}

// ---- Repair unmatched entries ----
async function startRepair() {
    if (!confirm('This will search PubChem for matches and let you review them before saving.\\n\\nContinue?')) return;

    const statusBox = document.getElementById('repair-status-box');
    const statusText = document.getElementById('repair-status-text');
    const btn = document.getElementById('btn-repair');

    btn.disabled = true;
    statusBox.style.display = 'flex';
    statusText.textContent = 'Starting repair...';

    try {
        const startResp = await fetch('/api/repair-unmatched/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });
        const startData = await startResp.json();

        if (startData.error) {
            alert('Error: ' + startData.error);
            btn.disabled = false;
            statusBox.style.display = 'none';
            return;
        }

        pollRepairStatus();
    } catch (e) {
        alert('Error: ' + e.message);
        btn.disabled = false;
        statusBox.style.display = 'none';
    }
}

async function pollRepairStatus() {
    const statusBox = document.getElementById('repair-status-box');
    const statusText = document.getElementById('repair-status-text');
    const spinner = document.getElementById('repair-spinner');
    const btn = document.getElementById('btn-repair');

    try {
        const resp = await fetch('/api/repair-unmatched/status');
        const data = await resp.json();

        if (data.status === 'running') {
            statusText.textContent = 'Repairing... ' + data.progress;
            setTimeout(() => pollRepairStatus(), 2000);
        } else if (data.status === 'done') {
            spinner.style.display = 'none';
            statusText.textContent = 'Repair scan complete! Loading results...';
            setTimeout(async () => {
                const hasPending = await loadPendingRepairs();
                if (hasPending) {
                    statusBox.style.display = 'none';
                    document.getElementById('review-repairs-card').scrollIntoView({behavior: 'smooth'});
                } else {
                    statusText.textContent = 'No matches found.';
                }
                btn.disabled = false;
            }, 500);
        } else {
            statusBox.style.display = 'none';
            btn.disabled = false;
        }
    } catch (e) {
        statusText.textContent = 'Error checking status';
        btn.disabled = false;
    }
}

async function loadPendingRepairs() {
    try {
        const resp = await fetch('/api/repair-unmatched/pending');
        const data = await resp.json();
        if (data.repaired_entries && data.repaired_entries.length > 0) {
            displayPendingRepairs(data.repaired_entries);
            document.getElementById('review-repairs-card').style.display = 'block';
            return true;
        }
    } catch (e) {
        console.error('Failed to load pending repairs:', e);
    }
    return false;
}

function displayPendingRepairs(entries) {
    const tbody = document.getElementById('pending-repairs-body');
    tbody.innerHTML = entries.map(entry =>
        '<tr>' +
        '<td><input type="checkbox" class="repair-checkbox" value="' + entry.row_index + '" checked></td>' +
        '<td>' + entry.name + '</td>' +
        '<td class="mono">' + entry.cas + '</td>' +
        '<td class="mono">' + (entry.real_cas || '<span class="text-dim">-</span>') + '</td>' +
        '<td><a href="https://pubchem.ncbi.nlm.nih.gov/compound/' + entry.cid + '" target="_blank" class="mono" style="color: var(--success);">' + entry.cid + '</a></td>' +
        '<td><img src="https://pubchem.ncbi.nlm.nih.gov/image/imgsrv.fcgi?cid=' + entry.cid + '&t=s" style="width: 40px; height: 40px; background: white; border-radius: 2px;" alt="structure"></td>' +
        '</tr>'
    ).join('');
}

function toggleAllRepairs(checked) {
    document.querySelectorAll('.repair-checkbox').forEach(cb => cb.checked = checked);
}

async function applySelectedRepairs(btn) {
    const selected = Array.from(document.querySelectorAll('.repair-checkbox:checked')).map(cb => cb.value);
    if (selected.length === 0) { alert('No repairs selected'); return; }
    if (!confirm('Apply ' + selected.length + ' repairs?')) return;

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = 'Applying...';

    try {
        const resp = await fetch('/api/repair-unmatched/apply', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({approved: selected})
        });
        const data = await resp.json();
        if (data.success) {
            alert('Applied ' + data.applied + ' repairs successfully!');
            window.location.reload();
        } else {
            alert('Error: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// --- Quick Import ---
async function importFromUrl(btn) {
    const url = document.getElementById('import-url').value.trim();
    if (!url) { alert('Enter a URL'); return; }
    btn.disabled = true; btn.textContent = 'Importing...';
    try {
        const resp = await fetch('/api/import-database-url', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url})
        });
        const data = await resp.json();
        if (data.success) {
            window.location.reload();
        } else if (data.auth_required) {
            window.open(data.url, '_blank');
            alert('Authentication required. The file is opening in your browser.\\nDownload it, then use the file picker to import.');
            btn.disabled = false; btn.textContent = 'Import from URL';
        } else {
            alert('Import failed: ' + data.error);
            btn.disabled = false; btn.textContent = 'Import from URL';
        }
    } catch (e) {
        alert('Import failed: ' + e.message);
        btn.disabled = false; btn.textContent = 'Import from URL';
    }
}

async function importFromFile(input) {
    const file = input.files[0];
    if (!file) return;
    const statusDiv = document.getElementById('import-status');
    const statusText = document.getElementById('import-status-text');
    statusDiv.style.display = '';
    statusText.textContent = 'Reading file...';
    try {
        const text = await file.text();
        statusText.textContent = 'Importing...';
        const resp = await fetch('/api/import-database', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: text
        });
        const data = await resp.json();
        if (data.success) { window.location.reload(); }
        else { alert('Import failed: ' + data.error); statusDiv.style.display = 'none'; }
    } catch (e) {
        alert('Import failed: ' + e.message);
        statusDiv.style.display = 'none';
    }
}

// Check for pending repairs on page load
document.addEventListener('DOMContentLoaded', loadPendingRepairs);


</script>
{% endblock %}
"""

# ============================================================================
# Template rendering helper
# ============================================================================

TEMPLATES = {
    "base": BASE_TEMPLATE,
    "search": SEARCH_TEMPLATE,
    "results": RESULTS_TEMPLATE,
    "setup": SETUP_TEMPLATE,
}

def render(template_name, **kwargs):
    """Render a template with the base template."""
    # Jinja2 doesn't support extends with render_template_string directly,
    # so we do a simple string replacement approach
    base = TEMPLATES["base"]
    content = TEMPLATES[template_name]

    # Extract the content block
    import re
    content_match = re.search(r'{%\s*block\s+content\s*%}(.*?){%\s*endblock\s*%}', content, re.DOTALL)
    scripts_match = re.search(r'{%\s*block\s+scripts\s*%}(.*?){%\s*endblock\s*%}', content, re.DOTALL)

    content_block = content_match.group(1) if content_match else ""
    scripts_block = scripts_match.group(1) if scripts_match else ""

    # Replace in base
    html = base.replace("{% block content %}{% endblock %}", content_block)
    html = html.replace("{% block scripts %}{% endblock %}", scripts_block)

    return render_template_string(html, **kwargs)


# ============================================================================
# Routes
# ============================================================================

@app.route("/")
def index():
    """Redirect to search (if setup complete) or setup."""
    if is_setup_complete():
        return redirect(url_for("search"))
    return redirect(url_for("setup"))


@app.route("/search")
def search():
    """Main search page."""
    cache = load_cid_cache()

    has_cids = False
    cid_count = 0
    if cache and "results" in cache:
        cids = [r for r in cache["results"].values() if r.get("cid")]
        has_cids = len(cids) > 0
        cid_count = len(cids)

    return render("search",
        title="Search",
        active_page="search",
        has_cids=has_cids,
        cid_count=cid_count,
    )


@app.route("/setup")
def setup():
    """Setup/configuration page."""
    snapshots = list_snapshots()
    cache = load_cid_cache()

    latest_snapshot = None
    cache_valid = False

    latest_path = get_latest_snapshot()
    if latest_path:
        latest_snapshot = {
            "name": latest_path.name,
            "path": latest_path,
            "timestamp": datetime.fromtimestamp(latest_path.stat().st_mtime),
            "size": latest_path.stat().st_size,
        }
        # Imported databases use source_hash="imported" — treat as valid
        if cache and cache.get("source_hash") == "imported":
            cache_valid = True
        else:
            cache_valid, _ = is_cid_cache_valid(latest_path)

    results_list = []
    if cache and "results" in cache:
        results_list = list(cache["results"].items())

    # Get no-match entries from RUG table
    no_match_rows = []
    rug_table = load_rug_table()
    if rug_table:
        for row in rug_table.get("rows", []):
            cid_val = row.get("CID")
            if cid_val is None or (isinstance(cid_val, float) and cid_val != cid_val):  # None or NaN
                no_match_rows.append(row)

    return render("setup",
        title="Setup",
        active_page="setup",
        snapshot_count=len(snapshots),
        snapshots=snapshots,
        cache_stats=cache.get("stats") if cache else None,
        cache_created=cache.get("created", "")[:19] if cache else None,
        latest_snapshot=latest_snapshot,
        cache_valid=cache_valid,
        setup_complete=is_setup_complete(),
        results=results_list,
        no_match_rows=no_match_rows,
        no_match_count=len(no_match_rows),
    )


# Legacy routes - redirect to new structure
@app.route("/snapshots")
def snapshots():
    return redirect(url_for("setup"))


@app.route("/results")
def results_page():
    """Results page showing filtered RUG table with enriched compound info."""
    filter_id = request.args.get("filter_id")
    rug_table = load_rug_table()
    filter_results = load_filter_results()
    compound_info = load_compound_info().get("compounds", {})

    current_filter = None
    filtered_rows = []

    if rug_table and filter_results:
        # Handle "All Chemicals" synthetic filter
        if filter_id == "all":
            # Create synthetic "All Chemicals" filter
            all_cids = []
            for row in rug_table.get("rows", []):
                cid_val = row.get("CID")
                if cid_val is not None:
                    try:
                        all_cids.append(int(cid_val))
                    except (TypeError, ValueError):
                        pass

            cache_key = upload_cids_to_pubchem_cache([str(c) for c in all_cids])
            pubchem_url = f"https://pubchem.ncbi.nlm.nih.gov/#query={cache_key}" if cache_key else ""

            current_filter = {
                "id": "all",
                "search_name": "All Chemicals",
                "operation": "None",
                "matching_cids": all_cids,
                "match_count": len(all_cids),
                "created": "",
                "pubchem_url": pubchem_url
            }
        else:
            # Existing filter logic
            if filter_id:
                current_filter = next((f for f in filter_results if f["id"] == filter_id), None)
            if not current_filter and filter_results:
                current_filter = filter_results[0]

        if current_filter:
            matching_cid_set = set(current_filter.get("matching_cids", []))
            for row in rug_table.get("rows", []):
                cid_val = row.get("CID")
                if cid_val is None:
                    continue
                try:
                    cid_int = int(cid_val)
                except (TypeError, ValueError):
                    continue
                if cid_int in matching_cid_set:
                    # Enrich row with compound info and helper
                    row["_cid_int"] = cid_int
                    row["_ci"] = compound_info.get(str(cid_int), {})

                    # Repair status is already on the row if it was repaired
                    if not row.get("_repair_status"):
                        row["_repair_status"] = "original"

                    filtered_rows.append(row)

    # Build grouped rows (group by CAS number)
    from collections import OrderedDict
    identity_cols = {'Structure', 'Name', 'CAS', 'Formula', 'MW', 'Hazards', 'SMILES', 'IUPAC', 'CID'}
    varying_cols = ['GROSname', 'Pot', 'Location', 'Owner', 'OwnerRegNumber']
    cas_groups = OrderedDict()
    for row in filtered_rows:
        cas = row.get('Casnr', '') or ''
        cas_groups.setdefault(cas, []).append(row)
    grouped_rows = []
    for cas, group in cas_groups.items():
        rep = dict(group[0])  # copy representative row
        rep['_group_count'] = len(group)
        varying = {}
        for vc in varying_cols:
            unique_vals = list(OrderedDict.fromkeys(
                r.get(vc, '') or '-' for r in group
            ))
            if len(unique_vals) > 1:
                varying[vc] = unique_vals
        rep['_group_varying'] = varying
        grouped_rows.append(rep)

    # Determine visible columns from defaults (JS will override via localStorage)
    visible_columns = list(DEFAULT_RESULTS_COLUMNS)

    # Collect unique locations for the filter dropdown
    unique_locations = sorted(set(
        row.get("Location", "").strip()
        for row in filtered_rows
        if row.get("Location", "").strip()
    ))

    return render("results",
        title="Results",
        active_page="results",
        rug_table=rug_table,
        filter_results=filter_results,
        current_filter=current_filter,
        filtered_rows=filtered_rows,
        grouped_rows=grouped_rows,
        all_columns=ALL_RESULTS_COLUMNS,
        default_columns=DEFAULT_RESULTS_COLUMNS,
        visible_columns=visible_columns,
        ghs_names=GHS_NAMES,
        unique_locations=unique_locations,
    )


@app.route("/combine")
def combine():
    return redirect(url_for("search"))


@app.route("/api/run-extraction", methods=["POST"])
def run_extraction():
    """Run the extraction process."""
    refresh_cids = request.args.get("refresh_cids") == "1"

    latest_path = get_latest_snapshot()
    if not latest_path:
        return jsonify({"error": "No HTML file found. Upload one first."})

    html_path = latest_path

    # Check cache
    use_cached = False
    if not refresh_cids:
        is_valid, cache_data = is_cid_cache_valid(html_path)
        if is_valid and cache_data:
            use_cached = True

    if not use_cached:
        # Parse HTML and run lookups
        try:
            df = parse_html_table(html_path)
            cas_numbers = extract_cas_numbers(df)
            pubchem_results = lookup_cas_to_cid_optimized(cas_numbers)
            save_cid_cache(html_path, compute_file_hash(html_path), pubchem_results)
            save_rug_table(df, pubchem_results)
        except Exception as e:
            return jsonify({"error": str(e)})

    # Kick off background compound info fetch (force re-fetch on re-lookup)
    start_compound_info_fetch(force=refresh_cids)

    return jsonify({"redirect": url_for("setup")})


@app.route("/api/refresh-html", methods=["POST"])
def refresh_html():
    """Trigger Selenium browser refresh to fetch new HTML."""
    try:
        snapshot_path = refresh_html_from_browser()
        if snapshot_path:
            return jsonify({
                "success": True,
                "snapshot": snapshot_path.name,
            })
        else:
            return jsonify({"error": "Failed to fetch HTML from browser"})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/refresh-html/start", methods=["POST"])
def refresh_html_start():
    """Start phase: Open browser for RUG login, return session ID immediately."""
    try:
        session_id = start_browser_session()
        return jsonify({
            "session_id": session_id,
            "status": "browser_opened",
        })
    except ImportError:
        return jsonify({"error": "Selenium is not installed. Run: pip install selenium"})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/refresh-html/continue/<session_id>", methods=["POST"])
def refresh_html_continue(session_id):
    """Continue phase: Wait for login, fetch data, return snapshot path."""
    try:
        snapshot_path = complete_browser_session(session_id)
        if snapshot_path:
            return jsonify({
                "success": True,
                "snapshot": snapshot_path.name,
            })
        else:
            return jsonify({"error": "Failed to fetch HTML from browser"})
    except KeyError:
        return jsonify({"error": "Session not found. Browser may have been closed."})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/upload-snapshot", methods=["POST"])
def upload_snapshot():
    """Upload a new HTML snapshot."""
    if "file" not in request.files:
        return redirect(url_for("setup"))

    file = request.files["file"]
    if file.filename == "":
        return redirect(url_for("setup"))

    # Save to snapshots directory
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Search_{timestamp}.html"
    filepath = SNAPSHOTS_DIR / filename

    file.save(filepath)

    # Update pointer to new snapshot
    update_latest_pointer(filepath)

    return redirect(url_for("setup"))


@app.route("/api/set-latest/<filename>", methods=["POST"])
def set_latest(filename):
    """Set a snapshot as the latest."""
    filepath = SNAPSHOTS_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "Snapshot not found"})

    # Update pointer to selected snapshot
    update_latest_pointer(filepath)

    return jsonify({"success": True})


@app.route("/api/open-pubchem", methods=["POST"])
def open_pubchem():
    """Upload CIDs to PubChem and return the search URL."""
    cache = load_cid_cache()
    if not cache or "results" not in cache:
        return jsonify({"error": "No results to search"})

    cids = [str(r["cid"]) for r in cache["results"].values() if r.get("cid")]
    if not cids:
        return jsonify({"error": "No CIDs found"})

    # Try to upload to PubChem cache
    cache_key = upload_cids_to_pubchem_cache(cids)

    if cache_key:
        url = f"https://pubchem.ncbi.nlm.nih.gov/#query={cache_key}"
    else:
        # Fallback to direct URL (may be too long)
        url = f"https://pubchem.ncbi.nlm.nih.gov/#query={','.join(cids[:100])}"

    return jsonify({"pubchem_url": url})


def _lookup_search_name(cache_key: str) -> str:
    """Look up a human-readable name for a cache key from browser history or app searches."""
    history = get_pubchem_history_details()
    for entry in history:
        if entry["cachekey"] == cache_key:
            return entry["name"]
    app_meta = load_app_search_metadata()
    if cache_key in app_meta:
        return app_meta[cache_key].get("query", "Unknown search")
    return "Unknown search"


@app.route("/api/combine-pubchem/<operation>", methods=["POST"])
def combine_pubchem(operation):
    """Combine our CIDs with a selected PubChem search."""
    operation = operation.upper()
    if operation not in ("AND", "OR", "NOT"):
        return jsonify({"error": f"Invalid operation: {operation}. Use AND, OR, or NOT."})

    # Get the selected cache key from query params
    user_key = request.args.get("cachekey")
    if not user_key:
        user_key = get_latest_pubchem_history_cachekey()
        if not user_key:
            return jsonify({
                "error": "No search selected. Perform a search on PubChem first."
            })

    # Get our CIDs
    cache = load_cid_cache()
    if not cache or "results" not in cache:
        return jsonify({"error": "No CID results found. Complete setup first."})

    cids = [str(r["cid"]) for r in cache["results"].values() if r.get("cid")]
    if not cids:
        return jsonify({"error": "No CIDs found in results."})

    # Upload our CIDs to PubChem cache
    our_key = upload_cids_to_pubchem_cache(cids)
    if not our_key:
        return jsonify({"error": "Failed to upload CIDs to PubChem cache."})

    # Combine the two cache keys
    combine_result = combine_pubchem_cache_keys(user_key, our_key, operation)

    combined_key = None
    list_size = None
    local_matching_cids = None  # Set if we computed results locally

    if combine_result:
        combined_key, list_size = combine_result
    else:
        # Combine API failed - try fallback: fetch both CID lists and compute locally
        logger.info("Combine API failed, trying local fallback...")

        user_cids = fetch_cids_from_listkey(user_key)
        if user_cids is None:
            # User search is stale/expired
            search_name = _lookup_search_name(user_key)
            return jsonify({
                "error": "stale_search",
                "cache_key": user_key,
                "search_name": search_name,
                "message": f"The search '{search_name}' is no longer available on PubChem (expired after ~12 hours).",
            })

        # Compute the operation locally
        our_cid_set = set(int(c) for c in cids)
        user_cid_set = set(user_cids)

        if operation == "AND":
            result_cids = our_cid_set & user_cid_set
        elif operation == "OR":
            result_cids = our_cid_set | user_cid_set
        elif operation == "NOT":
            result_cids = our_cid_set - user_cid_set
        else:
            result_cids = set()

        list_size = len(result_cids)
        local_matching_cids = list(result_cids)  # Save for later use
        logger.info("Local fallback computed %d results for %s operation", list_size, operation)

        if list_size > 0:
            # Upload result CIDs to get a cache key
            combined_key = upload_cids_to_pubchem_cache([str(c) for c in result_cids])
            if not combined_key:
                return jsonify({"error": "Failed to upload combined results to PubChem."})
        else:
            # 0 results - we'll handle this below
            combined_key = None

    # Handle the case where combine found 0 results (from API or local fallback)
    if list_size == 0 or combined_key is None:
        logger.info("Combine succeeded but found 0 matching compounds")
        # Look up search name for the message
        search_name = _lookup_search_name(user_key)

        # Save empty filter result so user can see it was attempted
        filter_id = save_filter_result(search_name, operation, [], "")
        return jsonify({
            "pubchem_url": "",
            "filter_id": filter_id,
            "match_count": 0,
        })

    # Record this combined key as app-generated
    save_app_search(combined_key)

    url = f"https://pubchem.ncbi.nlm.nih.gov/#query={combined_key}"
    result = {"pubchem_url": url}

    # Get the matching CIDs - use local results if available, otherwise fetch
    if local_matching_cids is not None:
        matching_cids = local_matching_cids
        logger.info("Using locally computed %d CIDs", len(matching_cids))
    else:
        logger.info("Fetching CIDs from combined listkey")
        matching_cids = fetch_cids_from_listkey(combined_key)

        if matching_cids is None:
            # Fetch failed - check if user search is stale
            logger.warning("CID fetch failed, checking if user search is stale...")

            # Validate the user's original search
            user_cids = fetch_cids_from_listkey(user_key)
            if user_cids is None:
                # User search is stale
                search_name = _lookup_search_name(user_key)
                return jsonify({
                    "error": "stale_search",
                    "cache_key": user_key,
                    "search_name": search_name,
                    "message": f"The search '{search_name}' is no longer available on PubChem (expired after ~12 hours)."
                })

            # Combined operation failed for other reason
            return jsonify({
                "error": "combine_failed",
                "message": "Failed to combine searches in PubChem."
            })

    if matching_cids:
        # Look up search name from history or app searches
        search_name = _lookup_search_name(user_key)
        filter_id = save_filter_result(search_name, operation, matching_cids, url)
        result["filter_id"] = filter_id

    return jsonify(result)


@app.route("/api/compound-info-status")
def compound_info_status():
    """Return status of background compound info fetch."""
    s = _compound_info_status
    total = s["total"]
    fetched = s["fetched"]
    progress = f"{fetched}/{total}" if total else ""
    return jsonify({"status": s["status"], "progress": progress})


@app.route("/api/filter-results/<filter_id>/table")
def filter_results_table(filter_id):
    """JSON endpoint returning the filtered table data."""
    rug_table = load_rug_table()
    if not rug_table:
        return jsonify({"error": "RUG table not loaded"}), 404

    all_filters = load_filter_results()
    current = next((f for f in all_filters if f["id"] == filter_id), None)
    if not current:
        return jsonify({"error": "Filter not found"}), 404

    matching_cid_set = set(current.get("matching_cids", []))
    columns = rug_table.get("columns", [])
    rows = [
        row for row in rug_table.get("rows", [])
        if row.get("CID") is not None and int(row["CID"]) in matching_cid_set
    ]

    return jsonify({
        "rows": rows,
        "columns": columns,
        "filter": {k: v for k, v in current.items() if k != "matching_cids"},
    })


@app.route("/api/pubchem-history/check")
def pubchem_history_check():
    """Lightweight endpoint: return only a fingerprint of browser storage mtimes.

    The frontend polls this every few seconds and only calls the full
    /api/pubchem-history when the fingerprint changes.
    """
    return jsonify({"fingerprint": get_history_fingerprint()})


@app.route("/api/pubchem-history")
def pubchem_history():
    """Get all PubChem search history."""
    history = get_pubchem_history_details()
    default_browser = get_default_browser()
    supported = default_browser is not None and default_browser in ("Chrome", "Firefox")
    warning = None
    if default_browser and not supported:
        warning = (
            f"Your default browser ({default_browser}) does not support "
            "history lookup. Only Firefox and Chrome are supported."
        )

    # Load app search metadata and stale searches
    app_search_metadata = load_app_search_metadata()
    app_searches = load_app_searches()
    stale_searches = load_stale_searches()

    # Get existing cache keys from browser history
    browser_history_keys = {entry["cachekey"] for entry in history}

    # Add app-initiated direct searches to history that aren't already present
    # These are distinct from combined searches - they ARE meant to be combined
    for cache_key, meta in app_search_metadata.items():
        if cache_key not in browser_history_keys and cache_key not in stale_searches:
            history.append({
                "cachekey": cache_key,
                "name": meta.get("query", "App search"),
                "timestamp": meta.get("timestamp", ""),
                "browser": "App",
                "url": f"https://pubchem.ncbi.nlm.nih.gov/#query={cache_key}",
                "_list_size": meta.get("count"),
            })

    # Sort by timestamp (most recent first)
    history.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # Filter out app-generated and stale searches by default
    excluded_searches = app_searches | stale_searches

    filtered_history = [
        entry for entry in history
        if entry["cachekey"] not in excluded_searches
    ]

    return jsonify({
        "history": filtered_history,
        "all_history": history,  # Keep full list for toggle
        "count": len(filtered_history),
        "default_browser": default_browser,
        "browser_warning": warning,
        "app_search_count": len(history) - len(filtered_history),
    })


def _pubchem_structure_search(search_type: str, smiles: str, threshold: int = 90) -> dict | None:
    """Execute a PubChem structure search via structure_search.cgi.

    Uses PubChem's internal structure_search.cgi endpoint which returns a
    cachekey directly, avoiding the unreliable PUG REST ListKey polling.

    Args:
        search_type: One of 'substructure', 'superstructure', 'similarity'
        smiles: SMILES string to search
        threshold: Similarity threshold (only used for similarity search)

    Returns:
        Dict with 'cache_key' and 'count', or None if failed
    """
    import json as _json
    from urllib.parse import quote

    parameters = [
        {"name": "smiles", "string": smiles},
        {"name": "UseCache", "bool": True},
        {"name": "SearchTimeMsec", "num": 5000},
        {"name": "SearchMaxRecords", "num": 10000},
    ]
    if search_type == "similarity":
        parameters.append({"name": "Threshold", "num": threshold})

    queryblob = _json.dumps({
        "query": {
            "type": search_type,
            "parameter": parameters,
        }
    })

    url = (
        "https://pubchem.ncbi.nlm.nih.gov/unified_search/structure_search.cgi"
        f"?format=json&queryblob={quote(queryblob)}"
    )

    logger.info("Submitting %s search via structure_search.cgi", search_type)

    try:
        resp = _requests.get(url, timeout=60)
    except _requests.RequestException as e:
        logger.error("PubChem %s search failed: %s", search_type, e)
        return None

    if resp.status_code != 200:
        logger.error("PubChem %s search HTTP %d: %s", search_type, resp.status_code, resp.text[:300])
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.error("Invalid JSON from PubChem %s search", search_type)
        return None

    response = data.get("response", {})
    cachekey = response.get("cachekey")
    hitcount = response.get("hitcount", 0)

    if not cachekey:
        logger.error("No cachekey in response: %s", data)
        return None

    logger.info("Got cachekey: %s... (%d hits)", cachekey[:20], hitcount)
    return {"cache_key": cachekey, "count": hitcount}


@app.route("/api/pubchem-search", methods=["POST"])
def pubchem_search():
    """Execute a PubChem search and return a cache key."""
    from urllib.parse import quote

    data = request.get_json()
    query = data.get("query", "").strip() if data else ""
    mode = data.get("mode", "name") if data else "name"

    if not query:
        return jsonify({"error": "No query provided"})

    cids = None
    search_label = query  # Label for the search in history

    if mode == "name":
        # Synchronous name/keyword search
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote(query)}/cids/JSON"
        try:
            resp = _requests.get(url, timeout=30)
        except _requests.RequestException as e:
            logger.error("PubChem name search failed: %s", e)
            return jsonify({"error": f"Request failed: {e}"})

        if resp.status_code == 404:
            return jsonify({"error": "No results found", "count": 0})
        if resp.status_code != 200:
            return jsonify({"error": f"PubChem error: {resp.status_code}"})

        try:
            cids = resp.json().get("IdentifierList", {}).get("CID", [])
        except (ValueError, KeyError):
            return jsonify({"error": "Invalid response from PubChem"})

    elif mode == "smiles":
        # Synchronous exact SMILES search
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/{quote(query)}/cids/JSON"
        try:
            resp = _requests.get(url, timeout=30)
        except _requests.RequestException as e:
            logger.error("PubChem SMILES search failed: %s", e)
            return jsonify({"error": f"Request failed: {e}"})

        if resp.status_code == 404:
            return jsonify({"error": "No compound found for this SMILES", "count": 0})
        if resp.status_code != 200:
            # Check for invalid SMILES error
            try:
                err_data = resp.json()
                if "Fault" in err_data:
                    fault_msg = err_data["Fault"].get("Message", "Invalid SMILES")
                    return jsonify({"error": fault_msg})
            except ValueError:
                pass
            return jsonify({"error": f"PubChem error: {resp.status_code}"})

        try:
            cids = resp.json().get("IdentifierList", {}).get("CID", [])
        except (ValueError, KeyError):
            return jsonify({"error": "Invalid response from PubChem"})
        search_label = f"SMILES: {query[:30]}{'...' if len(query) > 30 else ''}"

    elif mode in ("substructure", "superstructure", "similarity"):
        # Structure searches - use ListKey-based approach (doesn't download all CIDs)
        result = _pubchem_structure_search(mode, query)
        if result is None:
            return jsonify({"error": f"{mode.title()} search failed. Check that your SMILES is valid."})

        cache_key = result.get("cache_key")
        cid_count = result.get("count", 0)

        if not cache_key:
            if cid_count == 0:
                return jsonify({"error": "No results found", "count": 0})
            return jsonify({"error": "Failed to create search cache"})

        mode_labels = {
            "substructure": "Substructure",
            "superstructure": "Superstructure",
            "similarity": "Similarity"
        }
        search_label = f"{mode_labels[mode]}: {query[:25]}{'...' if len(query) > 25 else ''}"

        # Save and return directly (we already have cache_key)
        save_app_search_with_metadata(cache_key, search_label, cid_count)
        return jsonify({
            "success": True,
            "cache_key": cache_key,
            "query": search_label,
            "count": cid_count,
            "url": f"https://pubchem.ncbi.nlm.nih.gov/#query={cache_key}"
        })

    else:
        return jsonify({"error": f"Unknown search mode: {mode}"})

    if not cids:
        return jsonify({"error": "No results found", "count": 0})

    # Upload CIDs to get cache key (for name/smiles searches)
    cache_key = upload_cids_to_pubchem_cache([str(c) for c in cids])
    if not cache_key:
        return jsonify({"error": "Failed to create search cache"})

    # Save to app searches file with metadata
    save_app_search_with_metadata(cache_key, search_label, len(cids))

    return jsonify({
        "success": True,
        "cache_key": cache_key,
        "query": search_label,
        "count": len(cids),
        "url": f"https://pubchem.ncbi.nlm.nih.gov/#query={cache_key}"
    })


@app.route("/api/mark-stale-search", methods=["POST"])
def mark_stale_search():
    """Mark a search as stale/expired."""
    data = request.get_json()
    cache_key = data.get("cache_key")
    if not cache_key:
        return jsonify({"error": "cache_key required"}), 400

    mark_search_as_stale(cache_key)
    return jsonify({"success": True})


# Legacy endpoint alias
@app.route("/api/firefox-pubchem-history")
def firefox_pubchem_history():
    """Legacy alias for pubchem_history."""
    return pubchem_history()


@app.route("/api/download/cids")
def download_cids():
    """Download CIDs as text file."""
    cache = load_cid_cache()
    if not cache or "results" not in cache:
        return "No results", 404

    cids = [str(r["cid"]) for r in cache["results"].values() if r.get("cid")]
    content = "\n".join(cids)

    from flask import Response
    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=pubchem_cids.txt"}
    )


@app.route("/api/download/mapping")
def download_mapping():
    """Download CAS→CID mapping as CSV."""
    cache = load_cid_cache()
    if not cache or "results" not in cache:
        return "No results", 404

    lines = ["cas,status,cid"]
    for cas, data in cache["results"].items():
        cid = data.get("cid") or ""
        status = data.get("status", "unknown")
        lines.append(f"{cas},{status},{cid}")

    content = "\n".join(lines)

    from flask import Response
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cas_to_pubchem.csv"}
    )


@app.route("/api/repair-unmatched/start", methods=["POST"])
def start_repair():
    """Start repair task for unmatched entries (always review mode)."""
    if start_repair_task():
        return jsonify({"status": "started"})
    else:
        return jsonify({"error": "Repair task already running"}), 409


@app.route("/api/repair-unmatched/status")
def repair_status():
    """Get repair task status."""
    with _repair_lock:
        s = _repair_status.copy()

    progress_text = ""
    if s["total"] > 0:
        progress_text = f"{s['processed']}/{s['total']}"
        if s["current_name"]:
            progress_text += f" - {s['current_name']}"

    return jsonify({
        "status": s["status"],
        "progress": progress_text,
        "processed": s["processed"],
        "total": s["total"],
        "current_name": s.get("current_name", "")
    })


@app.route("/api/repair-unmatched/pending")
def get_pending_repairs():
    """Get pending repairs for review."""
    pending_file = DATA_DIR / "pending_repairs.json"
    if not pending_file.exists():
        return jsonify({"repaired_entries": []})

    with open(pending_file) as f:
        data = json.load(f)

    return jsonify(data)


@app.route("/api/repair-unmatched/apply", methods=["POST"])
def apply_repairs():
    """Apply approved repairs directly to rug_table rows by index."""
    data = request.get_json()
    approved_indices = set(int(i) for i in data.get("approved", []))

    pending_file = DATA_DIR / "pending_repairs.json"
    if not pending_file.exists():
        return jsonify({"error": "No pending repairs"}), 404

    with open(pending_file) as f:
        pending_data = json.load(f)

    # Load current data
    from extract_chemicals import load_rug_table, RUG_TABLE_FILE
    rug_table = load_rug_table()
    cid_cache = load_cid_cache()
    if not rug_table or not cid_cache:
        return jsonify({"error": "Data not found"}), 404

    rows = rug_table.get("rows", [])

    # Apply approved repairs by row index
    applied_count = 0
    for entry in pending_data.get("repaired_entries", []):
        row_index = entry.get("row_index")
        if row_index is None or row_index not in approved_indices:
            continue
        if row_index < 0 or row_index >= len(rows):
            continue

        row = rows[row_index]
        cid = entry["cid"]
        real_cas = entry.get("real_cas")

        # Set CID and repair metadata on the row
        row["CID"] = cid
        row["_repair_status"] = "repaired"
        row["_repair_source"] = entry.get("repair_source", "")

        # Update CAS: store original, set new real CAS if available
        if real_cas:
            row["_original_cas"] = row.get("Casnr", "")
            row["Casnr"] = real_cas

        # Add cache entry keyed by the (possibly new) CAS
        cache_cas = row["Casnr"]
        cid_cache.setdefault("results", {})[cache_cas] = {
            "cid": cid,
            "status": "repaired",
            "repair_attempted": True,
            "repair_source": entry.get("repair_source", ""),
            "repair_timestamp": datetime.now().isoformat()
        }

        applied_count += 1

    # Save rug_table directly (preserves row metadata)
    RUG_TABLE_FILE.write_text(json.dumps(rug_table, indent=2, default=str))

    # Save updated cache
    save_cid_cache(
        cid_cache["source_html"],
        cid_cache["source_hash"],
        cid_cache["results"]
    )

    # Delete pending file
    pending_file.unlink()

    # Trigger compound info fetch for new CIDs
    start_compound_info_fetch(force=False)

    return jsonify({
        "success": True,
        "applied": applied_count,
        "total_pending": len(pending_data.get("repaired_entries", []))
    })


@app.route("/api/export-database", methods=["POST"])
def export_database():
    """Export all data as a single JSON bundle."""
    rug_table = load_rug_table()
    cid_cache = load_cid_cache()
    compound_info = load_compound_info()

    bundle = {
        "version": 1,
        "exported": datetime.now().isoformat(),
        "cid_cache": cid_cache,
        "rug_table": rug_table,
        "compound_info": compound_info,
    }

    return Response(
        json.dumps(bundle, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=chem_database_export.json"},
    )


def _do_import_database(bundle):
    """Shared import logic. Returns (success, error_msg)."""
    if not isinstance(bundle, dict):
        return False, "Invalid JSON: expected object"
    if bundle.get("version") != 1:
        return False, "Unsupported bundle version"
    if "cid_cache" not in bundle or "rug_table" not in bundle:
        return False, "Bundle must contain cid_cache and rug_table"

    cid_cache = bundle["cid_cache"]
    rug_table = bundle["rug_table"]
    compound_info = bundle.get("compound_info")

    # Mark as imported
    cid_cache["source_html"] = "imported"
    cid_cache["source_hash"] = "imported"

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Write cid_cache
    CID_CACHE_FILE.write_text(json.dumps(cid_cache, indent=2, default=str))

    # Write rug_table
    RUG_TABLE_FILE.write_text(json.dumps(rug_table, indent=2, default=str))

    # Write compound_info if present
    if compound_info:
        COMPOUND_INFO_FILE.write_text(json.dumps(compound_info, indent=2, default=str))

    # Create dummy snapshot + latest.txt pointer so setup detects a valid state
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    dummy_path = SNAPSHOTS_DIR / "imported_database.html"
    if not dummy_path.exists():
        dummy_path.write_text("<html><body>Imported database</body></html>")
    update_latest_pointer(dummy_path)

    # Trigger compound info fetch if not in bundle
    if not compound_info or not compound_info.get("compounds"):
        start_compound_info_fetch()

    return True, None


@app.route("/api/import-database", methods=["POST"])
def import_database():
    """Import a database bundle from uploaded JSON."""
    try:
        bundle = request.get_json(force=True)
    except Exception as e:
        return jsonify({"success": False, "error": f"Invalid JSON: {e}"})

    success, error = _do_import_database(bundle)
    if success:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": error})


@app.route("/api/import-database-url", methods=["POST"])
def import_database_url():
    """Import a database bundle from a URL (e.g. Google Drive)."""
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "No URL provided"})

    # Convert Google Drive share URLs to direct download
    import re as _re
    gd_match = _re.search(r'drive\.google\.com/file/d/([^/]+)', url)
    if gd_match:
        file_id = gd_match.group(1)
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
    elif 'drive.google.com' in url and 'id=' in url:
        gd_match2 = _re.search(r'[?&]id=([^&]+)', url)
        if gd_match2:
            file_id = gd_match2.group(1)
            url = f"https://drive.google.com/uc?export=download&id={file_id}"

    original_url = data.get("url", "").strip()

    try:
        resp = _requests.get(url, timeout=30, allow_redirects=True)
    except Exception as e:
        return jsonify({"success": False, "error": f"Download failed: {e}"})

    content_type = resp.headers.get("Content-Type", "")
    body = resp.text.strip()

    # Detect HTML login page instead of JSON
    if "text/html" in content_type or body.startswith("<"):
        return jsonify({
            "success": False,
            "auth_required": True,
            "url": original_url,
            "error": "Authentication required — download the file in your browser and use the file upload option.",
        })

    try:
        bundle = resp.json()
    except Exception:
        return jsonify({"success": False, "error": "Response is not valid JSON"})

    success, error = _do_import_database(bundle)
    if success:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": error})


@app.route("/api/filter-results/<filter_id>/save", methods=["POST"])
def save_filter(filter_id):
    """Mark a filter result as saved."""
    if toggle_saved_filter(filter_id, True):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Filter not found"}), 404


@app.route("/api/filter-results/<filter_id>/unsave", methods=["POST"])
def unsave_filter(filter_id):
    """Remove saved flag from a filter result."""
    if toggle_saved_filter(filter_id, False):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Filter not found"}), 404


@app.route("/api/filter-results/<filter_id>", methods=["DELETE"])
def remove_filter(filter_id):
    """Delete a filter result entirely."""
    if delete_filter_result(filter_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Filter not found"}), 404


@app.route("/api/quit", methods=["POST"])
def quit_app():
    """Quit the application."""
    import threading
    def shutdown():
        import time
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=shutdown, daemon=True).start()
    return jsonify({"status": "shutting_down"})


@app.route("/api/check-update")
def check_update():
    """Check GitHub for a newer release."""
    try:
        from packaging.version import Version
    except ImportError:
        # Fallback: simple tuple comparison
        Version = None

    def parse_ver(v):
        parts = v.lstrip("v").split(".")
        return tuple(int(x) for x in parts)

    # Detect if running from a git repo (source install) vs PyInstaller
    import subprocess
    is_git = False
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True,
                       check=True, cwd=os.path.dirname(__file__) or ".")
        is_git = True
    except Exception:
        pass

    try:
        headers = {"Accept": "application/vnd.github+json"}

        # First try releases
        resp = _requests.get(
            "https://api.github.com/repos/mf-rug/rug_chemsearch_cl/releases/latest",
            timeout=10, headers=headers,
        )
        release_data = None
        if resp.status_code != 404:
            resp.raise_for_status()
            release_data = resp.json()

        # Also check latest tag for source installs or if no release exists
        latest_ver = None
        if release_data:
            latest_ver = release_data.get("tag_name", "v0.0.0").lstrip("v")
        else:
            tag_resp = _requests.get(
                "https://api.github.com/repos/mf-rug/rug_chemsearch_cl/tags?per_page=1",
                timeout=10, headers=headers,
            )
            tag_resp.raise_for_status()
            tags = tag_resp.json()
            if tags:
                latest_ver = tags[0]["name"].lstrip("v")
            else:
                return jsonify({"update_available": False, "current": APP_VERSION,
                                "latest": APP_VERSION, "message": "No versions published yet."})

        if Version:
            update_available = Version(latest_ver) > Version(APP_VERSION)
        else:
            update_available = parse_ver(latest_ver) > parse_ver(APP_VERSION)

        # Build download URL and notes
        release_notes = ""
        download_url = f"https://github.com/mf-rug/rug_chemsearch_cl/releases/tag/v{latest_ver}"
        if release_data:
            release_notes = release_data.get("body", "") or ""
            download_url = release_data.get("html_url", download_url)
            for asset in release_data.get("assets", []):
                if asset["name"].endswith(".zip"):
                    download_url = asset["browser_download_url"]
                    break

        return jsonify({
            "update_available": update_available,
            "current": APP_VERSION,
            "latest": latest_ver,
            "download_url": download_url,
            "release_notes": release_notes,
            "is_git": is_git,
        })
    except Exception as e:
        return jsonify({"update_available": False, "current": APP_VERSION,
                        "latest": APP_VERSION, "error": str(e)}), 200


@app.route("/api/git-pull", methods=["POST"])
def git_pull():
    """Run git pull to update a source install."""
    import subprocess
    try:
        repo_dir = os.path.dirname(__file__) or "."
        # Detect default branch
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, timeout=10, cwd=repo_dir,
        )
        default_branch = result.stdout.strip().split("/")[-1] if result.returncode == 0 else "main"
        # Checkout default branch (handles detached HEAD)
        subprocess.run(
            ["git", "checkout", default_branch],
            capture_output=True, text=True, timeout=10, cwd=repo_dir,
        )
        # Pull
        result = subprocess.run(
            ["git", "pull"], capture_output=True, text=True, timeout=30,
            cwd=repo_dir,
        )
        if result.returncode == 0:
            return jsonify({"success": True, "output": result.stdout})
        return jsonify({"success": False, "error": result.stderr or result.stdout})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    import argparse
    import threading
    import webbrowser

    parser = argparse.ArgumentParser(description="Web UI for Chemical Extractor")
    parser.add_argument("--port", type=int, default=5001, help="Port to run on (default: 5001)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")

    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Chemical Search Web UI")
    print(f"  Running at: {url}\n")

    # Auto-open browser after a short delay (to let server start)
    if not args.no_browser:
        def open_browser():
            import time
            time.sleep(0.5)
            if sys.platform == 'win32':
                os.startfile(url)
            else:
                webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    app.run(host=args.host, port=args.port, debug=args.debug)
