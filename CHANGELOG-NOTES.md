# Wiredrive Sync 1.1 Beta

A local macOS-oriented Wiredrive synchronization service reverse-engineered from the user's authenticated Wiredrive browser workflows.

## New in 1.0 Beta

- Upload Only, Download Only, and Two-Way directions.
- Polls the selected Wiredrive project/folder inventory.
- Identifies remote media by Wiredrive asset ID, size, and modified revision.
- Persistent SQLite `sync_assets` state prevents re-downloading completed assets after restart.
- Downloads to `.wdpartial` and atomically renames only after the expected byte count is received.
- Resumes partial downloads using HTTP Range when supported.
- Downloaded media is suppressed from the local upload watcher to prevent two-way loops.
- Existing unrelated local files are never silently overwritten; a Wiredrive asset suffix is used instead.
- Remote deletion mirroring is intentionally disabled.
- Manual `Sync Now` and `Check Remote Folder` controls.

## Important Beta limitations

- This build still identifies the Wiredrive destination by Project ID and Folder ID. It does not yet expose a complete remote project-tree picker.
- Conflict handling is conservative: no automatic destructive overwrite or deletion.
- Wiredrive is an undocumented/private browser workflow, so server-side behavior can change.


## 1.1 Beta — Remote browsing
The sync destination can now be selected visually. The browser loads the authenticated account's `projectList`, then reads a project's embedded `oTreeContent` folder hierarchy. Selecting a folder fills Project ID, Folder ID, and the human-readable destination path automatically. Manual IDs remain available under Advanced Wiredrive IDs.


## 1.2 Beta — Recursive folder synchronization
Mappings can now synchronize the complete hierarchy beneath the selected root. Local subfolders are watched recursively. When a local file is found in a relative path that does not exist remotely, Wiredrive Sync creates each missing Wiredrive folder first using Wiredrive's own `createFolder` endpoint, records the returned folder IDs, then uploads the file to the correct folder.

For download/two-way mappings, the client walks the selected Wiredrive folder tree, creates missing local directories, polls every descendant folder for assets, and downloads into the corresponding relative path. Deletion mirroring remains disabled.


## 1.3 Beta — True Sync reconciliation
Two-way sync now reconciles existing files before transferring data. A remote asset is considered the counterpart of a local file when it resolves to the same relative folder, filename, and exact byte size. The client records that Wiredrive asset ID against the existing local file instead of downloading a renamed duplicate.

Before uploading, the corresponding Wiredrive folder is inspected. If the same filename and byte size already exists, the upload is skipped and that remote asset is recorded as synchronized. Successful uploads are written to the sync database immediately, preventing the remote poller from downloading the application's own upload back to the source folder.

Same-name files with different byte sizes remain conflicts and are never silently overwritten. Deletion mirroring remains disabled.


## 1.4 Beta — Live transfer telemetry
Active upload and download rows now report percentage complete, transferred bytes versus total, current transfer speed, and estimated time remaining. Progress percentages are also overlaid directly on the transfer bars.


## 1.4.1 Beta — Progress bar cleanup
Removed the percentage overlay from inside transfer progress bars. Percentage, bytes, speed, and ETA remain in the transfer telemetry line above the bar.


## 1.5 Beta — Dashboard redesign and multiple mappings
The interface has been rebuilt around Dashboard, Mappings, Transfers, Activity, Remote Browser, Settings, and About views. Multiple independent mappings are now first-class: add, edit, enable/pause, sync, and delete mappings without replacing other mappings. Each mapping preserves its own local path, Wiredrive destination, direction, recursive behavior, create-missing-folder behavior, observer, jobs, and sync state.


## 1.5.1 Beta — Mapping deletion
Mapping deletion is now explicit in the interface. Edit Mapping includes a red **Delete Mapping** button, and the Mappings table has a labeled **Delete** action. Deletion removes the mapping, its queued/completed job history, and local sync-state records, but never deletes local media or Wiredrive media. Active transfers must finish before a mapping can be deleted.


## 1.5.2 Beta — Dashboard visual hierarchy
No sync-engine behavior changed in this release. Dashboard sections now have clearer visual identities:
- Sync Mappings uses a blue configuration surface and stronger structural accent.
- Active Transfers uses a darker operational-console surface with emphasized progress bars.
- Activity Stream is rendered visually as a timeline.
- Scheduler uses a green service-status treatment.
- System Health uses a subdued diagnostic treatment.
- Metric cards have purpose-specific accents and the major dashboard sections have more separation.


## 1.5.3 Beta — Faster default polling
Fresh configurations now default to a 15-second Wiredrive remote check interval and a 5-second local file stability wait. Existing saved user settings are preserved when upgrading.


## 1.5.4 Beta — Account card fit
Long Wiredrive login names now wrap cleanly inside the connected-account card instead of overflowing its bounds. The hostname and account controls remain constrained to the card width as well.


## 1.5.5 Beta — Dashboard mapping removal
Each mapping card on the Dashboard now includes an explicit **Remove** action. It uses the same confirmation and safe-delete behavior as the Mappings page: the mapping, queue/history records, and local sync-state entries are removed, but local media and Wiredrive media are never deleted.


