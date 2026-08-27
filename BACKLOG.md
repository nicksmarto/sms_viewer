# Backlog

Ideas and follow-ups for the SMS Viewer, roughly ordered. When this repo is on
GitHub, these can graduate into GitHub Issues (one issue per item).

## Features
- **Auto-detect "my" phone number.** Instead of manually setting `MY_PHONE_NUMBER`
  in `.env`, sample the archive for the most frequently occurring number (a strong
  signal it's the account owner's) and ask the user to confirm it. Falls back to the
  `.env` value / manual entry.
- **Refine spam/whitelist (future).** Possible follow-ups on the shipped filtering: a
  bulk-import of an existing spam list, a way to mark spam from the conversation *list*
  (not just an open conversation), and optionally excluding the `Spam/` folder from media
  export.

- **Write EXIF capture time into media.** Today the app only sets each extracted media
  file's *filesystem* date to the message time; it does not write the date into the image's
  own EXIF metadata (an original goal). Add this (e.g. via `piexif`, which is why it was
  once a dependency), preserving any real camera EXIF already present.

## Architecture / hygiene
- **Make assumptions configurable.** The UTC-6 timezone assumption and DB filename are
  baked in; expose them as config where it makes sense.
- **Auto-prune orphaned caches on startup.** Clearing caches whose source folder is gone is
  currently a manual button in the cache panel; optionally do it automatically on launch.
- **Reconsider repo location.** The repo lives in Google Drive (kept there with the folder
  marked "available offline"). If Git slows again, relocate to a non-synced path.

## Feature ideas (capability & delight)
*Unprioritized — brainstormed for future consideration.*
- **Incremental indexing.** Index only new/changed XML instead of a full rebuild — a big
  win for the multi-gigabyte real archive (the manifest already knows which files changed).
- **Jump-to-date / timeline scrubber.** Use the known date range to jump to a point in a
  conversation, or across the whole archive.
- **Media gallery view.** A grid of all photos/videos in a conversation (or globally),
  sorted by date, with click-to-open — a fast way to browse the media you've extracted.
- **Advanced search.** Upgrade to SQLite FTS5 for speed, add operators (`from:`,
  `has:media`, `before:`/`after:`), and search *within* the open conversation.
- **Export a single conversation.** Save one thread as HTML/PDF/plain text (with its media)
  — shareable and printable.
- **"On this day" memories.** Surface messages from today's date in previous years.
- **Archive analytics dashboard.** Message counts over time, most-active contacts,
  first/last contact, busiest days, emoji/word frequency — informative and fun.
- **Contact avatars.** Initials (or photos from the contacts export) beside names for a
  more familiar, polished feel.
- **Light/dark theme toggle.** The UI is dark-only today.
- **Keyboard navigation.** Arrow keys between conversations/messages; `/` to focus search;
  `Esc` already closes modals.
- **Conversation filters.** Filter the list to "has media", by date, or by
  contact-vs-unknown.
- **Redacted / safe-share export.** Export a conversation with numbers and names masked,
  for sharing a screenshot or thread without exposing personal data.

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
- ✅ **Contacts whitelist + spam blacklist** — `contacts.csv` names/whitelist plus a portable
  `spamnumbers.txt`; one-click "Mark as spam" (instant, query-time), a "Manage spam numbers"
  panel, contacts always override spam, spam media segregated into a flat `Spam/` folder.
- ✅ **Date range covered** — shown on the main stats panel and in the cache-management panel.
- ✅ SQLite robustness (WAL mode + busy timeout).
- ✅ Richer indexing progress in the UI (phase, file x/N, live message/media counts, date range).
- ✅ On-demand **Export media to folder** button (writes the browsable, sender-organized
  `mms_media/` export on request instead of always).
- ✅ Repaired the brittle integration test; suite now covers indexing, media timestamps,
  cache metadata, and the source-manifest logic.
- ✅ Confirmed MMS media (images/video/audio) renders inline in conversations.
