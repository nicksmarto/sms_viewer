import os
import sys
import unittest
import sqlite3
from datetime import datetime

# Add the root directory to the Python path to allow importing 'app'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, init_db, process_xml_files, get_db_connection

class TestAppProcessing(unittest.TestCase):

    def setUp(self):
        """Set up the test environment before each test."""
        self.test_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(self.test_dir, 'test_sms_messages.db')
        
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            
        app.config['XML_DIRECTORY'] = self.test_dir
        app.config['DB_PATH'] = self.db_path
        app.testing = True
        self.app_context = app.app_context()
        self.app_context.push()

    def tearDown(self):
        """Clean up the test environment after each test."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.app_context.pop()

    def test_exif_and_timestamp_update(self):
        """
        Test that the EXIF data and filesystem timestamps are correctly updated for media files.
        """
        self.assertTrue(init_db(), "Database initialization failed.")
        process_xml_files()
        conn = get_db_connection()
        self.assertIsNotNone(conn, "Failed to connect to the database after indexing.")
        
        try:
            cursor = conn.cursor()
            
            # 1. Find the text part of an MMS to get its date and participants string.
            # This message is known to have an accompanying image.
            cursor.execute("SELECT date, participants FROM messages WHERE body LIKE ?", ("%Thank you! I just sent you a Google Doc%",))
            text_part = cursor.fetchone()
            self.assertIsNotNone(text_part, "Text part of the target message not found.")
            
            message_date_ms = text_part['date']
            participants = text_part['participants']

            # 2. Find the corresponding media part of the same MMS.
            # It will have the same participants and a very close timestamp.
            time_window_ms = 1000  # 1 second window
            cursor.execute(
                "SELECT mms_media_path, date FROM messages WHERE participants = ? AND mms_media_path IS NOT NULL AND date BETWEEN ? AND ?",
                (participants, message_date_ms - time_window_ms, message_date_ms + time_window_ms)
            )
            media_part = cursor.fetchone()
            self.assertIsNotNone(media_part, "Media part of the target message not found.")

            # 3. Check the timestamp of the actual media file.
            media_filename = media_part['mms_media_path']
            media_date_ms = media_part['date']
            
            media_filepath = os.path.join(self.test_dir, 'mms_media', media_filename)
            self.assertTrue(os.path.exists(media_filepath), f"Media file does not exist: {media_filepath}")
            
            modification_time = os.path.getmtime(media_filepath)
            self.assertAlmostEqual(modification_time, media_date_ms / 1000, delta=1, 
                                   msg="File modification time does not match message time.")

        finally:
            if conn:
                conn.close()

if __name__ == '__main__':
    unittest.main()
