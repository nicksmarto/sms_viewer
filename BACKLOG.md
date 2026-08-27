# Backlog

Ideas and follow-ups for the SMS Viewer, roughly ordered. When this repo is on
GitHub, these can graduate into GitHub Issues (one issue per item).

## Features
- **Auto-detect "my" phone number.** Instead of manually setting `MY_PHONE_NUMBER`
  in `.env`, sample the archive for the most frequently occurring number (a strong
  signal it's the account owner's) and ask the user to confirm it. Falls back to the
  `.env` value / manual entry.
- **Unified contact/spam filtering (whitelist + blacklist + neither).** Combine three
  signals elegantly: `contacts.csv` as a *whitelist* (known people), a spam list (like
  the old `spamnumbers.txt`) as a *blacklist*, and a sensible default for numbers in
  neither. Historically spam was handled *outside* this app by a separate
  `delete_folders.py` script — this would bring it in as a first-class, non-destructive
  view/filter. New design; affects both conversation display and sender-folder naming.

- **Write EXIF capture time into media.** Today the app only sets each extracted media
  file's *filesystem* date to the message time; it does not write the date into the image's
  own EXIF metadata (an original goal). Add this (e.g. via `piexif`, which is why it was
  once a dependency), preserving any real camera EXIF already present.

## Architecture / hygiene
- **Make assumptions configurable.** The UTC-6 timezone assumption and DB filename are
  baked in; expose them as config where it makes sense.

## Packaging / distribution
- **Standalone app via PyInstaller.** Bundle Python + the code into a double-clickable
  `.app` / `.exe` so users need no Python at all ("just an icon"). Built separately per
  platform and heavier to ship; the `run.command` / `run.bat` launchers cover the
  everyday case for now.

## Done
- ✅ Clean single Git repo with SemVer tags; privacy-enforcing `.gitignore`.
- ✅ Removed a leaked API key and personal phone number from all history; `.env` config.
- ✅ One-click launchers (`run.command` / `run.bat`) that auto-create the venv.
- ✅ **Local cache** — the DB and extracted media now live in a local app-data folder
  (`~/Library/Application Support/SMSViewer/<per-archive>/`), never inside the archive
  folder. Fixes the Google Drive "disk I/O error" and stops polluting the backups.
- ✅ **Cache lifecycle** — detect presence, integrity, cache-format version, and whether
  the source backups changed; reuse a healthy cache, rebuild automatically when not.
- ✅ **Cache management** — a "Manage cached indexes" panel to list every cache (source,
  size, message count, date), delete individual ones, and clear caches whose source folder
  is gone; guarded against path traversal.
- ✅ SQLite robustness (WAL mode + busy timeout).
- ✅ Richer indexing progress in the UI (phase, file x/N, live message/media counts, date range).
- ✅ On-demand **Export media to folder** button (writes the browsable, sender-organized
  `mms_media/` export on request instead of always).
- ✅ Repaired the brittle integration test; suite now covers indexing, media timestamps,
  cache metadata, and the source-manifest logic.
- ✅ Confirmed MMS media (images/video/audio) renders inline in conversations.
