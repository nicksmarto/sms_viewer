import os
import sys
import shutil
import tempfile
import unittest

# Add the root directory to the Python path to allow importing 'app'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (
    app, init_db, process_xml_files, get_db_connection, build_source_manifest,
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

    def test_cache_meta_written(self):
        conn = get_db_connection()
        try:
            meta = {r['key']: r['value'] for r in conn.execute("SELECT key, value FROM cache_meta")}
        finally:
            conn.close()
        self.assertIn('format_version', meta)
        self.assertIn('source_manifest', meta)
        self.assertEqual(meta.get('source_dir'), TEST_DIR)


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
