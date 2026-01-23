#!/usr/bin/env python3
"""
Web UI for Chemical Data Extractor.

A simple Flask app providing a browser-based interface to:
- View and manage HTML snapshots
- Run CID lookups
- View results and open PubChem searches
"""

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template_string, jsonify, request, redirect, url_for

# Import functions from the main script
from extract_chemicals import (
    DATA_DIR,
    SNAPSHOTS_DIR,
    CID_CACHE_FILE,
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
)

app = Flask(__name__)

# ============================================================================
# HTML Templates (inline for simplicity)
# ============================================================================

BASE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Chemical Extractor</title>
    <style>
        :root {
            --bg: #1a1a2e;
            --bg-light: #16213e;
            --accent: #0f3460;
            --highlight: #e94560;
            --text: #eee;
            --text-dim: #888;
            --success: #4ecca3;
            --warning: #ffc107;
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
            padding: 20px 0;
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
        nav a {
            color: var(--text);
            text-decoration: none;
            margin-left: 20px;
            padding: 8px 16px;
            border-radius: 4px;
            transition: background 0.2s;
        }
        nav a:hover { background: var(--accent); }
        nav a.active { background: var(--highlight); }

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
        .btn-secondary { background: var(--accent); }
        .btn-success { background: var(--success); }

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
    </style>
</head>
<body>
    <header>
        <div class="container">
            <h1>Chemical <span>Extractor</span></h1>
            <nav>
                <a href="{{ url_for('index') }}" class="{{ 'active' if active_page == 'home' else '' }}">Dashboard</a>
                <a href="{{ url_for('snapshots') }}" class="{{ 'active' if active_page == 'snapshots' else '' }}">Exports</a>
                <a href="{{ url_for('results') }}" class="{{ 'active' if active_page == 'results' else '' }}">PubChem Results</a>
                <a href="{{ url_for('combine') }}" class="{{ 'active' if active_page == 'combine' else '' }}">Combine</a>
            </nav>
        </div>
    </header>
    <main class="container">
        {% block content %}{% endblock %}
    </main>
    <script>
        // Helper for async actions with loading state
        async function runAction(url, btn) {
            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> Processing...';

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
    </script>
    {% block scripts %}{% endblock %}
</body>
</html>
"""

INDEX_TEMPLATE = """
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
        <div class="stat-value">{{ cache_stats.not_found if cache_stats else '-' }}</div>
        <div class="stat-label">No PubChem Match</div>
    </div>
    <div class="stat">
        <div class="stat-value">{{ cache_stats.total_cas if cache_stats else '-' }}</div>
        <div class="stat-label">Total Chemicals</div>
    </div>
</div>

<div class="card">
    <div class="card-header">
        <h2>Current Database Export</h2>
        <span class="info">?<span class="tip">A database export is a saved HTML file containing the chemicals table from the RUG inventory system. Each export is a point-in-time snapshot of the database.</span></span>
    </div>
    {% if latest_snapshot %}
    <p><strong>Active export:</strong> <span class="mono">{{ latest_snapshot.name }}</span></p>
    <p class="text-dim">{{ latest_snapshot.timestamp.strftime('%Y-%m-%d %H:%M:%S') }} &bull; {{ (latest_snapshot.size / 1024)|round(1) }} KB</p>
    {% if cache_valid %}
    <p class="text-success" style="margin-top: 10px;">PubChem lookups are cached and ready</p>
    {% else %}
    <p class="text-warning" style="margin-top: 10px;">PubChem lookups needed (new export or no cache yet)</p>
    {% endif %}
    {% else %}
    <p class="text-dim">No database exports found. Use "Fetch from RUG" below or upload an HTML file.</p>
    {% endif %}
</div>

<div class="card">
    <div class="card-header">
        <h2>Actions</h2>
    </div>
    <div class="actions">
        {% if latest_snapshot %}
        <button class="btn" onclick="runAction('{{ url_for('run_extraction') }}', this)">
            Look up in PubChem
            <span class="info" style="margin-left: 4px; background: rgba(255,255,255,0.2);">?<span class="tip">Extracts CAS numbers from the current database export and looks them up in PubChem to find matching compound IDs (CIDs). Uses cached results when available.</span></span>
        </button>
        <button class="btn btn-secondary" onclick="runAction('{{ url_for('run_extraction') }}?refresh_cids=1', this)">
            Re-lookup All
            <span class="info" style="margin-left: 4px;">?<span class="tip">Forces a fresh lookup of all CAS numbers in PubChem, ignoring any cached results. Use this if you think PubChem data may have been updated.</span></span>
        </button>
        {% endif %}
        <a href="{{ url_for('snapshots') }}" class="btn btn-secondary">
            Manage Exports
            <span class="info" style="margin-left: 4px;">?<span class="tip">View all saved database exports, upload new HTML files, or switch between different exports.</span></span>
        </a>
        {% if cache_stats and cache_stats.found_cids > 0 %}
        <button class="btn btn-success" onclick="runAction('{{ url_for('open_pubchem') }}', this)">
            Open in PubChem
            <span class="info" style="margin-left: 4px; background: rgba(0,0,0,0.2);">?<span class="tip">Opens all matched compounds in PubChem's web interface, where you can browse, filter, and analyze the chemical data.</span></span>
        </button>
        {% endif %}
        {% if cache_stats and cache_stats.found_cids > 0 %}
        <a href="{{ url_for('combine') }}" class="btn btn-secondary">
            Combine with Firefox Search
            <span class="info" style="margin-left: 4px;">?<span class="tip">Combine your RUG chemicals with a PubChem search from Firefox (e.g., find which of your chemicals are "flammable").</span></span>
        </a>
        {% endif %}
    </div>
</div>

<div class="card">
    <div class="card-header">
        <h2>Fetch from RUG</h2>
        <span class="info">?<span class="tip">Automatically logs into the RUG chemicals inventory system and downloads a fresh export of all chemicals. Requires you to complete the login (including MFA) in the browser window that opens.</span></span>
    </div>
    <div class="actions" id="rug-actions">
        <button class="btn btn-secondary" id="btn-open-login" onclick="openRugLogin(this)">
            Open RUG Login
        </button>
        <button class="btn btn-success" id="btn-continue" onclick="continueAfterLogin(this)" style="display: none;">
            Continue
        </button>
    </div>
    <p class="text-dim" style="margin-top: 12px; font-size: 0.85rem;" id="rug-instructions">
        Opens Chrome for you to log in. After login, click Continue to fetch all chemicals.
    </p>
    <div id="browser-refresh-status" style="margin-top: 15px; display: none;">
        <p><span class="loading" id="status-spinner"></span> <span id="refresh-status-text">Starting browser...</span></p>
    </div>
</div>

{% if cache_created %}
<div class="card">
    <div class="card-header">
        <h2>Cache Status</h2>
        <span class="info">?<span class="tip">PubChem lookup results are cached locally to avoid repeated API calls. The cache is automatically invalidated when you switch to a different database export.</span></span>
    </div>
    <p><strong>Last lookup:</strong> {{ cache_created }}</p>
    <p class="text-dim"><strong>Based on:</strong> <span class="mono">{{ cache_source }}</span></p>
</div>
{% endif %}
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
        alert('No active session. Please click "Open RUG Login" first.');
        return;
    }

    const statusDiv = document.getElementById('browser-refresh-status');
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
</script>
{% endblock %}
"""

SNAPSHOTS_TEMPLATE = """
{% extends "base" %}
{% block content %}
<div class="card">
    <div class="card-header">
        <h2>Upload Export</h2>
        <span class="info">?<span class="tip">Upload an HTML file saved from the RUG chemicals inventory. The file should contain the chemicals table with CAS numbers.</span></span>
    </div>
    <form action="{{ url_for('upload_snapshot') }}" method="post" enctype="multipart/form-data" style="display: flex; gap: 10px; align-items: center;">
        <input type="file" name="file" accept=".html,.htm" required>
        <button type="submit" class="btn">Upload</button>
    </form>
</div>

<div class="card">
    <div class="card-header">
        <h2>Database Exports</h2>
        <span class="info">?<span class="tip">Each export is a saved HTML file from the RUG chemicals database. The "Active" export is the one currently used for PubChem lookups. You can switch between exports to compare different points in time.</span></span>
    </div>
    {% if snapshots %}
    <table>
        <thead>
            <tr>
                <th>Export Date</th>
                <th>Filename</th>
                <th>Size</th>
                <th>Status</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody>
        {% for snap in snapshots %}
            <tr>
                <td>{{ snap.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
                <td class="mono">{{ snap.path.name }}</td>
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
                    <button class="btn btn-secondary" style="padding: 5px 10px; font-size: 0.8rem;"
                            onclick="runAction('{{ url_for('set_latest', filename=snap.path.name) }}', this)">
                        Use This Export
                    </button>
                    {% endif %}
                </td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p class="text-dim">No database exports found. Upload an HTML file above or use "Fetch from RUG" on the Dashboard.</p>
    {% endif %}
</div>
{% endblock %}
"""

RESULTS_TEMPLATE = """
{% extends "base" %}
{% block content %}
{% if not cache %}
<div class="alert alert-info">
    <p>No PubChem lookup results yet. Click "Look up in PubChem" on the Dashboard to get started.</p>
    <a href="{{ url_for('index') }}" class="btn" style="margin-top: 10px;">Go to Dashboard</a>
</div>
{% else %}
<div class="stats">
    <div class="stat">
        <div class="stat-value text-success">{{ cache.stats.found_cids }}</div>
        <div class="stat-label">Matched in PubChem</div>
    </div>
    <div class="stat">
        <div class="stat-value text-warning">{{ cache.stats.not_found }}</div>
        <div class="stat-label">No Match Found</div>
    </div>
    <div class="stat">
        <div class="stat-value">{{ cache.stats.total_cas }}</div>
        <div class="stat-label">Total Chemicals</div>
    </div>
</div>

<div class="card">
    <div class="card-header">
        <h2>Export &amp; View</h2>
    </div>
    <div class="actions">
        <button class="btn btn-success" onclick="runAction('{{ url_for('open_pubchem') }}', this)">
            Open All in PubChem
            <span class="info" style="margin-left: 4px; background: rgba(0,0,0,0.2);">?<span class="tip">Opens all {{ cache.stats.found_cids }} matched compounds in PubChem's web interface for browsing, filtering, and analysis.</span></span>
        </button>
        <a href="{{ url_for('download_cids') }}" class="btn btn-secondary">
            Download CIDs
            <span class="info" style="margin-left: 4px;">?<span class="tip">Download a plain text file with one PubChem CID per line. Useful for importing into other tools.</span></span>
        </a>
        <a href="{{ url_for('download_mapping') }}" class="btn btn-secondary">
            Download Full Mapping
            <span class="info" style="margin-left: 4px;">?<span class="tip">Download a CSV file with CAS numbers, lookup status, and PubChem CIDs. Includes both matched and unmatched chemicals.</span></span>
        </a>
    </div>
</div>

<div class="card">
    <div class="card-header">
        <h2>CAS → PubChem Mapping</h2>
        <span class="info">?<span class="tip">Shows the mapping from CAS Registry Numbers (from your database export) to PubChem Compound IDs (CIDs). Click any CID to view the compound in PubChem.</span></span>
    </div>
    <div style="margin-bottom: 10px;">
        <label>Show: </label>
        <select id="filter-select" onchange="filterTable()">
            <option value="all">All chemicals ({{ cache.stats.total_cas }})</option>
            <option value="found">Matched in PubChem ({{ cache.stats.found_cids }})</option>
            <option value="not_found">No match found ({{ cache.stats.not_found }})</option>
        </select>
    </div>
    <div id="results-table">
        <table>
            <thead>
                <tr>
                    <th>CAS Number <span class="info">?<span class="tip">CAS Registry Number - a unique identifier for chemical substances assigned by the Chemical Abstracts Service.</span></span></th>
                    <th>Status</th>
                    <th>PubChem CID <span class="info">?<span class="tip">PubChem Compound ID - a unique identifier in NCBI's PubChem database. Click to view full compound details.</span></span></th>
                </tr>
            </thead>
            <tbody id="results-body">
            {% for cas, data in results[:500] %}
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
    {% if cache.stats.total_cas > 500 %}
    <p class="text-dim" style="margin-top: 10px;">Showing first 500 of {{ cache.stats.total_cas }} results.</p>
    {% endif %}
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
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
</script>
{% endblock %}
"""

COMBINE_TEMPLATE = """
{% extends "base" %}
{% block content %}
{% if not has_cids %}
<div class="alert alert-info">
    <p>No PubChem results yet. Run a CID lookup on the Dashboard first.</p>
    <a href="{{ url_for('index') }}" class="btn" style="margin-top: 10px;">Go to Dashboard</a>
</div>
{% else %}
<div class="card">
    <div class="card-header">
        <h2>Combine with Firefox PubChem Search</h2>
        <span class="info">?<span class="tip">Select a PubChem search from your Firefox history and combine it with your {{ cid_count }} matched RUG chemicals using AND, OR, or NOT operations.</span></span>
    </div>

    <p style="margin-bottom: 15px; font-size: 0.9rem;">
        Select a search from your Firefox PubChem history below, then combine it with your <strong>{{ cid_count }}</strong> matched chemicals.
        <a href="https://pubchem.ncbi.nlm.nih.gov/" target="_blank" style="color: var(--highlight);">Open PubChem</a> in Firefox to add new searches.
    </p>

    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
        <span class="text-dim" style="font-size: 0.85rem;">Firefox PubChem Search History:</span>
        <button class="btn btn-secondary" style="padding: 5px 10px; font-size: 0.8rem;" onclick="refreshFirefoxHistory()">
            Refresh
        </button>
    </div>

    <div id="firefox-history-container" style="max-height: 350px; overflow-y: auto; margin-bottom: 20px;">
        <table id="firefox-history-table">
            <thead>
                <tr>
                    <th style="width: 30px;"></th>
                    <th>Search Query</th>
                    <th>Type</th>
                    <th>Results</th>
                    <th>Date</th>
                    <th>Link</th>
                </tr>
            </thead>
            <tbody id="firefox-history-body">
                <tr><td colspan="6" class="text-dim" style="text-align: center; padding: 20px;">
                    <span class="loading" style="width: 14px; height: 14px;"></span> Loading history...
                </td></tr>
            </tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-header">
        <h2>Combine Operations</h2>
    </div>
    <p style="margin-bottom: 15px; font-size: 0.9rem;">
        Choose how to combine your RUG chemicals with the selected Firefox search:
    </p>
    <div class="actions" id="combine-actions">
        <button class="btn btn-success" onclick="combineSelectedSearch('AND', this)" disabled id="btn-combine-and">
            AND (Intersection)
        </button>
        <button class="btn btn-secondary" onclick="combineSelectedSearch('OR', this)" disabled id="btn-combine-or">
            OR (Union)
        </button>
        <button class="btn btn-secondary" onclick="combineSelectedSearch('NOT', this)" disabled id="btn-combine-not">
            NOT (Exclude)
        </button>
    </div>
    <div style="margin-top: 20px; font-size: 0.85rem; color: var(--text-dim);">
        <p><strong>AND:</strong> Shows chemicals in BOTH your RUG inventory AND the selected search. Example: "Which of my chemicals are flammable?"</p>
        <p style="margin-top: 8px;"><strong>OR:</strong> Shows chemicals in EITHER your RUG inventory OR the selected search. Combines both lists.</p>
        <p style="margin-top: 8px;"><strong>NOT:</strong> Shows chemicals in your RUG inventory that are NOT in the selected search. Example: "Which of my chemicals are NOT toxic?"</p>
    </div>
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
let selectedCacheKey = null;
let firefoxHistory = [];

async function loadFirefoxHistory() {
    const tbody = document.getElementById('firefox-history-body');
    if (!tbody) return;

    try {
        const resp = await fetch('/api/firefox-pubchem-history');
        const data = await resp.json();
        firefoxHistory = data.history || [];

        if (firefoxHistory.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-dim" style="text-align: center; padding: 20px;">
                No PubChem searches found in Firefox.<br>
                <a href="https://pubchem.ncbi.nlm.nih.gov/" target="_blank" style="color: var(--highlight);">Search on PubChem</a> using Firefox, then click Refresh.
            </td></tr>`;
            updateCombineButtons();
            return;
        }

        tbody.innerHTML = firefoxHistory.map((entry, idx) => `
            <tr onclick="selectHistoryEntry('${entry.cachekey}')"
                data-cachekey="${entry.cachekey}"
                style="cursor: pointer;"
                class="${idx === 0 ? 'selected' : ''}">
                <td style="text-align: center;">
                    <input type="radio" name="history-select" value="${entry.cachekey}" ${idx === 0 ? 'checked' : ''}
                           onclick="event.stopPropagation(); selectHistoryEntry('${entry.cachekey}')">
                </td>
                <td style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                    title="${entry.name}">${entry.name}</td>
                <td><span class="badge badge-dim">${entry.domain || entry.type || 'compound'}</span></td>
                <td>${entry.list_size ? entry.list_size.toLocaleString() : '-'}</td>
                <td class="mono" style="font-size: 0.8rem;">${entry.timestamp_display}</td>
                <td><a href="${entry.url}" target="_blank" onclick="event.stopPropagation();"
                       style="color: var(--highlight); font-size: 0.85rem;">View</a></td>
            </tr>
        `).join('');

        // Select first entry by default
        if (firefoxHistory.length > 0) {
            selectedCacheKey = firefoxHistory[0].cachekey;
        }
        updateCombineButtons();

    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-warning" style="text-align: center; padding: 20px;">
            Error loading history: ${e.message}
        </td></tr>`;
    }
}

function selectHistoryEntry(cachekey) {
    selectedCacheKey = cachekey;

    document.querySelectorAll('#firefox-history-body tr').forEach(row => {
        const isSelected = row.dataset.cachekey === cachekey;
        row.classList.toggle('selected', isSelected);
        const radio = row.querySelector('input[type="radio"]');
        if (radio) radio.checked = isSelected;
    });

    updateCombineButtons();
}

function updateCombineButtons() {
    const hasSelection = selectedCacheKey !== null;
    const btnAnd = document.getElementById('btn-combine-and');
    const btnOr = document.getElementById('btn-combine-or');
    const btnNot = document.getElementById('btn-combine-not');
    if (btnAnd) btnAnd.disabled = !hasSelection;
    if (btnOr) btnOr.disabled = !hasSelection;
    if (btnNot) btnNot.disabled = !hasSelection;
}

async function combineSelectedSearch(operation, btn) {
    if (!selectedCacheKey) {
        alert('Please select a search from the history first.');
        return;
    }

    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="loading"></span> Combining...';

    try {
        const resp = await fetch('/api/combine-pubchem/' + operation + '?cachekey=' + encodeURIComponent(selectedCacheKey), { method: 'POST' });
        const data = await resp.json();

        if (data.error) {
            alert('Error: ' + data.error);
        } else if (data.pubchem_url) {
            window.open(data.pubchem_url, '_blank');
        }
    } catch (e) {
        alert('Error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
    }
}

async function refreshFirefoxHistory() {
    const tbody = document.getElementById('firefox-history-body');
    if (tbody) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-dim" style="text-align: center; padding: 20px;">
            <span class="loading" style="width: 14px; height: 14px;"></span> Refreshing...
        </td></tr>`;
    }
    selectedCacheKey = null;
    await loadFirefoxHistory();
}

document.addEventListener('DOMContentLoaded', loadFirefoxHistory);
</script>
{% endblock %}
"""

# ============================================================================
# Template rendering helper
# ============================================================================

TEMPLATES = {
    "base": BASE_TEMPLATE,
    "index": INDEX_TEMPLATE,
    "snapshots": SNAPSHOTS_TEMPLATE,
    "results": RESULTS_TEMPLATE,
    "combine": COMBINE_TEMPLATE,
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
    """Dashboard page."""
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
        cache_valid, _ = is_cid_cache_valid(latest_path)

    return render("index",
        title="Dashboard",
        active_page="home",
        snapshot_count=len(snapshots),
        cache_stats=cache.get("stats") if cache else None,
        cache_created=cache.get("created", "")[:19] if cache else None,
        cache_source=Path(cache.get("source_html", "")).name if cache else None,
        latest_snapshot=latest_snapshot,
        cache_valid=cache_valid,
    )


@app.route("/snapshots")
def snapshots():
    """Snapshots management page."""
    return render("snapshots",
        title="Snapshots",
        active_page="snapshots",
        snapshots=list_snapshots(),
    )


@app.route("/results")
def results():
    """Results view page."""
    cache = load_cid_cache()

    results_list = []
    if cache and "results" in cache:
        results_list = list(cache["results"].items())

    return render("results",
        title="Results",
        active_page="results",
        cache=cache,
        results=results_list,
    )


@app.route("/combine")
def combine():
    """Combine with Firefox PubChem search page."""
    cache = load_cid_cache()

    has_cids = False
    cid_count = 0
    if cache and "results" in cache:
        cids = [r for r in cache["results"].values() if r.get("cid")]
        has_cids = len(cids) > 0
        cid_count = len(cids)

    return render("combine",
        title="Combine",
        active_page="combine",
        has_cids=has_cids,
        cid_count=cid_count,
    )


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
        except Exception as e:
            return jsonify({"error": str(e)})

    return jsonify({"redirect": url_for("results")})


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
        return redirect(url_for("snapshots"))

    file = request.files["file"]
    if file.filename == "":
        return redirect(url_for("snapshots"))

    # Save to snapshots directory
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Search_{timestamp}.html"
    filepath = SNAPSHOTS_DIR / filename

    file.save(filepath)

    # Update pointer to new snapshot
    update_latest_pointer(filepath)

    return redirect(url_for("snapshots"))


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


@app.route("/api/combine-pubchem/<operation>", methods=["POST"])
def combine_pubchem(operation):
    """
    Combine our CIDs with a selected Firefox PubChem search.

    Operations:
    - AND: Intersection (chemicals in BOTH our list AND selected search)
    - OR: Union (chemicals in EITHER our list OR selected search)
    - NOT: Difference (chemicals in our list but NOT in selected search)

    Query params:
    - cachekey: The PubChem cache key of the search to combine with
    """
    operation = operation.upper()
    if operation not in ("AND", "OR", "NOT"):
        return jsonify({"error": f"Invalid operation: {operation}. Use AND, OR, or NOT."})

    # Get the selected cache key from query params
    user_key = request.args.get("cachekey")
    if not user_key:
        # Fallback to latest Firefox search if no key provided
        user_key = get_latest_pubchem_history_cachekey()
        if not user_key:
            return jsonify({
                "error": "No search selected and could not find PubChem search in Firefox."
            })

    # Get our CIDs
    cache = load_cid_cache()
    if not cache or "results" not in cache:
        return jsonify({"error": "No CID results found. Run 'Look up in PubChem' first."})

    cids = [str(r["cid"]) for r in cache["results"].values() if r.get("cid")]
    if not cids:
        return jsonify({"error": "No CIDs found in results."})

    # Upload our CIDs to PubChem cache
    our_key = upload_cids_to_pubchem_cache(cids)
    if not our_key:
        return jsonify({"error": "Failed to upload CIDs to PubChem cache."})

    # Combine the two cache keys
    combined_key = combine_pubchem_cache_keys(user_key, our_key, operation)
    if not combined_key:
        return jsonify({"error": "Failed to combine searches in PubChem."})

    url = f"https://pubchem.ncbi.nlm.nih.gov/#query={combined_key}"
    return jsonify({"pubchem_url": url})


@app.route("/api/firefox-pubchem-history")
def firefox_pubchem_history():
    """Get all PubChem search history from Firefox localStorage."""
    history = get_pubchem_history_details()
    return jsonify({"history": history, "count": len(history)})


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
    print(f"\n  Chemical Extractor Web UI")
    print(f"  Running at: {url}\n")

    # Auto-open browser after a short delay (to let server start)
    if not args.no_browser:
        def open_browser():
            import time
            time.sleep(0.5)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    app.run(host=args.host, port=args.port, debug=args.debug)
