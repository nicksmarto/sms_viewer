# Backlog

Ideas and follow-ups for the SMS Viewer, roughly ordered. When this repo is on
GitHub, these can graduate into GitHub Issues (one issue per item).

## Features
- **Auto-detect "my" phone number.** Instead of manually setting `MY_PHONE_NUMBER`
  in `.env`, sample the archive for the most frequently occurring number (a strong
  signal it's the account owner's) and ask the user to confirm it. Fall back to the
  `.env` value / manual entry. Replaces the current manual config from v2.0.2.

## Architecture / hygiene
- **Write generated data outside the source tree.** The indexer currently writes
  `sms_messages.db` and `mms_media/` into the target directory. Consider a dedicated
  output/cache location (e.g. a user-data dir, or a configurable `--output` path) so
  generated artifacts never sit next to source or risk being committed.
- **Make assumptions configurable.** The UTC-6 timezone assumption and DB filename
  are baked in; expose them as config where it makes sense.

## Testing
- **Repair the brittle integration test.** `test/test_app.py`'s
  `test_exif_and_timestamp_update` asserts on one specific hard-coded message and its
  media part, which fails on data subsets that lack it. Make it robust (e.g. pick a
  target dynamically, or ship a tiny fixed fixture).
- **Restore lost unit tests.** `test_contact_name_logic.py` and
  `test_mms_processing.py` existed at v1.0.0 but were dropped by v2.0.0. Recover them
  from history (`git show v1.0.0:test_contact_name_logic.py`) and update to current code.

## Infrastructure
- **Reconsider repo location.** The repo lives in Google Drive; Git was slow until the
  folder was marked "available offline." If it slows again, relocate to a non-synced
  path (e.g. `~/Developer/sms_viewer`).

## Done
- ✅ Convert tangled multi-folder project into one clean Git repo (main + tags
  v1.0.0 / v2.0.0 / v2.0.1).
- ✅ Enforce privacy via `.gitignore`; verified no message data/media/contacts tracked.
- ✅ Remove leaked API key from all history; make phone number configurable (v2.0.2).
