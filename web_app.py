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

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template_string, jsonify, request, redirect, url_for

# Import functions from the main script
import logging

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
    get_default_browser,
    get_history_fingerprint,
    save_rug_table,
    load_rug_table,
    fetch_cids_from_listkey,
    save_filter_result,
    load_filter_results,
)

logger = logging.getLogger("chemical_extractor")

app = Flask(__name__)


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
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Chemical Search</title>
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
            text-align: center;
            padding: 30px 20px;
            background: linear-gradient(135deg, var(--bg-light) 0%, var(--accent) 100%);
            border-radius: 12px;
            margin-bottom: 25px;
        }
        .search-hero h2 {
            font-size: 1.4rem;
            margin-bottom: 20px;
            border: none;
            color: var(--text);
        }
        .search-status {
            font-size: 0.9rem;
            color: var(--text-dim);
            margin-top: 15px;
        }
    </style>
</head>
<body>
    <header>
        <div class="container">
            <h1>Chemical <span>Search</span></h1>
            <nav>
                <a href="{{ url_for('search') }}" class="{{ 'active' if active_page == 'search' else '' }}">Search</a>
                <a href="{{ url_for('results_page') }}" class="{{ 'active' if active_page == 'results' else '' }}">Results</a>
                <a href="{{ url_for('setup') }}" class="{{ 'active' if active_page == 'setup' else '' }}">Setup</a>
                <span class="nav-divider"></span>
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
    {% block scripts %}{% endblock %}
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
    <h2>Search Your {{ cid_count }} Chemicals</h2>

    {% if selected_search %}
    <button class="btn btn-success btn-large" onclick="combineSelectedSearch('AND', this)" id="btn-main-search">
        Find matching chemicals
    </button>
    <div class="search-status">
        Searching for: <strong>{{ selected_search.name }}</strong>
        <span class="text-dim">({{ selected_search.list_size|default('?') }} results)</span>
    </div>
    {% else %}
    <button class="btn btn-large" onclick="window.open('https://pubchem.ncbi.nlm.nih.gov/', '_blank')">
        Start a New Search on PubChem
    </button>
    <div class="search-status">
        Search for any structure, property, or keyword on PubChem, then come back here.
    </div>
    {% endif %}
</div>

<div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
        <div class="card-header" style="border: none; margin: 0; padding: 0;">
            <h2 style="margin: 0;">Recent PubChem Searches</h2>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
            <span class="text-dim" style="font-size: 0.8rem;" id="auto-refresh-status"></span>
            <button class="btn btn-secondary" style="padding: 5px 12px; font-size: 0.8rem;" onclick="refreshHistory()">
                Refresh
            </button>
        </div>
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

    <div class="actions">
        <button class="btn btn-success" onclick="combineSelectedSearch('AND', this)" disabled id="btn-combine-and">
            Find in My Chemicals (AND)
        </button>
        <button class="btn btn-secondary" onclick="combineSelectedSearch('NOT', this)" disabled id="btn-combine-not">
            Exclude from My Chemicals (NOT)
        </button>
    </div>
    <p class="text-dim" style="margin-top: 15px; font-size: 0.85rem;">
        <strong>AND:</strong> Which of my chemicals match this search? &nbsp;
        <strong>NOT:</strong> Which of my chemicals do NOT match this search?
    </p>
</div>

<div class="card">
    <div class="collapsible-header" onclick="toggleSection('new-search-section')">
        <h2 style="margin: 0; border: none; padding: 0;">New Search</h2>
        <span class="toggle-icon" id="new-search-section-icon">+ expand</span>
    </div>
    <div class="collapsible-content" id="new-search-section">
        <p style="margin-bottom: 15px;">
            To search for specific structures, properties, or keywords:
        </p>
        <ol style="margin-left: 20px; margin-bottom: 15px; color: var(--text-dim);">
            <li>Click the button below to open PubChem</li>
            <li>Search for what you're interested in (e.g., "flammable", a structure, etc.)</li>
            <li>Come back here - your search will appear in the list above</li>
        </ol>
        <button class="btn" onclick="window.open('https://pubchem.ncbi.nlm.nih.gov/', '_blank')">
            Open PubChem
        </button>
    </div>
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
let selectedCacheKey = null;
let searchHistory = [];
let autoRefreshInterval = null;

