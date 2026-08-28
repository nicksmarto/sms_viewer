import os
import sys
import shutil
import tempfile
import unittest

# Add the root directory to the Python path to allow importing 'app'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lxml import etree

from app import (
    app, init_db, process_xml_files, get_db_connection, build_source_manifest,
    fix_surrogate_charrefs,
)

TEST_DIR = os.path.dirname(os.path.abspath(__file__))


class TestIndexing(unittest.TestCase):
    """Indexes the real sample XML in test/ into a throwaway local cache."""

    def setUp(self):
        # Generated data goes into a temp cache dir, never into the source folder.
        self.cache_dir = tempfile.mkdtemp(prefix="smsviewer_test_")
        app.config['XML_DIRECTORY'] = TEST_DIR
        app.config['DB_PATH'] = os.path.join(self.cache_dir, 'sms_messages.db')
        app.config['MEDIA_DIR'] = os.path.join(self.cache_dir, 'media')
        os.makedirs(app.config['MEDIA_DIR'], exist_ok=True)
        app.testing = True
        self.ctx = app.app_context()
        self.ctx.push()
        self.assertTrue(init_db(), "Database initialization failed.")
        process_xml_files()

    def tearDown(self):
        self.ctx.pop()
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def test_messages_and_conversations_indexed(self):
        conn = get_db_connection()
        try:
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            convos = conn.execute("SELECT COUNT(DISTINCT participants) FROM messages").fetchone()[0]
            contacts = conn.execute("SELECT COUNT(*) FROM contact_names").fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(messages, 0, "No messages were indexed.")
        self.assertGreater(convos, 0, "No conversations were found.")
        self.assertGreater(contacts, 0, "No contact names were loaded.")

    def test_media_extracted_into_cache_with_message_mtime(self):
        """Any extracted media file should exist in the cache and carry the
        message's timestamp as its modification time (robust: uses whatever
        media the sample happens to contain, not one hard-coded message)."""
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT mms_media_path, date FROM messages "
                "WHERE mms_media_path IS NOT NULL LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "Sample data produced no media to test.")
        media_path = os.path.join(app.config['MEDIA_DIR'], row['mms_media_path'])
        self.assertTrue(os.path.exists(media_path), f"Media not in cache: {media_path}")
        self.assertAlmostEqual(
            os.path.getmtime(media_path), row['date'] / 1000, delta=1,
            msg="Media file mtime does not match the message time.",
        )

    def test_no_duplicate_messages_across_overlapping_archives(self):
        """Overlapping backups (the same history exported more than once, often
        with the number formatted differently or group participants in a
        different order) must collapse to a single row per logical message.
        Regression test for de-duplication built on the raw address."""
        conn = get_db_connection()
        try:
            # Text rows: identity is (conversation, time, direction, body, sender).
            text_dupes = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM messages
                    WHERE mms_media_path IS NULL
                    GROUP BY participants, date, type,
                             IFNULL(body, ''), IFNULL(sender_address, '')
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            # Media rows: identity also includes the specific media file.
            media_dupes = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM messages
                    WHERE mms_media_path IS NOT NULL
                    GROUP BY participants, date, type, mms_media_path
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(text_dupes, 0, "Duplicate text messages were indexed.")
        self.assertEqual(media_dupes, 0, "Duplicate media messages were indexed.")

    def test_cache_meta_written(self):
        conn = get_db_connection()
        try:
            meta = {r['key']: r['value'] for r in conn.execute("SELECT key, value FROM cache_meta")}
        finally:
            conn.close()
        self.assertIn('format_version', meta)
        self.assertIn('source_manifest', meta)
        self.assertEqual(meta.get('source_dir'), TEST_DIR)


class TestSurrogateCharrefs(unittest.TestCase):
    """'SMS Backup & Restore' stores emoji as surrogate-pair numeric character
    references (e.g. '&#55357;&#56832;'). libxml2 turns those into invalid
    UTF-8, so every emoji-bearing message used to be dropped on read. The
    ingest-time repair must recover them without corrupting ordinary XML."""

    def test_pair_becomes_astral_character(self):
        self.assertEqual(fix_surrogate_charrefs("hi&#55357;&#56832;!"), "hi\U0001F600!")
        self.assertEqual(fix_surrogate_charrefs("a&#xD83D;&#xDE00;b"), "a\U0001F600b")

    def test_ordinary_references_untouched(self):
        # '&#60;' ('<') must survive so XML parsing still sees it as data.
        self.assertEqual(fix_surrogate_charrefs("x&#60;3"), "x&#60;3")
        # A normal reference directly before a real pair must not be consumed.
        self.assertEqual(fix_surrogate_charrefs("&#65;&#55357;&#56832;"), "&#65;\U0001F600")

    def test_unpaired_surrogates_dropped(self):
        self.assertEqual(fix_surrogate_charrefs("a&#55357;b"), "ab")   # lone high
        self.assertEqual(fix_surrogate_charrefs("a&#56832;b"), "ab")   # lone low

    def test_survives_round_trip_through_lxml(self):
        """The whole point: the repaired attribute must read back cleanly."""
        fixed = fix_surrogate_charrefs("<root><sms body='joy&#55357;&#56832;!'/></root>")
        parser = etree.XMLParser(recover=True, huge_tree=True)
        el = etree.fromstring(fixed.encode('utf-8'), parser=parser).find('sms')
        self.assertEqual(el.get('body'), "joy\U0001F600!")  # no UnicodeDecodeError


class TestSourceManifest(unittest.TestCase):
    """The manifest is what powers stale-cache detection."""

    def test_manifest_tracks_xml_and_changes(self):
        tmp = tempfile.mkdtemp(prefix="smsviewer_manifest_")
        try:
            with open(os.path.join(tmp, 'a.xml'), 'w') as f:
                f.write('<smses></smses>')
            first = build_source_manifest(tmp)
            self.assertIn('a.xml', first)

            # Adding another XML changes the fingerprint (=> cache would be stale).
            with open(os.path.join(tmp, 'b.xml'), 'w') as f:
                f.write('<smses></smses>')
            self.assertNotEqual(first, build_source_manifest(tmp))

            # Non-XML files are ignored.
            with open(os.path.join(tmp, 'notes.txt'), 'w') as f:
                f.write('ignore me')
            self.assertNotIn('notes.txt', build_source_manifest(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