## 1.5.6 Beta — Fast reconciliation and persistent upgrade state
Runtime configuration and sync state now live in `~/Library/Application Support/Wiredrive Sync` on macOS rather than inside each extracted version folder. Future upgrades reuse mappings, completed asset state, folder mappings, and history automatically. On its first launch, 1.5.6 attempts to migrate the newest nearby older Wiredrive Sync `config.json` and `watchfolder.db`.

Startup/remap scanning is now optimized for files that are already uploaded. Unchanged files already recorded in SQLite are skipped immediately using local path + byte size + modification time, with no Wiredrive request and no 5-second stability wait. Unknown files are compared against a single bulk recursive Wiredrive inventory index using relative folder + exact filename + exact byte size. Exact matches are marked synchronized immediately instead of being queued one-by-one.

Known relative-folder to Wiredrive-folder-ID mappings are reused from SQLite, and a short-lived remote inventory cache reduces repeated duplicate-check requests during upload bursts.


## 1.5.7 Beta — Browser icon
Added a custom Wiredrive Sync favicon. The build includes a multi-resolution `favicon.ico`, a 32px browser PNG, an Apple touch icon, and an explicit `/favicon.ico` route so the launched browser tab/window uses the Wiredrive Sync icon.


## 1.5.8 Beta — Newest media first
Transfer jobs now carry a media priority timestamp. Uploads use the local file modification time and downloads use Wiredrive's remote modified time. Upload and download workers select the highest-priority/newest media next rather than the oldest queued job.

During startup/remapping, local candidates are sorted newest-first and the newest 12 files are immediately placed into the normal safe upload queue before the slower historical bulk reconciliation completes. Their standard Wiredrive duplicate check remains active, so already-uploaded files are skipped rather than duplicated. Historical reconciliation continues in the background.

Remote download inventories are also sorted newest-first before new download jobs are created. Existing jobs from older databases remain compatible; newly discovered files automatically outrank legacy jobs with no priority timestamp.


## 1.5.9 Beta — Functional global search
The top search field now provides categorized live results for mappings, active/queued transfers, and activity/history. It searches mapping names, filenames, local/remote paths, destinations, status, direction, messages, and remote asset IDs. Clicking a result jumps to the appropriate section. Command/Ctrl-K focuses the field, Enter opens the first result, Escape clears it, and the × button clears it manually.


## 1.6.1 Beta — Native wrapper isolation
The macOS application now always starts the bundled current Wiredrive Sync engine on a private dynamically assigned localhost port. It never attaches to a process already running on port 8765. This prevents older Watch Folder/browser builds from appearing inside the native application window and also isolates WebKit static-asset caching between launches.


## 1.7 — Release
Wiredrive Sync is now promoted from beta to version 1.7. All in-app version labels, the browser/native window title, About page, footer, configuration metadata, and macOS bundle metadata report version 1.7.

The sidebar and About page branding now use the same Wiredrive Sync artwork as the favicon and macOS app icon instead of the previous letter-only `W` mark.


## 1.7 — Apple Silicon compatibility update
The macOS bootstrap now supports both Python 3.9 and current Python releases correctly. Python 3.9 installs the last compatible PyObjC 11.1 framework packages; Python 3.10+ installs PyObjC 12.2.1. The launcher prefers a native arm64 Python on Apple Silicon and records the selected Python version, architecture, macOS version, dependency installation, and Cocoa/WebKit preflight results in the application log.


## 1.7 — AppleDouble / hidden metadata fix
macOS AppleDouble sidecar files such as `._TAH26_COLOUR___173_D` are now
explicitly excluded from synchronization. All dot-prefixed files and folders
are ignored by live Watchdog events, startup/fast reconciliation scans, manual
enqueue paths, and the upload worker. Previously queued hidden uploads are
purged automatically on startup. macOS `UF_HIDDEN` filesystem flags are also
honored. Existing hidden assets already uploaded to Wiredrive are not deleted.


## 1.7 — Cocoa application-menu fix
The native macOS launcher now renames pywebview's actual Cocoa application menu
after it is created. The menu-bar application title is changed from `Python`
to `Wiredrive Sync`, along with the standard `About`, `Hide`, and `Quit`
application-menu entries. This is separate from the earlier process-name fix
and addresses the visible menu shown by macOS.


## 1.7 — Native About panel fix
About Wiredrive Sync now uses a dedicated native Cocoa handler instead of Python's interpreter About panel. It displays Wiredrive Sync, Version 1.7, Build 170, the app icon, and © 2026 Three Crow Studios.


## 1.7 — Persistent Cocoa menu identity fix
The macOS launcher now applies the Wiredrive Sync application-menu identity on
the AppKit main thread and reasserts it during the first five seconds of startup.
This prevents pywebview from reverting the application menu title to `Python`
after its own Cocoa menu initialization. The native Wiredrive Sync About panel
and AppleDouble hidden-file filtering remain in place.


## 1.7 — Exact Files Synced dashboard total
The top dashboard Files Synced metric now comes from an exact SQLite COUNT of
all complete sync-assets. The recent sync-assets payload remains limited to
300 rows for UI/history performance, but that limit no longer affects the
displayed total.
