# Design Notes

Why this project exists and the thinking behind its main decisions. For installation and
usage, see the [README](README.md); for planned work, see [BACKLOG.md](BACKLOG.md).

## Why this exists

Text-message history from the **SMS Backup & Restore** Android app is stored as XML —
often many gigabytes spread across multiple files accumulated over years and phones. Those
files are impractical to actually read: the vendor's viewer handles one file at a time,
can't extract MMS media, and large XML is slow to open anywhere. This tool indexes one or
more of those backups into a fast local database, extracts the media, and presents
everything as a familiar, searchable messaging UI — entirely offline.

## Design principles

- **Local-first and private.** Everything runs on your machine; nothing is uploaded. Real
  message data is never committed to the repo (enforced by `.gitignore`).
- **The cache is disposable.** Anything the app generates can be rebuilt from the original
  XML, so it is treated as a throwaway cache, never as a source of truth.
- **Never mutate the originals.** The `.xml` backups are read-only inputs; the app never
  writes to or alters them.
- **Robustness over strictness.** Real backups are messy (malformed entries, huge lines,
  duplicates). The indexer skips bad records and keeps going rather than abandoning a whole
  file. One quirk is repaired rather than skipped: "SMS Backup & Restore" stores emoji as
  numeric character references to UTF-16 surrogate code points (e.g. `&#55357;&#56832;`),
  which libxml2 turns into invalid UTF-8; these are stitched back into real characters at
  ingest so emoji-bearing messages survive instead of being dropped.
- **Familiar UX.** Reading should feel like a normal messaging app.

## Key decisions & rationale

- **SQLite index.** Re-parsing multi-GB XML on every view is too slow, so messages are
  parsed once into SQLite for instant browsing and search.
- **Cache lives outside the archive folder.** Generated data (the SQLite DB and extracted
  media) is written to a local app-data folder, not into the backup folder. This keeps
  backups pristine and — critically — avoids SQLite "disk I/O error" failures on
  cloud-synced filesystems like Google Drive, where the archives often live.
- **Content-hash de-duplication.** Overlapping backups repeat the same messages and media.
  Messages are de-duplicated by a hash of their *normalized* fields (the sorted participant
  list, timestamp, direction, body, and normalized sender) — never the raw address, since
  different backup apps format the same number differently ("(412) 656-4424" vs "4126564424")
  and reorder group participants. Media is de-duplicated by the SHA-256 of the file bytes,
  keeping the first occurrence found.
- **Timestamps reflect when the media was received.** Each extracted media file's
  *filesystem* modification time is set to the message's date (superseding the extraction
  time), because the received date is the meaningful one when browsing a photo library.
  Precision (time zones, milliseconds) is intentionally not a goal. *(Writing the date into
  the image's own EXIF metadata was an original goal but is not yet implemented — see
  [BACKLOG.md](BACKLOG.md).)*
- **Sender-organized media, exported on demand.** Media is stored in the cache; a browsable
  copy organized into per-sender folders (named like the contact) is written into the
  archive folder only when the user asks, rather than always writing thousands of files
  into a possibly cloud-synced location.
- **Contact-name resolution.** Numbers are resolved to names using data in the backups plus
  an optional `contacts.csv`; those names also drive the media folder names.
- **Whitelist + spam blacklist.** `contacts.csv` doubles as a whitelist; a portable
  `spamnumbers.txt` (kept in the archive folder, so it travels with the backups) is a
  blacklist. Spam is filtered at *query time* — marking a number hides it instantly with no
  rebuild, and un-marking restores it — while **contacts always override spam** so a known
  person can never be hidden by accident. On rebuild, spam senders' media is segregated into
  a flat `Spam/` folder (number-prefixed filenames). All matching uses the same number
  normalization (country code stripped, punctuation ignored).
- **Cache lifecycle.** On opening an archive the cache is validated — it must exist, pass an
  integrity check, match the current cache format version, and match a manifest of the
  source files — and is reused when healthy, rebuilt when not.

## Assumptions

- Time zone defaults to UTC−6 unless better data exists; exactness is not important.
- On duplicate media, the first copy found is kept.
- The time a message was received is treated as the media's capture time.

## Non-goals

- **Not a backup creator** — it reads existing SMS Backup & Restore exports; it doesn't make them.
- **Single format** — only that app's XML schema is supported (not iOS, PDF, CSV-only, etc.).
- **Not cloud-hosted** — there is no server or online version; it runs locally.
- **Not an editor of your originals** — the `.xml` backups are never modified.
