import os
import sqlite3
import xml.etree.ElementTree as ET
import hashlib
import json
import re
import base64
import csv
from flask import Flask, request, jsonify, render_template, abort, send_from_directory
from threading import Thread
import webbrowser
import time
from io import StringIO

# --- Configuration ---
DB_NAME = 'sms_messages.db'

# --- Flask App Initialization ---
app = Flask(__name__, template_folder='.')

# --- Database Setup and Indexing Logic ---

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    db_path = app.config.get("DB_PATH")
    if not db_path:
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes and migrates the database schema."""
    db_path = app.config.get("DB_PATH")
    if not db_path or (not db_path == ":memory:" and not os.path.exists(os.path.dirname(db_path))):
        return False
    
    conn = get_db_connection()
    if conn is None: return False
    
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
    table_exists = cursor.fetchone()

    with conn:
        if not table_exists:
            conn.execute('''
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
                    date INTEGER NOT NULL, type INTEGER NOT NULL, body TEXT,
                    mms_media_path TEXT, mms_media_type TEXT, sender_address TEXT, readable_date TEXT,
                    unique_hash TEXT UNIQUE NOT NULL
                );
            ''')
        else:
            cursor.execute("PRAGMA table_info(messages)")
            columns = [info['name'] for info in cursor.fetchall()]
            if 'mms_media_path' not in columns:
                conn.execute('ALTER TABLE messages ADD COLUMN mms_media_path TEXT;')
            if 'mms_media_type' not in columns:
                conn.execute('ALTER TABLE messages ADD COLUMN mms_media_type TEXT;')
            if 'sender_address' not in columns:
                conn.execute('ALTER TABLE messages ADD COLUMN sender_address TEXT;')
            
            # Drop old, now-unused columns if they exist
            if 'contact_name' in columns:
                # SQLite doesn't have a simple DROP COLUMN, so we rebuild the table
                print("Schema migration: Rebuilding messages table to remove unused columns...")
                conn.execute('BEGIN TRANSACTION;')
                conn.execute('''
                    CREATE TABLE messages_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
                        date INTEGER NOT NULL, type INTEGER NOT NULL, body TEXT,
                        mms_media_path TEXT, mms_media_type TEXT, sender_address TEXT, readable_date TEXT,
                        unique_hash TEXT UNIQUE NOT NULL
                    );
                ''')
                # Copy data, carefully mapping old sender to new sender_address
                conn.execute('''
                    INSERT INTO messages_new (id, address, date, type, body, mms_media_path, mms_media_type, sender_address, readable_date, unique_hash)
                    SELECT id, address, date, type, body, mms_media_path, mms_media_type, sender, readable_date, unique_hash FROM messages;
                ''')
                conn.execute('DROP TABLE messages;')
                conn.execute('ALTER TABLE messages_new RENAME TO messages;')
                conn.execute('COMMIT;')
                print("Schema migration complete.")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS contact_names (
                address TEXT PRIMARY KEY, name TEXT NOT NULL
            );
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_address ON messages (address);')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_date ON messages (date);')
        
    conn.close()
    return True

indexing_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_file': ''
}

def normalize_number(phone_number):
    if not phone_number: return None
    
    # Handle the "~" character by taking the first part
    if '~' in phone_number:
        parts = phone_number.split('~')
        phone_number = next((part for part in parts if part), None)
        if phone_number is None:
            return None

    letter_map = {
        'a': '2', 'b': '2', 'c': '2', 'd': '3', 'e': '3', 'f': '3',
        'g': '4', 'h': '4', 'i': '4', 'j': '5', 'k': '5', 'l': '5',
        'm': '6', 'n': '6', 'o': '6', 'p': '7', 'q': '7', 'r': '7', 's': '7',
        't': '8', 'u': '8', 'v': '8', 'w': '9', 'x': '9', 'y': '9', 'z': '9',
    }
    
    numeric_string = "".join([letter_map.get(char, char) for char in phone_number.lower()])
    digits_only = re.sub(r'\D', '', numeric_string)

    if len(digits_only) >= 11 and digits_only.startswith('1'):
        return digits_only[1:]
    return digits_only

def generate_unique_hash(msg, part_identifier=""):
    data = f"{msg.get('address', '')}-{msg.get('date', '')}-{msg.get('body', '')}-{msg.get('type', '')}-{msg.get('sender','')}-{part_identifier}"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()

def safe_b64_decode(s: str) -> bytes:
    s = "".join(s.split())
    if not s: return b""
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
    except Exception:
        return b""

def sanitize_xml_file(filepath):
    """Reads an XML file line by line and removes invalid characters."""
    sanitized_lines = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                # Remove C0 and C1 control characters, except for tab, newline, and carriage return
                line = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', line)
                # Remove invalid XML character references like &#55550;
                line = re.sub(r'&\#\d+;', '', line)
                sanitized_lines.append(line)
    except UnicodeDecodeError:
        print(f"  [Warning] UnicodeDecodeError in {filepath}. Retrying with latin-1 encoding.")
        with open(filepath, 'r', encoding='latin-1') as f:
            for line in f:
                line = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', line)
                line = re.sub(r'&\#\d+;', '', line)
                sanitized_lines.append(line)

    return "".join(sanitized_lines)

def load_contacts_from_csv(directory):
    contact_map = {}
    csv_path = os.path.join(directory, 'contacts.csv')
    if not os.path.exists(csv_path):
        return contact_map

    print("Found contacts.csv, loading names...")
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = [h.lower().strip() for h in next(reader)]

            # --- Smart Name Column Detection ---
            name_col_indices = {
                'file_as': header.index('file as') if 'file as' in header else -1,
                'first_name': header.index('first name') if 'first name' in header else -1,
                'middle_name': header.index('middle name') if 'middle name' in header else -1,
                'last_name': header.index('last name') if 'last name' in header else -1,
                'nickname': header.index('nickname') if 'nickname' in header else -1
            }

            phone_cols = [i for i, col in enumerate(header) if 'phone' in col and 'value' in col]

            for row in reader:
                # --- Constructing the Best Display Name ---
                display_name = ""
                if name_col_indices['file_as'] != -1 and row[name_col_indices['file_as']].strip():
                    display_name = row[name_col_indices['file_as']].strip()
                else:
                    first_name = row[name_col_indices['first_name']].strip() if name_col_indices['first_name'] != -1 else ""
                    middle_name = row[name_col_indices['middle_name']].strip() if name_col_indices['middle_name'] != -1 else ""
                    last_name = row[name_col_indices['last_name']].strip() if name_col_indices['last_name'] != -1 else ""
                    
                    full_name_parts = [first_name, middle_name, last_name]
                    full_name = " ".join(filter(None, full_name_parts)).strip()

                    if full_name:
                        display_name = full_name
                    elif name_col_indices['nickname'] != -1 and row[name_col_indices['nickname']].strip():
                        display_name = row[name_col_indices['nickname']].strip()

                if not display_name:
                    continue

                # --- Comprehensive Phone Number Parsing ---
                for col_idx in phone_cols:
                    phone_number = row[col_idx]
                    if phone_number:
                        for num in phone_number.split(':::'):
                            normalized = normalize_number(num)
                            if normalized:
                                contact_map[normalized] = display_name
    except Exception as e:
        print(f"Error reading contacts.csv: {e}")

    print(f"Loaded {len(contact_map)} unique numbers from contacts.csv")
    return contact_map

def process_xml_files():
    global indexing_status
    indexing_status.update({'running': True, 'progress': 0, 'current_file': ''})
    
    xml_directory = app.config.get("XML_DIRECTORY")
    media_dir = os.path.join(xml_directory, 'mms_media')
    os.makedirs(media_dir, exist_ok=True)

    xml_files = [f for f in os.listdir(xml_directory) if f.lower().endswith('.xml')]
    indexing_status['total'] = len(xml_files)
    
    conn = get_db_connection()
    all_contact_names = load_contacts_from_csv(xml_directory)
    total_mms_count, total_media_files_found = 0, 0
    
    if all_contact_names:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO contact_names (address, name) VALUES (?, ?)",
                all_contact_names.items()
            )
    
    for i, filename in enumerate(xml_files):
        indexing_status.update({'current_file': f"Processing: {filename}", 'progress': i + 1})
        filepath = os.path.join(xml_directory, filename)
        
        malformed_elements = 0
        messages_to_insert = []
        try:
            sanitized_content = sanitize_xml_file(filepath)
            
            for event, elem in ET.iterparse(StringIO(sanitized_content), events=('end',)):
                try:
                    if elem.tag not in ['sms', 'mms']: continue
                    date = int(elem.get('date'))
                    address = elem.get('address')
                    normalized_address = normalize_number(address)
                    if not normalized_address: continue

                    msg_data = {
                        'address': normalized_address, 'date': date, 'type': elem.get('type', '1'),
                        'readable_date': elem.get('readable_date'), 'body': None,
                        'mms_media_path': None, 'mms_media_type': None, 'sender_address': None
                    }

                    if elem.tag == 'sms':
                        msg_data.update({
                            'body': elem.get('body', ''),
                            'sender_address': normalized_address
                        })
                        msg_data['unique_hash'] = generate_unique_hash(msg_data, part_identifier='sms')
                        messages_to_insert.append(msg_data)
                    
                    elif elem.tag == 'mms':
                        total_mms_count += 1
                        from_address = normalize_number(elem.get('from', address.split('~')[0]))
                        base_msg_data = {
                            'address': normalized_address, 'date': date, 'type': elem.get('type', '2'),
                            'readable_date': elem.get('readable_date'),
                            'sender_address': from_address
                        }

                        mms_body_parts = []
                        media_parts = []
                        
                        parts_elem = elem.find('parts')
                        if parts_elem is not None:
                            for part in parts_elem:
                                if part.tag == 'part':
                                    ct = part.get('ct', '').lower()
                                    text = part.get('text')
                                    data = part.get('data')

                                    if ct == 'text/plain' and text:
                                        mms_body_parts.append(text)
                                    elif ct.startswith(('image/', 'video/', 'audio/')) and data:
                                        media_parts.append({'ct': ct, 'data': data})

                        if mms_body_parts:
                            text_msg = base_msg_data.copy()
                            text_msg['body'] = " | ".join(mms_body_parts)
                            text_msg['mms_media_path'] = None
                            text_msg['mms_media_type'] = None
                            text_msg['unique_hash'] = generate_unique_hash(text_msg, part_identifier="text")
                            messages_to_insert.append(text_msg)

                        for i, part in enumerate(media_parts):
                            media_msg = base_msg_data.copy()
                            decoded_data = safe_b64_decode(part['data'])
                            if decoded_data:
                                total_media_files_found += 1
                                ext = part['ct'].split('/')[-1]
                                media_filename = f"{date}_{normalized_address}_{total_media_files_found}.{ext}"
                                media_filepath = os.path.join(media_dir, media_filename)
                                with open(media_filepath, 'wb') as f: f.write(decoded_data)
                                
                                media_msg['body'] = ""
                                media_msg['mms_media_path'] = media_filename
                                media_msg['mms_media_type'] = part['ct']
                                media_msg['unique_hash'] = generate_unique_hash(media_msg, part_identifier=f"media_{i}")
                                messages_to_insert.append(media_msg)
                        
                        continue

                except (ValueError, TypeError):
                    malformed_elements += 1
                    continue
                finally:
                    pass
            
            if messages_to_insert:
                with conn:
                    conn.executemany('''
                        INSERT OR IGNORE INTO messages (address, date, type, body, mms_media_path, mms_media_type, sender_address, readable_date, unique_hash)
                        VALUES (:address, :date, :type, :body, :mms_media_path, :mms_media_type, :sender_address, :readable_date, :unique_hash)
                    ''', messages_to_insert)
            
            print(f"  File '{filename}' ({i+1}/{len(xml_files)}) complete. Skipped {malformed_elements} malformed messages.")

        except ET.ParseError as e:
            print(f"  [Critical Failure] Could not parse '{filename}': {e}. Skipping file.")

    print(f"\nProcessed {total_mms_count} MMS messages, found {total_media_files_found} media files")
    
    with conn:
        total_messages = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        total_mms_media = conn.execute('SELECT COUNT(*) FROM messages WHERE mms_media_path IS NOT NULL').fetchone()[0]
        print(f"\n=== INDEXING COMPLETE ===")
        print(f"Total messages in DB: {total_messages}")
        print(f"MMS media files in DB: {total_mms_media}")

    conn.close()
    indexing_status['running'] = False

# --- Flask API Endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/set-directory', methods=['POST'])
def set_directory():
    path = request.json.get('path')
    if not path or not os.path.isdir(path): abort(400, 'Invalid directory path provided.')
    app.config['XML_DIRECTORY'] = path
    app.config['DB_PATH'] = os.path.join(path, DB_NAME)
    if not init_db(): abort(500, 'Failed to initialize database.')
    return jsonify({'db_exists': os.path.exists(app.config['DB_PATH'])})
    
@app.route('/api/start-indexing', methods=['POST'])
def start_indexing():
    if indexing_status['running']: return jsonify({'message': 'Indexing is already in progress.'}), 409
    if request.json.get('rebuild', False):
        db_path = app.config.get("DB_PATH")
        if db_path and os.path.exists(db_path):
            os.remove(db_path)
            init_db()
    Thread(target=process_xml_files).start()
    return jsonify({'message': 'Indexing started.'})

@app.route('/api/indexing-status')
def get_indexing_status():
    return jsonify(indexing_status)

@app.route('/api/stats')
def get_stats():
    conn = get_db_connection()
    if conn is None: return jsonify({'error': 'Could not connect to database.'}), 500
    try:
        stats = {
            'total_messages': conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
            'total_conversations': conn.execute('SELECT COUNT(DISTINCT address) FROM messages').fetchone()[0],
            'total_with_names': conn.execute('SELECT COUNT(DISTINCT address) FROM contact_names').fetchone()[0],
            'total_mms_images': conn.execute('SELECT COUNT(*) FROM messages WHERE mms_media_path IS NOT NULL').fetchone()[0],
            'db_size_mb': round(os.path.getsize(app.config["DB_PATH"]) / (1024*1024), 2)
        }
        return jsonify(stats)
    except sqlite3.OperationalError:
         return jsonify({'error': 'Database is not initialized correctly.'}), 500
    finally:
        conn.close()

@app.route('/api/media/<path:filename>')
def get_media(filename):
    media_dir = os.path.join(app.config.get("XML_DIRECTORY"), 'mms_media')
    return send_from_directory(media_dir, filename)

@app.route('/api/conversations')
def get_conversations():
    conn = get_db_connection()
    if conn is None: return jsonify({'error': 'Database not available.'}), 500
    query = '''
        SELECT 
            m.address, 
            COALESCE(c.name, m.address) as contact_name, 
            MAX(m.date) as last_message_date, 
            COUNT(m.id) as message_count,
            SUM(CASE WHEN m.mms_media_path IS NOT NULL THEN 1 ELSE 0 END) as image_count
        FROM messages m
        LEFT JOIN contact_names c ON m.address = c.address
        GROUP BY m.address 
        ORDER BY last_message_date DESC
    '''
    rows = conn.execute(query).fetchall()
    conversations = [dict(row) for row in rows]
    conn.close()
    return jsonify(conversations)

@app.route('/api/messages/<address>')
def get_messages(address):
    conn = get_db_connection()
    if conn is None: return jsonify({'error': 'Database not available.'}), 500
    query = '''
        SELECT 
            m.id, m.address, m.date, m.type, m.body, m.mms_media_path, m.mms_media_type, m.readable_date,
            COALESCE(c.name, m.address) as contact_name,
            COALESCE(s.name, m.sender_address) as sender
        FROM messages m
        LEFT JOIN contact_names c ON m.address = c.address
        LEFT JOIN contact_names s ON m.sender_address = s.address
        WHERE m.address = ? 
        ORDER BY m.date ASC
    '''
    rows = conn.execute(query, (address,)).fetchall()
    messages = [dict(row) for row in rows]
    conn.close()
    return jsonify(messages)
    
@app.route('/api/search')
def search_messages():
    query_term = request.args.get('q', '')
    if len(query_term) < 3: return jsonify({'error': 'Search term must be at least 3 characters long.'}), 400
    conn = get_db_connection()
    if conn is None: return jsonify({'error': 'Database not available.'}), 500
    query = '''
        SELECT 
            m.id, m.address, m.date, m.type, m.body, m.mms_media_path, m.mms_media_type, m.readable_date,
            COALESCE(c.name, m.address) as contact_name,
            COALESCE(s.name, m.sender_address) as sender
        FROM messages m
        LEFT JOIN contact_names c ON m.address = c.address
        LEFT JOIN contact_names s ON m.sender_address = s.address
        WHERE m.body LIKE ? 
        ORDER BY m.date DESC 
        LIMIT 100
    '''
    rows = conn.execute(query, (f'%{query_term}%',)).fetchall()
    results = [dict(row) for row in rows]
    conn.close()
    return jsonify(results)

# --- Main Execution ---
if __name__ == '__main__':
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    def open_browser():
        time.sleep(1)
        webbrowser.open_new("http://127.0.0.1:5000")
    Thread(target=open_browser).start()
    app.run(debug=False, port=5000)