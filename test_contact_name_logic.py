import unittest
import os
import tempfile
import sqlite3
from app import app, init_db, load_contacts_from_csv

class TestContactNameLogic(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        app.config['XML_DIRECTORY'] = self.test_dir.name
        app.config['DB_PATH'] = os.path.join(self.test_dir.name, 'test.db')
        init_db()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_name_priority_file_as(self):
        csv_content = "File As,First Name,Last Name,Phone 1 - Value\nJD,John,Doe,5551112222\n"
        with open(os.path.join(self.test_dir.name, 'contacts.csv'), 'w') as f:
            f.write(csv_content)
        
        contact_map = load_contacts_from_csv(self.test_dir.name)
        self.assertEqual(contact_map['5551112222'], 'JD')

    def test_name_priority_full_name(self):
        csv_content = "File As,First Name,Middle Name,Last Name,Phone 1 - Value\n,John,Michael,Doe,5551112222\n"
        with open(os.path.join(self.test_dir.name, 'contacts.csv'), 'w') as f:
            f.write(csv_content)
        
        contact_map = load_contacts_from_csv(self.test_dir.name)
        self.assertEqual(contact_map['5551112222'], 'John Michael Doe')

    def test_name_priority_nickname(self):
        csv_content = "File As,First Name,Last Name,Nickname,Phone 1 - Value\n,,,Johnny,5551112222\n"
        with open(os.path.join(self.test_dir.name, 'contacts.csv'), 'w') as f:
            f.write(csv_content)
        
        contact_map = load_contacts_from_csv(self.test_dir.name)
        self.assertEqual(contact_map['5551112222'], 'Johnny')

    def test_multiple_phone_numbers(self):
        csv_content = "First Name,Phone 1 - Value,Phone 2 - Value\nJane,\"5553334444 ::: (555) 444-5555\",1-555-666-7777\n"
        with open(os.path.join(self.test_dir.name, 'contacts.csv'), 'w') as f:
            f.write(csv_content)
        
        contact_map = load_contacts_from_csv(self.test_dir.name)
        self.assertEqual(contact_map['5553334444'], 'Jane')
        self.assertEqual(contact_map['5554445555'], 'Jane')
        self.assertEqual(contact_map['5556667777'], 'Jane')

if __name__ == '__main__':
    unittest.main()
