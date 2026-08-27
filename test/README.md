# test/ — local test data (intentionally NOT committed)

This folder holds **real** SMS/MMS data used to run and test the viewer locally:

- `*.xml` — real SMS Backup & Restore archives
- `contacts.csv` — real contacts export (names + phone numbers)
- `mms_media/` — extracted MMS attachments (real photos)
- `sms_messages.db` — the generated SQLite index

**All of the above is private and is excluded by `.gitignore`.** It lives here on disk
so the app has something to run against, but Git never tracks it — nothing private can
ever be pushed to a remote.

The one file here that *is* committed is `test_app.py` (the unit tests), because it's
source code, not private data.

If you clone this repo on a fresh machine, this folder will be empty except for the
tests — copy your real archive into it locally to run the viewer.
