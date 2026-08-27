import unittest
import os
import sqlite3
import shutil
import calendar
from datetime import datetime
import piexif
from app import app, init_db, process_xml_files

class TestMmsProcessing(unittest.TestCase):

    def setUp(self):
        """Set up a temporary test environment."""
        self.test_dir = 'temp_test_dir'
        os.makedirs(self.test_dir, exist_ok=True)
        
        # Copy test XMLs to the temp directory
        shutil.copy('test/sms-20160120004550 (DROID 3).xml', self.test_dir)
        shutil.copy('test/sms-20211223163832.xml', self.test_dir)

        # Configure the Flask app for testing
        app.config['TESTING'] = True
        app.config['XML_DIRECTORY'] = self.test_dir
        app.config['DB_PATH'] = os.path.join(self.test_dir, 'test_sms.db')
        
        # Initialize a fresh database for each test
        if os.path.exists(app.config['DB_PATH']):
            os.remove(app.config['DB_PATH'])
        init_db()

    def tearDown(self):
        """Clean up the test environment."""
        shutil.rmtree(self.test_dir)

    def test_deduplication_and_timestamping(self):
        """
        Test that duplicate media files are skipped and that timestamps
        of new files are corrected.
        """
        # --- Run the main processing function ---
        process_xml_files()

        # --- Verification ---
        conn = sqlite3.connect(app.config['DB_PATH'])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Verify Deduplication
        cursor.execute("SELECT COUNT(hash) FROM media_files")
        unique_files_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM messages WHERE mms_media_path IS NOT NULL")
        total_media_messages = cursor.fetchone()[0]

        media_dir = os.path.join(self.test_dir, 'mms_media')
        saved_files_count = len(os.listdir(media_dir))

        self.assertEqual(saved_files_count, unique_files_count, "Mismatch between saved files and unique hashes in DB.")
        self.assertLess(saved_files_count, total_media_messages, "Deduplication failed: number of saved files is not less than total media messages.")

        # 2. Verify Timestamp Correction
        cursor.execute("""
            SELECT m.date, mf.filepath
            FROM messages m
            JOIN media_files mf ON m.mms_media_path = mf.filepath
            WHERE m.mms_media_path IS NOT NULL
        """)
        media_messages = cursor.fetchall()
        
        self.assertTrue(len(media_messages) > 0, "No media messages found to test.")

        for msg in media_messages:
            mms_date_ms = msg['date']
            filepath = os.path.join(media_dir, msg['filepath'])
            
            # Check filesystem timestamp (as UTC)
            file_mod_time = os.path.getmtime(filepath)
            mms_utc_dt = datetime.utcfromtimestamp(mms_date_ms / 1000)
            mms_utc_timestamp = calendar.timegm(mms_utc_dt.utctimetuple())
            self.assertAlmostEqual(file_mod_time, mms_utc_timestamp, delta=1)

            # Check EXIF timestamp for JPEGs
            if filepath.lower().endswith(('.jpg', '.jpeg')):
                try:
                    exif_dict = piexif.load(filepath)
                    exif_date_str = exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal].decode('utf-8')
                    exif_date = datetime.strptime(exif_date_str, "%Y:%m:%d %H:%M:%S")
                    
                    mms_date = datetime.utcfromtimestamp(mms_date_ms / 1000)
                    
                    # Compare year, month, day, hour, minute, second
                    self.assertEqual(exif_date.strftime("%Y-%m-%d %H:%M:%S"), mms_date.strftime("%Y-%m-%d %H:%M:%S"))

                except Exception as e:
                    self.fail(f"Failed to read EXIF data from {msg['filepath']}: {e}")

        conn.close()

if __name__ == '__main__':
    unittest.main()
