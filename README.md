# Chemical Search

A desktop app for searching your RUG chemicals inventory against PubChem. Find which of your chemicals match any structure, property, or keyword search.

![Search Interface](https://img.shields.io/badge/Platform-Windows-blue) ![Python](https://img.shields.io/badge/Python-3.10+-green)

## What It Does

1. **Import** your chemicals from the RUG inventory system
2. **Look up** each chemical in PubChem (maps CAS numbers to PubChem compound IDs)
3. **Search** - perform any search on PubChem, then instantly find which of *your* chemicals match

**Example use cases:**
- "Which of my chemicals are flammable?"
- "Which contain a benzene ring?"
- "Which are NOT classified as toxic?"

## Quick Start

### Windows Users

1. Download the latest release from [Releases](../../releases)
2. Extract the ZIP file
3. Run `ChemicalExtractor.exe`
4. Your browser will open automatically

### First-Time Setup

1. **Import your database**: Click "Fetch from RUG System", and log in.
<img width="498" height="326" alt="image" src="https://github.com/user-attachments/assets/a19ba76c-be1e-4f13-8705-cfec0d1214c3" />

Switch to department chemicals. It may require some fiddling / repetitions of login and view switching, given the instability of the page. You need to get to this view:

<img width="978" height="701" alt="image" src="https://github.com/user-attachments/assets/08de3caf-9c6b-44b2-a6c3-a0b60fba3548" />

Once you see this window, return to the app and click "Continue.

2. **Look up in PubChem**: Click the button and wait for the lookup to complete (this can take a while the first time)
3. **Done!** You'll be redirected to the Search page on Pubchem.

## Using the Search

1. Go to [PubChem](https://pubchem.ncbi.nlm.nih.gov/) and search for anything (structures, properties, keywords)
2. Come back to Chemical Search - your search appears in the list automatically
3. Click **"Find in My Chemicals (AND)"** to see which of your chemicals match

The search list auto-refreshes every 30 seconds, so just search on PubChem and switch back.

### Search Operations

- **AND**: Which of my chemicals match this search? (intersection)
- **NOT**: Which of my chemicals do NOT match this search? (exclusion)

## Technical Details

### How It Works

The app uses a multi-layer lookup strategy for CAS → PubChem CID mapping:

1. **Local PubChem dump** (~4M mappings, instant) - bundled with the app
2. **Local cache** - remembers previous API lookups
3. **CTS API** - Chemical Translation Service (batch, fast)
4. **PubChem API** - direct lookup (rate-limited fallback)

Search history is read from Firefox's localStorage for PubChem, allowing seamless integration with your browser searches.

### Data Storage

All data is stored next to the executable:
- `data/snapshots/` - HTML exports from RUG
- `data/cid_cache.json` - PubChem lookup results
- `data/latest.txt` - pointer to current snapshot
- `chemical_extractor.log` - debug log (Windows only)

### Building from Source

```bash
# Clone the repository
git clone https://github.com/mf-rug/rug_chemsearch_cl.git
cd rug_chemsearch_cl

# Install dependencies
pip install -r requirements.txt

# Run directly
python web_app.py

# Or build the Windows executable
pip install pyinstaller
pyinstaller chemical_extractor.spec
```

The built app will be in `dist/ChemicalExtractor/`.

### Requirements

- Python 3.10+
- Firefox (for PubChem search history integration)
- Chrome + ChromeDriver (for RUG database fetching)

### Dependencies

- Flask - web UI
- pandas - data processing
- BeautifulSoup + lxml - HTML parsing
- aiohttp - async HTTP requests
- selenium - browser automation
- cramjam - snappy decompression for Firefox localStorage

## Troubleshooting

### "Button hangs when clicking Look up in PubChem"

The first lookup loads a 4M-line gzipped file which can take 30+ seconds. Check `chemical_extractor.log` next to the exe for progress.

### "No PubChem searches found"

Make sure you're using Firefox to search on PubChem. The app reads search history from Firefox's localStorage.

### Can't close the app on Windows

Use the "Quit App" button in the top-right, or use Task Manager (`Ctrl+Shift+Esc`) to end the process.

## License

MIT