async function loadHistory() {
    const tbody = document.getElementById('history-body');
    if (!tbody) return;

    try {
        const resp = await fetch('/api/pubchem-history');
        const data = await resp.json();
        searchHistory = data.history || [];

        // Show browser warning if default browser is unsupported
        const warningEl = document.getElementById('browser-warning');
        if (data.browser_warning && warningEl) {
            warningEl.textContent = data.browser_warning;
            warningEl.style.display = 'block';
        } else if (warningEl) {
            warningEl.style.display = 'none';
        }

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
            <tr onclick="selectEntry('${entry.cachekey}')"
                data-cachekey="${entry.cachekey}"
                style="cursor: pointer;"
                class="${idx === 0 ? 'selected' : ''}">
                <td style="text-align: center;">
                    <input type="radio" name="search-select" value="${entry.cachekey}" ${idx === 0 ? 'checked' : ''}
                           onclick="event.stopPropagation(); selectEntry('${entry.cachekey}')">
                </td>
                <td style="max-width: 350px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                    title="${entry.name}">${entry.name}</td>
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

    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-warning" style="text-align: center; padding: 20px;">
            Error loading search history: ${e.message}
        </td></tr>`;
    }
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
        const resp = await fetch('/api/combine-pubchem/' + operation + '?cachekey=' + encodeURIComponent(selectedCacheKey), { method: 'POST' });
        const data = await resp.json();

        if (data.error) {
            alert('Error: ' + data.error);
        } else if (data.pubchem_url) {
            window.open(data.pubchem_url, '_blank');
            if (data.filter_id) {
                window.location.href = '/results?filter_id=' + data.filter_id;
                return;
            }
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

// --- Smart auto-refresh: poll file fingerprint every 5s, full fetch only on change ---
let lastFingerprint = null;
const POLL_INTERVAL_MS = 5000;

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
</script>
{% endblock %}
"""

RESULTS_TEMPLATE = """
{% extends "base" %}
{% block content %}
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
            {% if current_filter.pubchem_url %}
            <a href="{{ current_filter.pubchem_url }}" target="_blank" class="btn btn-secondary" style="font-size: 0.85rem;">Open on PubChem</a>
            {% endif %}
        </div>
    </div>

    {% if filter_results|length > 1 %}
    <div style="margin-bottom: 15px;">
        <label class="text-dim" style="font-size: 0.85rem;">Switch result: </label>
        <select onchange="if(this.value) window.location.href='/results?filter_id='+this.value;" style="font-size: 0.85rem;">
            {% for fr in filter_results %}
            <option value="{{ fr.id }}" {{ 'selected' if fr.id == current_filter.id else '' }}>
                {{ fr.search_name }} ({{ fr.operation }}) — {{ fr.match_count }} matches
            </option>
            {% endfor %}
        </select>
    </div>
    {% endif %}

    {% if filtered_rows %}
    <div style="max-height: 500px; overflow-y: auto;">
        <table>
            <thead>
                <tr>
                    {% for col in columns %}
                    <th>{{ col }}</th>
                    {% endfor %}
                </tr>
            </thead>
            <tbody>
                {% for row in filtered_rows %}
                <tr>
                    {% for col in columns %}
                    <td style="font-size: 0.85rem;">
                        {% if col == 'CID' and row[col] %}
                        {# Ensure CID is rendered as an integer, not a float like 7410.0 #}
                        <a href="https://pubchem.ncbi.nlm.nih.gov/compound/{{ row[col]|int }}" target="_blank"
                           style="color: var(--success);" class="mono">{{ row[col]|int }}</a>
                        {% else %}
                        {{ row[col] if row[col] is not none else '-' }}
                        {% endif %}
                    </td>
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
        <div class="stat-value">{{ cache_stats.not_found if cache_stats else '-' }}</div>
        <div class="stat-label">No PubChem Match</div>
    </div>
    <div class="stat">
        <div class="stat-value">{{ cache_stats.total_cas if cache_stats else '-' }}</div>
        <div class="stat-label">Total Chemicals</div>
    </div>
</div>

{% if setup_complete %}
<div class="alert alert-success">
    <p><strong>Setup Complete!</strong> You have {{ cache_stats.found_cids }} chemicals ready to search.</p>
    <a href="{{ url_for('search') }}" class="btn btn-success" style="margin-top: 10px;">Go to Search</a>
</div>
{% endif %}

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
</div>

<!-- Collapsible: Manage Exports -->
<div class="card">
    <div class="collapsible-header" onclick="toggleSection('manage-exports')">
        <h2 style="margin: 0; border: none; padding: 0;">Manage Exports</h2>
        <span class="toggle-icon" id="manage-exports-icon">+ expand</span>
    </div>
    <div class="collapsible-content" id="manage-exports">
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

    # Get the first search from history for the hero
    selected_search = None
    history = get_pubchem_history_details()
    if history:
        selected_search = history[0]

    return render("search",
        title="Search",
        active_page="search",
        has_cids=has_cids,
        cid_count=cid_count,
        selected_search=selected_search,
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
        cache_valid, _ = is_cid_cache_valid(latest_path)

    results_list = []
    if cache and "results" in cache:
        results_list = list(cache["results"].items())

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
    )


# Legacy routes - redirect to new structure
@app.route("/snapshots")
def snapshots():
    return redirect(url_for("setup"))


@app.route("/results")
def results_page():
    """Results page showing filtered RUG table."""
    filter_id = request.args.get("filter_id")
    rug_table = load_rug_table()
    filter_results = load_filter_results()

    current_filter = None
    filtered_rows = []
    columns = []

    if rug_table and filter_results:
        # Find the requested filter (or most recent)
        if filter_id:
            current_filter = next((f for f in filter_results if f["id"] == filter_id), None)
        if not current_filter:
            current_filter = filter_results[0]

        if current_filter:
            logger.debug(
                "Loading filter %s, %d matching CIDs",
                current_filter["id"],
                len(current_filter.get("matching_cids", [])),
            )
            matching_cid_set = set(current_filter.get("matching_cids", []))
            columns = rug_table.get("columns", [])
            filtered_rows = []
            for row in rug_table.get("rows", []):
                cid_val = row.get("CID")
                if cid_val is None:
                    continue
                try:
                    cid_int = int(cid_val)
                except (TypeError, ValueError):
                    # Handles NaN or other non-integer values gracefully
                    continue
                if cid_int in matching_cid_set:
                    filtered_rows.append(row)

    return render("results",
        title="Results",
        active_page="results",
        rug_table=rug_table,
        filter_results=filter_results,
        current_filter=current_filter,
        filtered_rows=filtered_rows,
        columns=columns,
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
    combined_key = combine_pubchem_cache_keys(user_key, our_key, operation)
    if not combined_key:
        return jsonify({"error": "Failed to combine searches in PubChem."})

    url = f"https://pubchem.ncbi.nlm.nih.gov/#query={combined_key}"
    result = {"pubchem_url": url}

    # Fetch the actual matching CIDs from the combined listkey
    logger.info("Fetching CIDs from combined listkey")
    matching_cids = fetch_cids_from_listkey(combined_key)
    if matching_cids:
        # Look up search name from history
        search_name = "Unknown search"
        history = get_pubchem_history_details()
        for entry in history:
            if entry["cachekey"] == user_key:
                search_name = entry["name"]
                break
        filter_id = save_filter_result(search_name, operation, matching_cids, url)
        result["filter_id"] = filter_id
    else:
        logger.warning("CID fetch failed, returning PubChem URL only")

    return jsonify(result)


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
    return jsonify({
        "history": history,
        "count": len(history),
        "default_browser": default_browser,
        "browser_warning": warning,
    })


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
