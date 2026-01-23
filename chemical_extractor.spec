# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Chemical Extractor.

This creates a one-folder bundle that includes:
- The Flask web app and all dependencies
- The static pubchem_dump_cid_to_cas.tsv lookup file (~87MB)

The 'data/' directory is NOT included - it contains user-specific data
and will be auto-created when the user first runs the app.
"""

import os
from pathlib import Path

block_cipher = None

# Get the directory containing this spec file
SPEC_DIR = Path(SPECPATH)

# Data files to include in the bundle
# Format: (source_path, destination_folder_in_bundle)
datas = [
    # Include the PubChem dump file (CAS->CID lookup table)
    # This is a ~28MB gzipped file required for offline CAS lookups
    (str(SPEC_DIR / 'pubchem_dump_cid_to_cas.tsv.gz'), '.'),
]

# Hidden imports that PyInstaller might miss
hidden_imports = [
    # Flask and web
    'flask',
    'flask.json',
    'jinja2',
    'jinja2.ext',
    'werkzeug',
    'werkzeug.routing',
    'werkzeug.serving',
    'werkzeug.utils',
    'markupsafe',

    # Async HTTP
    'aiohttp',
    'aiohttp.web',
    'multidict',
    'yarl',
    'async_timeout',
    'aiosignal',
    'frozenlist',

    # Data processing
    'pandas',
    'pandas.core',
    'pandas._libs',
    'numpy',

    # HTML parsing
    'bs4',
    'lxml',
    'lxml.etree',
    'lxml.html',

    # HTTP requests
    'requests',
    'urllib3',
    'certifi',
    'charset_normalizer',

    # Browser automation
    'selenium',
    'selenium.webdriver',
    'selenium.webdriver.chrome',
    'selenium.webdriver.chrome.service',
    'selenium.webdriver.chrome.options',
    'selenium.webdriver.common',
    'selenium.webdriver.common.by',

    # Progress bars
    'tqdm',

    # Standard library that might need explicit inclusion
    'sqlite3',
    'hashlib',
    'html',
    'json',
    'uuid',
    'webbrowser',
    'argparse',
    'asyncio',
    'threading',
    'tempfile',
]

# Try to add snappy if available
try:
    import snappy
    hidden_imports.append('snappy')
except ImportError:
    pass

a = Analysis(
    ['web_app.py'],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary modules to reduce size
        'tkinter',
        'matplotlib',
        'scipy',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
        'setuptools',
        'pip',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ChemicalExtractor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window - the Flask server runs in background
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',  # Uncomment if you add an icon file
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ChemicalExtractor',
)
