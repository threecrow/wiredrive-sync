# Wiredrive Sync

Wiredrive Sync 1.7 is a desktop synchronization client for mapping local folders to Wiredrive folders.

## Current feature set

- Multiple independent sync mappings
- Upload-only, download-only, and two-way mappings
- Recursive subfolder synchronization
- Remote Wiredrive folder browser
- Automatic remote subfolder creation
- True-sync reconciliation to avoid duplicate uploads
- Fast reconciliation of previously synced media
- Newest-files-first transfer priority
- Upload/download speed, percentage, and ETA
- Global search
- Mapping edit, pause, sync-now, and remove controls
- Persistent sync state outside the application bundle
- AppleDouble / hidden-file filtering on macOS
- Native macOS Cocoa/WebKit window
- Exact dashboard Files Synced count

Current application version: **1.7**  
macOS build number: **170**

## Repository layout

```text
Wiredrive-Sync/
├── app/                  Core Flask sync engine + HTML/CSS/JS interface
├── packaging/
│   └── macos/            macOS app-wrapper source and icon
├── scripts/
│   ├── build_macos_app.py
│   └── run_dev.command
├── CHANGELOG-NOTES.md    Development/release notes from the application
├── .gitignore
└── README.md
```

## Local development on macOS

From Terminal:

```bash
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The development server runs locally and exposes the same application UI used by the desktop wrapper.

You can also double-click:

```text
scripts/run_dev.command
```

## Build the macOS application bundle

From the repository root:

```bash
python3 scripts/build_macos_app.py
```

The generated application is written to:

```text
dist/Wiredrive Sync.app
```

The app stores user/runtime state separately under:

```text
~/Library/Application Support/Wiredrive Sync/
```

That directory is intentionally **not** part of the repository.

## Opening this project in Nova

1. Extract this repository folder.
2. Open **Nova**.
3. Choose **File → Open…**
4. Select the `Wiredrive-Sync-1.7-GitHub-Project` folder.
5. Use Nova's Source Control sidebar to initialize Git if the folder is not already a repository.
6. Add your GitHub remote.
7. Commit the project.
8. Push `main` to GitHub.

A sensible first commit message is:

```text
Initial Wiredrive Sync 1.7 source release
```

## Distribution

The repository contains source and packaging files. Built `.app`, `.zip`, `.dmg`, `.exe`, and similar release artifacts should normally be attached to GitHub Releases rather than committed into the source tree.
