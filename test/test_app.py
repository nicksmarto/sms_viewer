import os
import sys
import shutil
import base64
import tempfile
import unittest
from urllib.parse import quote

# Add the root directory to the Python path to allow importing 'app'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lxml import etree

from app import (
    app, init_db, process_xml_files, get_db_connection, build_source_manifest,
    fix_surrogate_charrefs, normalize_number, compute_redundancy,
    generate_unique_hash, clean_message_text,
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

    def test_source_file_provenance_recorded(self):
        """Every XML file gets a source_files row, and its stored msg_count
        matches how many message identities were mapped to it."""
        xml_count = len([f for f in os.listdir(TEST_DIR) if f.lower().endswith('.xml')])
        conn = get_db_connection()
        try:
            files = conn.execute(
                "SELECT id, msg_count, redundant_count, unique_only_count FROM source_files"
            ).fetchall()
            self.assertEqual(len(files), xml_count, "One source_files row expected per XML file.")
            for f in files:
                mapped = conn.execute(
                    "SELECT COUNT(*) FROM file_messages WHERE file_id = ?", (f['id'],)
                ).fetchone()[0]
                self.assertEqual(f['msg_count'], mapped,
                                 "source_files.msg_count must equal its file_messages rows.")
                # redundant + unique-only partition the file's messages.
                self.assertEqual((f['redundant_count'] or 0) + (f['unique_only_count'] or 0),
                                 f['msg_count'], "redundant + unique-only must sum to msg_count.")
        finally:
            conn.close()

    def test_every_message_has_provenance(self):
        """Each indexed message identity is attributed to at least one file, so
        no row is orphaned from the overlap analysis."""
        conn = get_db_connection()
        try:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM messages m "
                "WHERE NOT EXISTS (SELECT 1 FROM file_messages fm WHERE fm.msg_hash = m.unique_hash)"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(orphans, 0, "Every message must trace back to a source file.")

    def test_safe_to_delete_set_loses_no_messages(self):
        """The critical guarantee on real data: the messages held by every file
        flagged safe-to-delete are all still present in a file being kept."""
        conn = get_db_connection()
        try:
            kept_hashes = set(r[0] for r in conn.execute(
                "SELECT DISTINCT fm.msg_hash FROM file_messages fm "
                "JOIN source_files sf ON sf.id = fm.file_id WHERE sf.safe_to_delete = 0"
            ))
            deletable_hashes = set(r[0] for r in conn.execute(
                "SELECT DISTINCT fm.msg_hash FROM file_messages fm "
                "JOIN source_files sf ON sf.id = fm.file_id WHERE sf.safe_to_delete = 1"
            ))
        finally:
            conn.close()
        lost = deletable_hashes - kept_hashes
        self.assertEqual(lost, set(),
                         "Deleting the safe-to-delete set would lose messages held nowhere else.")

    def test_analytics_endpoint_ok(self):
        client = app.test_client()
        resp = client.get('/api/analytics')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for key in ('histogram', 'files', 'files_summary', 'top_contacts',
                    'sent_received', 'heatmap', 'yearly', 'records'):
            self.assertIn(key, data)
        self.assertEqual(len(data['heatmap']), 7)
        self.assertEqual(len(data['heatmap'][0]), 24)


class TestComputeRedundancy(unittest.TestCase):
    """The overlap / safe-to-delete math. Pure function, no DB."""

    @staticmethod
    def _kept_cover_all(file_to_msgs, result):
        """Invariant: union of files NOT marked deletable covers every message."""
        all_msgs = set().union(*file_to_msgs.values()) if file_to_msgs else set()
        kept = set()
        for fid, msgs in file_to_msgs.items():
            if not result['per_file'][fid]['safe_to_delete']:
                kept |= msgs
        return all_msgs <= kept

    def test_subset_file_is_deletable(self):
        sizes = {'A': (100, 3), 'B': (50, 2)}
        f = {'A': {'m1', 'm2', 'm3'}, 'B': {'m1', 'm2'}}
        r = compute_redundancy(sizes, f)
        self.assertTrue(r['per_file']['B']['safe_to_delete'])
        self.assertFalse(r['per_file']['A']['safe_to_delete'])
        self.assertEqual(r['per_file']['B']['unique_only_count'], 0)
        self.assertEqual(r['per_file']['A']['unique_only_count'], 1)  # m3
        self.assertIn(('A', 'B', 2), r['overlap_rows'])
        self.assertTrue(self._kept_cover_all(f, r))

    def test_disjoint_files_keep_everything(self):
        sizes = {'A': (10, 2), 'B': (10, 2)}
        f = {'A': {'m1', 'm2'}, 'B': {'m3', 'm4'}}
        r = compute_redundancy(sizes, f)
        self.assertFalse(r['per_file']['A']['safe_to_delete'])
        self.assertFalse(r['per_file']['B']['safe_to_delete'])
        self.assertEqual(r['overlap_rows'], [])
        self.assertTrue(self._kept_cover_all(f, r))

    def test_three_way_partial_overlap_never_loses_a_message(self):
        # Each message is in exactly two of the three files.
        sizes = {'A': (10, 2), 'B': (20, 2), 'C': (30, 2)}
        f = {'A': {'m1', 'm2'}, 'B': {'m2', 'm3'}, 'C': {'m1', 'm3'}}
        r = compute_redundancy(sizes, f)
        deletable = [k for k, v in r['per_file'].items() if v['safe_to_delete']]
        self.assertEqual(len(deletable), 1, "Only one of a 3-cycle can go without loss.")
        self.assertTrue(self._kept_cover_all(f, r))

    def test_triplicate_keeps_the_largest(self):
        # Same single message in all three; two are redundant, keep the biggest.
        sizes = {'A': (10, 1), 'B': (20, 1), 'C': (30, 1)}
        f = {'A': {'m'}, 'B': {'m'}, 'C': {'m'}}
        r = compute_redundancy(sizes, f)
        self.assertTrue(r['per_file']['A']['safe_to_delete'])
        self.assertTrue(r['per_file']['B']['safe_to_delete'])
        self.assertFalse(r['per_file']['C']['safe_to_delete'])  # largest kept
        self.assertTrue(self._kept_cover_all(f, r))

    def test_empty_file_is_deletable(self):
        sizes = {'A': (10, 1), 'B': (0, 0)}
        f = {'A': {'m1'}, 'B': set()}
        r = compute_redundancy(sizes, f)
        self.assertTrue(r['per_file']['B']['safe_to_delete'])
        self.assertFalse(r['per_file']['A']['safe_to_delete'])
        self.assertTrue(self._kept_cover_all(f, r))


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


class TestNormalizeNumber(unittest.TestCase):
    """Group addresses and odd inputs must normalize without raising — a crash
    here skips the whole message during indexing."""

    def test_none_and_empty(self):
        self.assertIsNone(normalize_number(None))
        self.assertIsNone(normalize_number(""))

    def test_strips_country_code_and_punctuation(self):
        self.assertEqual(normalize_number("+1 (716) 555-1234"), "7165551234")

    def test_group_is_sorted_and_order_independent(self):
        # Same members in either order yield the same identity.
        self.assertEqual(
            normalize_number("7165551234~2015551234"),
            normalize_number("2015551234~7165551234"),
        )

    def test_group_with_unnormalizable_member_does_not_crash(self):
        # An alphanumeric short code (VZWPMSG) drops out instead of raising
        # "'<' not supported between 'NoneType' and 'str'" during the sort.
        self.assertEqual(normalize_number("7165551234~VZWPMSG"), "7165551234")
        self.assertEqual(normalize_number("VZWPMSG~7165551234"), "7165551234")

    def test_group_with_empty_member_does_not_crash(self):
        self.assertEqual(
            normalize_number("7165551234~~2015551234"),
            "2015551234~7165551234",
        )


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


class TestUniqueHashIdentity(unittest.TestCase):
    """The de-duplication identity. These encode the exact cross-source
    mismatches that were producing duplicate rows."""

    def test_sender_address_is_not_part_of_identity(self):
        # Same logical message; one source recorded the sender as a phone
        # number, another as an Apple-ID email mangled into digits. Must hash
        # the same so they de-duplicate.
        base = {'participants': '7135533770', 'date': 1570894223000,
                'body': 'You just suffled on', 'type': '2'}
        h_phone = generate_unique_hash({**base, 'sender_address': '7135533770'}, 'text')
        h_email = generate_unique_hash({**base, 'sender_address': '467378233737786536'}, 'text')
        self.assertEqual(h_phone, h_email)

    def test_date_compared_at_whole_second(self):
        # iMessage export lands on the whole second; the Android archive kept
        # the real milliseconds. Same second => same identity.
        a = generate_unique_hash({'participants': 'X', 'date': 1547921797000, 'body': 'hi', 'type': '2'})
        b = generate_unique_hash({'participants': 'X', 'date': 1547921797137, 'body': 'hi', 'type': '2'})
        self.assertEqual(a, b)

    def test_messages_more_than_a_second_apart_stay_distinct(self):
        a = generate_unique_hash({'participants': 'X', 'date': 1547921797000, 'body': 'hi', 'type': '2'})
        c = generate_unique_hash({'participants': 'X', 'date': 1547921799000, 'body': 'hi', 'type': '2'})
        self.assertNotEqual(a, c)

    def test_content_fields_still_differentiate(self):
        base = {'participants': 'X', 'date': 1547921797000, 'type': '1'}
        h = generate_unique_hash({**base, 'body': 'hi'})
        self.assertNotEqual(h, generate_unique_hash({**base, 'body': 'bye'}))                 # body
        self.assertNotEqual(h, generate_unique_hash({**base, 'body': 'hi', 'type': '2'}))     # direction
        self.assertNotEqual(h, generate_unique_hash({'participants': 'Y', 'date': 1547921797000, 'body': 'hi', 'type': '1'}))  # convo

    def test_non_numeric_date_does_not_raise(self):
        # An empty/garbage date must fall back, not crash indexing.
        self.assertIsInstance(generate_unique_hash({'participants': 'X', 'date': '', 'body': 'hi', 'type': '1'}), str)
        self.assertIsInstance(generate_unique_hash({'participants': 'X', 'date': None, 'body': 'hi', 'type': '1'}), str)


class TestCleanMessageText(unittest.TestCase):
    """U+FFFC is iMessage's inline-attachment placeholder; left in, it renders
    as a phantom blank bubble next to the media."""

    def test_placeholder_only_becomes_empty(self):
        self.assertEqual(clean_message_text('￼'), '')

    def test_placeholder_stripped_from_mixed_text(self):
        self.assertEqual(clean_message_text('look ￼ here'), 'look  here')

    def test_ordinary_text_untouched(self):
        self.assertEqual(clean_message_text('hello world'), 'hello world')

    def test_none_and_empty_pass_through(self):
        self.assertIsNone(clean_message_text(None))
        self.assertEqual(clean_message_text(''), '')


class _CraftedIndexBase(unittest.TestCase):
    """Indexes hand-written XML from a throwaway dir into a throwaway cache."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="smsv_src_")
        self.cache = tempfile.mkdtemp(prefix="smsv_cache_")
        app.testing = True
        self.ctx = app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.cache, ignore_errors=True)

    def index(self, files):
        for name, content in files.items():
            with open(os.path.join(self.src, name), 'w', encoding='utf-8') as f:
                f.write(content)
        app.config['XML_DIRECTORY'] = self.src
        app.config['DB_PATH'] = os.path.join(self.cache, 'sms_messages.db')
        app.config['MEDIA_DIR'] = os.path.join(self.cache, 'media')
        os.makedirs(app.config['MEDIA_DIR'], exist_ok=True)
        self.assertTrue(init_db())
        process_xml_files()

    def query(self, sql, params=()):
        conn = get_db_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class TestCrossSourceDeduplication(_CraftedIndexBase):
    def test_subsecond_timestamp_difference_collapses(self):
        """The same sent SMS, exported once by iMessage (whole second) and once
        by Android SMS Backup & Restore (with milliseconds) and formatted
        differently, must collapse to a single row."""
        self.index({
            'imessage.xml': '<smses><sms address="+13042205142" date="1547921797000" '
                            'type="2" body="dup me" readable_date="Jan 19, 2019 11:16:37 AM"/></smses>',
            'android.xml': '<smses><sms address="+1 (304) 220-5142" date="1547921797137" '
                           'type="2" body="dup me" readable_date="Jan 19, 2019 11:16:37"/></smses>',
        })
        rows = self.query("SELECT COUNT(*) FROM messages WHERE body='dup me'")
        self.assertEqual(rows[0][0], 1, "Sub-second-apart copies of one message were not de-duplicated.")


class TestAttachmentPlaceholderIngest(_CraftedIndexBase):
    def _mms(self, text_part):
        img = base64.b64encode(b'\xff\xd8\xff\xe0TESTJPEGDATA').decode()
        return ('<smses><mms date="1586831671264" msg_box="2" address="7034891613" m_type="128">'
                '<parts>' + text_part +
                f'<part seq="1" ct="image/jpeg" data="{img}"/>'
                '</parts><addrs><addr address="7034891613" type="137"/></addrs></mms></smses>')

    def test_placeholder_only_text_makes_no_blank_bubble(self):
        self.index({'m.xml': self._mms('<part seq="0" ct="text/plain" text="￼"/>')})
        rows = self.query("SELECT body, mms_media_path FROM messages ORDER BY mms_media_path IS NULL")
        self.assertEqual(len(rows), 1, "The ￼-only text part should not create a second row.")
        self.assertIsNotNone(rows[0]['mms_media_path'], "The media row must survive.")
        no_placeholder = all('￼' not in (r['body'] or '') for r in rows)
        self.assertTrue(no_placeholder, "No stored body may contain the ￼ placeholder.")

    def test_real_text_alongside_media_is_kept_and_cleaned(self):
        self.index({'m.xml': self._mms('<part seq="0" ct="text/plain" text="check this ￼ out"/>')})
        text_rows = self.query("SELECT body FROM messages WHERE mms_media_path IS NULL")
        self.assertEqual(len(text_rows), 1)
        self.assertEqual(text_rows[0]['body'], 'check this  out')  # ￼ removed, real text kept


class TestSearchEndpoint(_CraftedIndexBase):
    def setUp(self):
        super().setUp()
        # Three 1:1 conversations; only one body contains the word "zebra".
        self.index({'s.xml':
            '<smses>'
            '<sms address="+17202433345" date="1600000000000" type="1" body="hey there"/>'
            '<sms address="+14155550001" date="1600000000001" type="1" body="has zebra here"/>'
            '<sms address="+14155550002" date="1600000000002" type="1" body="nothing special"/>'
            '</smses>'})
        # Give the first number a contact name, so number-search must look past it.
        conn = get_db_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO contact_names (address, name) VALUES (?, ?)",
                         ('7202433345', 'Helena Gallegos'))
        conn.close()
        self.client = app.test_client()

    def _search(self, q):
        return self.client.get('/api/search', query_string={'q': q}).get_json()

    def test_number_search_finds_conversation_with_a_contact_name(self):
        data = self._search('7202433345')
        self.assertTrue(any(c['address'] == '7202433345' for c in data['conversations']),
                        "A numbered conversation must be findable even when it has a contact name.")

    def test_formatted_number_search_matches(self):
        data = self._search('(720) 243-3345')
        self.assertTrue(any(c['address'] == '7202433345' for c in data['conversations']))

    def test_word_search_does_not_match_everything(self):
        # Regression: a no-digit query once matched every row via a bad sentinel.
        data = self._search('zebra')
        self.assertEqual(len(data['messages']), 1, "Word search leaked unrelated messages.")
        self.assertIn('zebra', data['messages'][0]['body'])

    def test_name_search_still_works(self):
        data = self._search('Helena')
        self.assertTrue(any(c['address'] == '7202433345' for c in data['conversations']))

    def test_short_query_rejected(self):
        self.assertEqual(self.client.get('/api/search', query_string={'q': 'ab'}).status_code, 400)


class TestMediaMimeOverride(_CraftedIndexBase):
    def setUp(self):
        super().setUp()
        qt = base64.b64encode(b'\x00\x00\x00\x18ftypqt   TESTMOOVATOM').decode()
        jp = base64.b64encode(b'\xff\xd8\xff\xe0TESTJPEG').decode()
        self.index({'m.xml':
            '<smses>'
            f'<mms date="1586831671264" msg_box="2" address="7034891613" m_type="128">'
            f'<parts><part seq="0" ct="video/quicktime" data="{qt}"/></parts>'
            f'<addrs><addr address="7034891613" type="137"/></addrs></mms>'
            f'<mms date="1586831680000" msg_box="2" address="7034891613" m_type="128">'
            f'<parts><part seq="0" ct="image/jpeg" data="{jp}"/></parts>'
            f'<addrs><addr address="7034891613" type="137"/></addrs></mms>'
            '</smses>'})
        self.client = app.test_client()

    def _fetch(self, like):
        row = self.query("SELECT mms_media_path FROM messages WHERE mms_media_path LIKE ?", (like,))[0]
        return self.client.get('/api/media/' + quote(row['mms_media_path']))

    def test_quicktime_served_as_playable_mp4(self):
        resp = self._fetch('%.quicktime')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers['Content-Type'].startswith('video/mp4'),
                        "iPhone .mov (H.264) must be served as video/mp4 so the browser plays it.")

    def test_ordinary_image_mime_unchanged(self):
        resp = self._fetch('%.jpeg')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers['Content-Type'].startswith('image/jpeg'))


if __name__ == '__main__':
    unittest.main()
