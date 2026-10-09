# PC Optimizer

[中文](README.zh-CN.md) | English

A local disk audit tool for Windows. It shows what is stored on each drive, which items can be cleaned and why, and exports a per-project inventory you can hand to an AI agent such as Cursor. Everything runs locally: no network access, no data upload.

**System files are only measured, never deleted.** Project source code is never cleaned by this tool either.

## Download

1. Download `PCOptimizer-v*-win64.zip` from the Releases page.
2. Unzip it and run `PCOptimizer.exe`.
3. Open "Storage", pick a drive, and click start.

The exe is not code-signed, so SmartScreen may warn on first launch: click "More info", then "Run anyway". The Microsoft Edge WebView2 runtime is required; it ships with Windows 11 and recent Windows 10, and the app shows a download link if it is missing. Verify the download against `CHECKSUMS.txt` (SHA256).

The interface is in Chinese.

## What it does

- **Overview**: free space per drive, file-type breakdown, how old the last scan is, one-click rescan.
- **Storage audit**: drill-down directory map; every finding comes with a reason and a suggestion.
- **Projects**: splits the disk by project, shows rebuildable folders (`node_modules`, `target`, ...) and git status, and exports a report for AI agents.
- **Cleanup**: known caches (browsers, npm, pip, ...).
- **Performance**: process and memory advice.

## Safety

Never deleted: `Windows`, `Program Files`, `ProgramData`; drive roots and `System Volume Information`, `$Recycle.Bin`, `Recovery`, `Boot`; `pagefile.sys`, `hiberfil.sys`, `swapfile.sys`; files with the system attribute; junctions and symlinks; project directories; conda/anaconda, `node_modules`, `.pnpm-store`, `DockerData`; and any path you add in Settings.

| Action | Meaning |
|--------|---------|
| Clean directly | Regenerable cache. Deleted for real, not restorable. |
| Recycle bin | Old installers, duplicates, empty folders. You tick each one; restorable. Refused if the drive has the recycle bin disabled or the file exceeds its capacity, so nothing is permanently deleted by accident. |
| Advice only / read-only | VM disks, large videos, chat files, system folders. Never touched. |

Every path is re-checked right before execution; earlier scan results are not trusted.

## Hand-off to an AI agent

"Projects" page, "Export for AI" writes `exports/handoff-<time>/REPORT.md` and `inventory.json`: paths, types, sizes, rebuildable folders, git state and risk notes. No file contents, no delete scripts.

## Run from source

Python 3.10 to 3.13.

```bat
pip install -r requirements.txt
python main.py
```

## Build the exe

```bat
build_release.bat
```

Creates a `.venv`, installs dependencies, runs tests, builds, checks that the window opens, and prints the full path of the zip. Set `PCOPT_PYTHON` to a `python.exe` if Python is not found automatically. The version comes from the `VERSION` file.

## Development

```bat
pip install -r requirements-dev.txt
python -m pytest tests
```

To preview the UI in a browser with fake data: run `python -m http.server 8770` and open `http://127.0.0.1:8770/web/index.html?demo=1`.

Layout: `core/` logic (scan, classify, safety, execute), `app/` window bridge, `web/` UI, `tests/`.

## License

MIT. See `LICENSE`.
