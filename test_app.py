import unittest
import os
import json
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock
from app import app, normalize_number, generate_unique_hash, process_xml_files, init_db

class TestApp(unittest.TestCase):

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True
        # Create a dummy db for testing
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config["DB_PATH"] = self.db_path
        self.init_test_db()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def init_test_db(self):
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL,
                    contact_name TEXT,
                    date INTEGER NOT NULL,
                    type INTEGER NOT NULL,
                    body TEXT,
                    mms_media_path TEXT,
                    mms_media_type TEXT,
                    sender TEXT,
                    readable_date TEXT,
                    unique_hash TEXT UNIQUE NOT NULL
                );
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS contact_names (
                    address TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
            ''')
            conn.execute("INSERT INTO messages (address, contact_name, date, type, body, readable_date, unique_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ("5551234567", "John Doe", 1678886400000, 1, "Hello", "2023-03-15", "hash1"))
            conn.execute("INSERT INTO messages (address, contact_name, date, type, body, readable_date, unique_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ("5551234567", "John Doe", 1678886400001, 2, "Hi there", "2023-03-15", "hash2"))
            conn.execute("INSERT INTO messages (address, contact_name, date, type, body, readable_date, unique_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ("5559876543", "Jane Smith", 1678886400002, 1, "Test message", "2023-03-15", "hash3"))
        conn.close()

    def test_normalize_number(self):
        self.assertEqual(normalize_number("+1 (555) 123-4567"), "5551234567")
        self.assertEqual(normalize_number("555-123-4567"), "5551234567")
        self.assertEqual(normalize_number("12345"), "12345")
        self.assertEqual(normalize_number(None), None)
        self.assertEqual(normalize_number(""), None)
        self.assertEqual(normalize_number("1-800-FLOWERS"), "8003569377")
        self.assertEqual(normalize_number("~12345~67890"), "12345")

    def test_generate_unique_hash(self):
        msg1 = {"address": "123", "date": "12345", "body": "hello", "type": "1"}
        msg2 = {"address": "123", "date": "12345", "body": "hello", "type": "1"}
        msg3 = {"address": "123", "date": "12345", "body": "world", "type": "1"}
        self.assertEqual(generate_unique_hash(msg1), generate_unique_hash(msg2))
        self.assertNotEqual(generate_unique_hash(msg1), generate_unique_hash(msg3))
        self.assertNotEqual(generate_unique_hash(msg1, part_identifier="text"), generate_unique_hash(msg1, part_identifier="media_0"))

    def test_index_route(self):
        response = self.app.get('/')
        self.assertEqual(response.status_code, 200)

    def test_set_directory_invalid(self):
        response = self.app.post('/api/set-directory',
                                 data=json.dumps({'path': '/invalid/path'}),
                                 content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_api_stats(self):
        response = self.app.get('/api/stats')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data['total_messages'], 3)
        self.assertEqual(data['total_conversations'], 2)

    def test_api_conversations(self):
        response = self.app.get('/api/conversations')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["address"], "5559876543")

    def test_api_messages(self):
        response = self.app.get('/api/messages/5551234567')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["body"], "Hello")

    def test_api_search(self):
        response = self.app.get('/api/search?q=Test')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["body"], "Test message")

    def test_mms_extraction_from_parts(self):
        xml_content = """
        <smses>
            <mms address="5551112222" date="1678886400000" type="2">
                <parts>
                    <part ct="text/plain" text="Hello world"/>
                    <part ct="image/png" data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=" />
                </parts>
            </mms>
        </smses>
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            xml_path = os.path.join(temp_dir, "test_mms.xml")
            with open(xml_path, "w") as f:
                f.write(xml_content)

            app.config["XML_DIRECTORY"] = temp_dir
            app.config["DB_PATH"] = os.path.join(temp_dir, "test.db")
            
            init_db()
            process_xml_files()

            conn = sqlite3.connect(app.config["DB_PATH"])
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM messages WHERE address = '5551112222'")
            rows = cursor.fetchall()
            
            self.assertEqual(len(rows), 2)
            
            text_message = next((r for r in rows if r["body"] == "Hello world"), None)
            image_message = next((r for r in rows if r["mms_media_path"] is not None), None)

            self.assertIsNotNone(text_message)
            self.assertIsNone(text_message["mms_media_path"])

            self.assertIsNotNone(image_message)
            self.assertTrue(image_message["mms_media_path"].endswith(".png"))
            self.assertEqual(image_message["mms_media_type"], "image/png")
            
            media_file_path = os.path.join(temp_dir, "mms_media", image_message["mms_media_path"])
            self.assertTrue(os.path.exists(media_file_path))
            
            conn.close()

    def test_populate_contact_names_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_content = "First Name,Phone 1 - Value\nJean,\"(412) 600-2412\"\n"
            xml_content = '<smses><sms address="+14126002412" date="1445949377000" type="1" body="Test" /></smses>'
            
            with open(os.path.join(temp_dir, "contacts.csv"), "w") as f: f.write(csv_content)
            with open(os.path.join(temp_dir, "test.xml"), "w") as f: f.write(xml_content)

            app.config["XML_DIRECTORY"] = temp_dir
            db_path = os.path.join(temp_dir, "test.db")
            app.config["DB_PATH"] = db_path
            
            init_db()
            process_xml_files()

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM contact_names WHERE address = '4126002412'")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'Jean')
            conn.close()

    def test_get_conversations_api_with_contact_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_content = "First Name,Phone 1 - Value\nJean,\"(412) 600-2412\"\n"
            xml_content = '<smses><sms address="+14126002412" date="1445949377000" type="1" body="Test" /></smses>'
            
            with open(os.path.join(temp_dir, "contacts.csv"), "w") as f: f.write(csv_content)
            with open(os.path.join(temp_dir, "test.xml"), "w") as f: f.write(xml_content)

            app.config["XML_DIRECTORY"] = temp_dir
            app.config["DB_PATH"] = os.path.join(temp_dir, "test.db")
            
            init_db()
            process_xml_files()

            response = self.app.get('/api/conversations')
            data = json.loads(response.data)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]['contact_name'], 'Jean')

    def test_get_messages_api_with_contact_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_content = "First Name,Phone 1 - Value\nJean,\"(412) 600-2412\"\n"
            xml_content = '<smses><sms address="+14126002412" date="1445949377000" type="1" body="Test" /></smses>'
            
            with open(os.path.join(temp_dir, "contacts.csv"), "w") as f: f.write(csv_content)
            with open(os.path.join(temp_dir, "test.xml"), "w") as f: f.write(xml_content)

            app.config["XML_DIRECTORY"] = temp_dir
            app.config["DB_PATH"] = os.path.join(temp_dir, "test.db")
            
            init_db()
            process_xml_files()

            response = self.app.get('/api/messages/4126002412')
            data = json.loads(response.data)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]['contact_name'], 'Jean')

    def test_search_messages_api_with_contact_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_content = "First Name,Phone 1 - Value\nJean,\"(412) 600-2412\"\n"
            xml_content = '<smses><sms address="+14126002412" date="1445949377000" type="1" body="search term" /></smses>'
            
            with open(os.path.join(temp_dir, "contacts.csv"), "w") as f: f.write(csv_content)
            with open(os.path.join(temp_dir, "test.xml"), "w") as f: f.write(xml_content)

            app.config["XML_DIRECTORY"] = temp_dir
            app.config["DB_PATH"] = os.path.join(temp_dir, "test.db")
            
            init_db()
            process_xml_files()

            response = self.app.get('/api/search?q=search')
            data = json.loads(response.data)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]['contact_name'], 'Jean')

    def test_mms_sender_name_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_content = "First Name,Phone 1 - Value\nJean,\"(412) 600-2412\"\n"
            xml_content = '<smses><mms address="~14126002412~15551234567" date="1445949377000" type="2" from="+14126002412"><parts><part ct="text/plain" text="Group MMS test"/></parts></mms></smses>'
            
            with open(os.path.join(temp_dir, "contacts.csv"), "w") as f: f.write(csv_content)
            with open(os.path.join(temp_dir, "test.xml"), "w") as f: f.write(xml_content)

            app.config["XML_DIRECTORY"] = temp_dir
            app.config["DB_PATH"] = os.path.join(temp_dir, "test.db")
            
            init_db()
            process_xml_files()

            conn = sqlite3.connect(app.config["DB_PATH"])
            cursor = conn.cursor()
            cursor.execute("SELECT sender FROM messages WHERE address = '4126002412'")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'Jean')
            conn.close()

    def test_john_detrick_name_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_content = "First Name,Middle Name,Last Name,Phone 1 - Value\nJohn,Michael,Detrick,\"(717) 254-0330\"\n"
            xml_content = '<smses><sms address="+17172540330" date="1445949377000" type="1" body="Test" /></smses>'
            
            with open(os.path.join(temp_dir, "contacts.csv"), "w") as f: f.write(csv_content)
            with open(os.path.join(temp_dir, "test.xml"), "w") as f: f.write(xml_content)

            app.config["XML_DIRECTORY"] = temp_dir
            db_path = os.path.join(temp_dir, "test.db")
            app.config["DB_PATH"] = db_path
            
            init_db()
            process_xml_files()

            response = self.app.get('/api/messages/7172540330')
            data = json.loads(response.data)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]['contact_name'], 'John Michael Detrick')

if __name__ == '__main__':
    unittest.main()