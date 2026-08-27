import os
import sqlite3
from lxml import etree
import hashlib
import json
import re
import base64
import csv
from flask import Flask, request, jsonify, render_template, abort, send_from_directory
from threading import Thread
import shutil
import webbrowser
import time
from io import StringIO
import logging
from datetime import datetime

# --- Configuration ---
DB_NAME = 'sms_messages.db'

# --- Flask App Initialization ---
app = Flask(__name__, template_folder='.')
logging.basicConfig(level=logging.INFO)

# --- Database Setup and Indexing Logic ---

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    db_path = app.config.get("DB_PATH")
    if not db_path:
        app.logger.error("DB_PATH not configured.")
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        app.logger.error(f"Database connection error: {e}")
        return None

def init_db():
    """Initializes and migrates the database schema."""
    app.logger.info("Initializing database...")
    db_path = app.config.get("DB_PATH")
    if not db_path:
        app.logger.error("DB_PATH not set for init_db.")
        return False
    
    if not db_path == ":memory:" and not os.path.exists(os.path.dirname(db_path)):
        app.logger.error(f"Database directory does not exist: {os.path.dirname(db_path)}")
        return False
    
    try:
        conn = get_db_connection()
        if conn is None: return False
        
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
        table_exists = cursor.fetchone()

        with conn:
            if not table_exists:
                app.logger.info("Creating new 'messages' table.")
                conn.execute('''
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
                        date INTEGER NOT NULL, type INTEGER NOT NULL, body TEXT,
                        mms_media_path TEXT, mms_media_type TEXT, sender_address TEXT, readable_date TEXT,
                        unique_hash TEXT UNIQUE NOT NULL, participants TEXT
                    );
                ''')
            else:
                app.logger.info("'messages' table already exists. Checking for schema migrations.")
                cursor.execute("PRAGMA table_info(messages)")
                columns = [info['name'] for info in cursor.fetchall()]
                if 'participants' not in columns:
                    app.logger.info("Adding 'participants' column.")
                    conn.execute('ALTER TABLE messages ADD COLUMN participants TEXT;')
                    conn.execute('UPDATE messages SET participants = address;')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS contact_names (
                    address TEXT PRIMARY KEY, name TEXT NOT NULL
                );
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_participants ON messages (participants);')
        
        app.logger.info("Database initialization complete.")
        return True
    except sqlite3.Error as e:
        app.logger.error(f"Database initialization failed: {e}")
        return False
    finally:
        if conn:
            conn.close()

indexing_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_file': ''
}

def normalize_number(phone_number):
    if not phone_number: return None
    
    # Handle RCS addresses by extracting the numeric part before the @ sign
    if '@rcs.google.com' in phone_number:
        base_number = phone_number.split('@')[0]
        # If the base_number contains non-digit characters, it's likely an invalid/special address.
        if not base_number.isdigit():
            # Returning None will cause this participant to be skipped.
            return None
        phone_number = base_number

    # Handle group conversations
    if '~' in phone_number:
        numbers = [re.sub(r'\D', '', num) for num in phone_number.split('~') if num]
        normalized_numbers = [normalize_number(n) for n in numbers]
        # Sort for consistency
        normalized_numbers.sort()
        return '~'.join(filter(None, normalized_numbers))

    letter_map = {
        'a': '2', 'b': '2', 'c': '2', 'd': '3', 'e': '3', 'f': '3',
        'g': '4', 'h': '4', 'i': '4', 'j': '5', 'k': '5', 'l': '5',
        'm': '6', 'n': '6', 'o': '6', 'p': '7', 'q': '7', 'r': '7', 's': '7',
        't': '8', 'u': '8', 'v': '8', 'w': '9', 'x': '9', 'y': '9', 'z': '9',
    }
    
    numeric_string = "".join([letter_map.get(char, char) for char in phone_number.lower()])
    digits_only = re.sub(r'\D', '', numeric_string)

    if len(digits_only) == 11 and digits_only.startswith('1'):
        return digits_only[1:]
    return digits_only

def generate_unique_hash(msg, part_identifier=""):
    data = f"{msg.get('address', '')}-{msg.get('date', '')}-{msg.get('body', '')}-{msg.get('type', '')}-{msg.get('sender','')}-{part_identifier}"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()

def safe_b64_decode(s: str) -> bytes:
    """Safely decodes a base64 string, handling padding and whitespace."""
    if not s:
        return b""
    s = "".join(s.split())
    try:
        # Add padding if necessary
        padding = len(s) % 4
        if padding != 0:
            s += "=" * (4 - padding)
        return base64.b64decode(s, validate=True)
    except (base64.binascii.Error, ValueError) as e:
        app.logger.warning(f"Base64 decoding failed: {e}. Input (first 50 chars): '{s[:50]}'")
        return b""

def sanitize_xml_file(filepath):
    """Reads an XML file and yields sanitized lines, attempting multiple encodings."""
    encodings_to_try = ['utf-8', 'latin-1', 'iso-8859-1']
    for encoding in encodings_to_try:
        try:
            with open(filepath, 'r', encoding=encoding) as f:
                for line in f:
                    # Remove invalid XML characters (control characters)
                    yield re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', line)
            return  # Success
        except UnicodeDecodeError:
            app.logger.warning(f"UnicodeDecodeError with {encoding} for {filepath}. Trying next encoding.")
            continue
    app.logger.error(f"Could not decode {filepath} with any of the attempted encodings.")

def load_contacts_from_csv(directory):
    csv_path = os.path.join(directory, 'contacts.csv')
    if not os.path.exists(csv_path):
        return {}

    print("Found contacts.csv, loading and consolidating names...")
    name_to_numbers = {}
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = [h.lower().strip() for h in next(reader)]

            name_col_indices = {
                'file_as': header.index('file as') if 'file as' in header else -1,
                'first_name': header.index('first name') if 'first name' in header else -1,
                'middle_name': header.index('middle name') if 'middle name' in header else -1,
                'last_name': header.index('last name') if 'last name' in header else -1,
                'nickname': header.index('nickname') if 'nickname' in header else -1
            }
            phone_cols = [i for i, col in enumerate(header) if 'phone' in col and 'value' in col]

            for row in reader:
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

                if display_name not in name_to_numbers:
                    name_to_numbers[display_name] = set()

                for col_idx in phone_cols:
                    phone_number = row[col_idx]
                    if phone_number:
                        for num in phone_number.split(':::'):
                            normalized = normalize_number(num)
                            if normalized:
                                name_to_numbers[display_name].add(normalized)
    except Exception as e:
        print(f"Error reading contacts.csv: {e}")
        return {}

    # --- Consolidate contact names using a Disjoint Set Union (DSU) approach ---
    parent = {name: name for name in name_to_numbers.keys()}

    def find_set(name):
        if parent[name] == name:
            return name
        parent[name] = find_set(parent[name])
        return parent[name]

    def unite_sets(name1, name2):
        root1 = find_set(name1)
        root2 = find_set(name2)
        if root1 != root2:
            # Union by rank/size: shorter name (or fewer words) points to longer one
            if len(root1.split()) < len(root2.split()) or (len(root1.split()) == len(root2.split()) and len(root1) < len(root2)):
                parent[root1] = root2
            else:
                parent[root2] = root1
    
    sorted_names = sorted(name_to_numbers.keys(), key=lambda x: (-len(x.split()), -len(x)))

    for i in range(len(sorted_names)):
        for j in range(i + 1, len(sorted_names)):
            name_a = sorted_names[i]
            name_b = sorted_names[j]

            # Clean names for comparison
            clean_name_a = name_a.lower().replace('(', '').replace(')', '')
            clean_name_b = name_b.lower().replace('(', '').replace(')', '')
            
            parts_a_set = set(clean_name_a.split())
            parts_b_set = set(clean_name_b.split())

            # Heuristic 1: Subset (e.g., "John Doe" is a subset of "John F. Doe")
            if parts_b_set.issubset(parts_a_set):
                unite_sets(name_a, name_b)
                continue

            # Heuristic 2: Prefix on first name, same last name
            parts_a = clean_name_a.split()
            parts_b = clean_name_b.split()
            if len(parts_a) > 0 and len(parts_b) > 0 and parts_a[-1] == parts_b[-1]:
                if parts_a[0].startswith(parts_b[0]) or parts_b[0].startswith(parts_a[0]):
                    unite_sets(name_a, name_b)
                    continue

    # Build the final contact_map
    contact_map = {}
    for name, numbers in name_to_numbers.items():
        canonical_name = find_set(name)
        for number in numbers:
            # If number is already mapped, prefer the longer canonical name
            if number not in contact_map or len(canonical_name) > len(contact_map.get(number, '')):
                contact_map[number] = canonical_name

    print(f"Loaded and consolidated to {len(contact_map)} unique numbers from contacts.csv")
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
    processed_media_hashes = set()
    
    if all_contact_names:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO contact_names (address, name) VALUES (?, ?)",
                all_contact_names.items()
            )
    
    for i, filename in enumerate(xml_files):
        indexing_status.update({'current_file': f"Processing: {filename}", 'progress': i + 1})
        filepath = os.path.join(xml_directory, filename)
        
        messages_to_insert = []
        min_date, max_date = None, None
        
        # Use a dummy root to handle files with multiple root elements
        sanitized_content = "<root>" + "".join(sanitize_xml_file(filepath)) + "</root>"
        
        try:
            parser = etree.XMLParser(recover=True, huge_tree=True)
            root = etree.fromstring(sanitized_content.encode('utf-8'), parser=parser)

            for elem in root.iter('sms', 'mms'):
                try:
                    date_str = elem.get('date')
                    if not date_str or not date_str.isdigit(): continue
                    date = int(date_str)

                    if min_date is None or date < min_date: min_date = date
                    if max_date is None or date > max_date: max_date = date

                    address = elem.get('address')
                    
                    # Skip normalization if the address is likely a name (contains letters) and not a group chat
                    if '~' not in address and re.search('[a-zA-Z]', address):
                        normalized_address = None
                    else:
                        normalized_address = normalize_number(address)

                    if not normalized_address: continue

                    if elem.tag == 'sms':
                        msg_data = {
                            'address': address, 'date': date, 'type': elem.get('type', '1'),
                            'readable_date': elem.get('readable_date'), 'body': elem.get('body', ''),
                            'mms_media_path': None, 'mms_media_type': None, 
                            'sender_address': normalized_address, 'participants': normalized_address
                        }
                        msg_data['unique_hash'] = generate_unique_hash(msg_data, part_identifier='sms')
                        messages_to_insert.append(msg_data)
                    
                    elif elem.tag == 'mms':
                        total_mms_count += 1
                        
                        participant_nodes = elem.findall(".//addr")
                        participant_addrs = {normalize_number(addr.get('address')) for addr in participant_nodes if addr.get('address')}
                        
                        my_number = '' # This should be configurable
                        participant_addrs.discard(my_number)
                        participants = '~'.join(sorted(list(filter(None, participant_addrs)))) or normalized_address

                        from_address_node = elem.find(".//addr[@type='137']")
                        from_address = normalize_number(from_address_node.get('address')) if from_address_node is not None else None
                        
                        message_type = elem.get('msg_box', '1')

                        base_msg_data = {
                            'address': address, 'date': date, 'type': message_type,
                            'readable_date': elem.get('readable_date'),
                            'sender_address': from_address, 'participants': participants
                        }

                        mms_body_parts = []
                        media_parts = []
                        
                        for part in elem.iter('part'):
                            ct = part.get('ct', '').lower()
                            text = part.get('text')
                            data = part.get('data')

                            if ct == 'text/plain' and text:
                                mms_body_parts.append(text)
                            elif ct.startswith(('image/', 'video/', 'audio/')) and data:
                                media_parts.append({'ct': ct, 'data': data})

                        if mms_body_parts:
                            text_msg = base_msg_data.copy()
                            text_msg.update({
                                'body': " | ".join(mms_body_parts), 'mms_media_path': None, 'mms_media_type': None
                            })
                            text_msg['unique_hash'] = generate_unique_hash(text_msg, part_identifier="text")
                            messages_to_insert.append(text_msg)

                        for part in media_parts:
                            decoded_data = safe_b64_decode(part['data'])
                            if not decoded_data: continue

                            file_hash = hashlib.sha256(decoded_data).hexdigest()
                            if file_hash in processed_media_hashes: continue
                            processed_media_hashes.add(file_hash)
                            total_media_files_found += 1

                            media_msg = base_msg_data.copy()
                            
                            sender_name = all_contact_names.get(from_address, from_address)
                            sender_folder = "Sent" if message_type == '2' else (sender_name or "Unknown Sender")
                            
                            dt_object = datetime.fromtimestamp(date / 1000.0)
                            base_filename = dt_object.strftime('%Y-%m-%d %H-%M-%S')
                            extension = part['ct'].split('/')[-1]
                            
                            sender_dir = os.path.join(media_dir, sender_folder)
                            os.makedirs(sender_dir, exist_ok=True)
                            
                            final_filename, unique_suffix = f"{base_filename}.{extension}", 1
                            while os.path.exists(os.path.join(sender_dir, final_filename)):
                                final_filename = f"{base_filename}_{unique_suffix}.{extension}"
                                unique_suffix += 1
                            
                            media_filepath = os.path.join(sender_dir, final_filename)
                            with open(media_filepath, 'wb') as f: f.write(decoded_data)
                            
                            # Set the modification time of the file to the message date
                            os.utime(media_filepath, (date / 1000, date / 1000))
                            
                            media_msg.update({
                                'body': "", 'mms_media_path': os.path.join(sender_folder, final_filename),
                                'mms_media_type': part['ct']
                            })
                            media_msg['unique_hash'] = generate_unique_hash(media_msg, part_identifier=f"media_{file_hash}")
                            messages_to_insert.append(media_msg)

                except (ValueError, TypeError, AttributeError) as e:
                    app.logger.warning(f"Skipping malformed message element in {filename}: {e}")
                    continue
        
        except etree.XMLSyntaxError as e:
            app.logger.error(f"XML syntax error in {filename}: {e}")

        if messages_to_insert:
            with conn:
                conn.executemany('''
                    INSERT OR IGNORE INTO messages (address, date, type, body, mms_media_path, mms_media_type, sender_address, readable_date, unique_hash, participants)
                    VALUES (:address, :date, :type, :body, :mms_media_path, :mms_media_type, :sender_address, :readable_date, :unique_hash, :participants)
                ''', messages_to_insert)
        
        min_date_str = datetime.fromtimestamp(min_date / 1000.0).strftime('%Y-%m-%d') if min_date else "N/A"
        max_date_str = datetime.fromtimestamp(max_date / 1000.0).strftime('%Y-%m-%d') if max_date else "N/A"
        app.logger.info(f"  File '{filename}' ({i+1}/{len(xml_files)}) complete. Contains: {min_date_str} to {max_date_str}.")

    app.logger.info(f"\nProcessed {total_mms_count} MMS messages, found {total_media_files_found} media files")
    
    with conn:
        total_messages = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        total_mms_media = conn.execute('SELECT COUNT(*) FROM messages WHERE mms_media_path IS NOT NULL').fetchone()[0]
        app.logger.info(f"\n=== INDEXING COMPLETE ===")
        app.logger.info(f"Total messages in DB: {total_messages}")
        app.logger.info(f"MMS media files in DB: {total_mms_media}")

    conn.close()
    indexing_status['running'] = False

# --- Flask API Endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/set-directory', methods=['POST'])
def set_directory():
    path = request.json.get('path')
    app.logger.info(f"Received request to set directory to: {path}")

    if not path or not isinstance(path, str):
        app.logger.warning("Invalid path provided: path is empty or not a string.")
        return jsonify({'error': 'Invalid path provided.'}), 400

    if not os.path.isdir(path):
        app.logger.warning(f"Directory does not exist: {path}")
        return jsonify({'error': f"Directory not found: {path}"}), 400

    app.config['XML_DIRECTORY'] = path
    app.config['DB_PATH'] = os.path.join(path, DB_NAME)
    app.logger.info(f"XML_DIRECTORY set to: {app.config['XML_DIRECTORY']}")
    app.logger.info(f"DB_PATH set to: {app.config['DB_PATH']}")

    try:
        db_exists_before_init = os.path.exists(app.config['DB_PATH'])
        app.logger.info(f"Database exists before init: {db_exists_before_init}")
        
        if not init_db():
            app.logger.error("Failed to initialize database.")
            return jsonify({'error': 'Failed to initialize database. Check server logs for details.'}), 500
        
        return jsonify({'db_exists': db_exists_before_init})
    except Exception as e:
        app.logger.error(f"An unexpected error occurred in set_directory: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected server error occurred.'}), 500
    
@app.route('/api/start-indexing', methods=['POST'])
def start_indexing():
    if indexing_status['running']: return jsonify({'message': 'Indexing is already in progress.'}), 409
    if request.json.get('rebuild', False):
        xml_directory = app.config.get("XML_DIRECTORY")
        db_path = app.config.get("DB_PATH")
        media_dir = os.path.join(xml_directory, 'mms_media')

        if db_path and os.path.exists(db_path):
            os.remove(db_path)
        
        if os.path.exists(media_dir):
            shutil.rmtree(media_dir)

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
            'total_conversations': conn.execute('SELECT COUNT(DISTINCT participants) FROM messages').fetchone()[0],
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
    
    try:
        query = '''
            SELECT 
                participants as address, 
                MAX(date) as last_message_date, 
                COUNT(id) as message_count,
                SUM(CASE WHEN mms_media_path IS NOT NULL THEN 1 ELSE 0 END) as image_count
            FROM messages
            GROUP BY participants 
            ORDER BY last_message_date DESC
        '''
        rows = conn.execute(query).fetchall()
        
        conversations = []
        for row in rows:
            row_dict = dict(row)
            participants_list = row_dict['address'].split('~')
            
            # Create a string of placeholders for the query, e.g., (?,?,?)
            placeholders = ','.join('?' for _ in participants_list)
            
            # Query for names for the participants in this specific conversation
            contacts_query = f"SELECT name, address FROM contact_names WHERE address IN ({placeholders})"
            contacts_rows = conn.execute(contacts_query, participants_list).fetchall()
            
            # Build a map for found contacts
            found_contacts = {row['address']: row['name'] for row in contacts_rows}
            
            # Get names, falling back to the number if not found
            names = [found_contacts.get(p, p) for p in participants_list]
            
            # Deduplicate, sort, and join
            row_dict['contact_name'] = ', '.join(sorted(list(set(names))))
            conversations.append(row_dict)
            
        return jsonify(conversations)
    finally:
        if conn:
            conn.close()

@app.route('/api/messages/<participants>')
def get_messages(participants):
    conn = get_db_connection()
    if conn is None: return jsonify({'error': 'Database not available.'}), 500
    
    try:
        query = '''
            SELECT 
                m.id, m.address, m.date, m.type, m.body, m.mms_media_path, m.mms_media_type, m.readable_date,
                -- When the message is received (type=1), the sender is the person from sender_address.
                -- When the message is sent (type=2), the sender is 'You'.
                CASE 
                    WHEN m.type = 2 THEN 'You' 
                    ELSE COALESCE(s.name, m.sender_address) 
                END as sender,
                -- contact_name should also reflect the sender's name for received messages.
                COALESCE(s.name, m.sender_address) as contact_name
            FROM messages m
            LEFT JOIN contact_names s ON m.sender_address = s.address
            WHERE m.participants = ? 
            ORDER BY m.date ASC
        '''
        rows = conn.execute(query, (participants,)).fetchall()
        messages = [dict(row) for row in rows]
        return jsonify(messages)
    finally:
        if conn:
            conn.close()
    
@app.route('/api/search')
def search_messages():
    query_term = request.args.get('q', '')
    if len(query_term) < 3:
        return jsonify({'error': 'Search term must be at least 3 characters long.'}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({'error': 'Database not available.'}), 500

    try:
        # Search for conversations by contact name
        conv_query = '''
            SELECT 
                participants as address, 
                MAX(date) as last_message_date, 
                COUNT(id) as message_count,
                SUM(CASE WHEN mms_media_path IS NOT NULL THEN 1 ELSE 0 END) as image_count,
                COALESCE(cn.name, participants) as contact_name
            FROM messages
            LEFT JOIN contact_names cn ON messages.participants = cn.address
            WHERE contact_name LIKE ?
            GROUP BY participants 
            ORDER BY last_message_date DESC
        '''
        conv_rows = conn.execute(conv_query, (f'%{query_term}%',)).fetchall()
        conversations = [dict(row) for row in conv_rows]

        # Search for messages by body content
        msg_query = '''
            SELECT 
                m.id, m.address, m.date, m.type, m.body, m.mms_media_path, m.mms_media_type, m.readable_date,
                COALESCE(c.name, m.address) as contact_name,
                COALESCE(s.name, m.sender_address) as sender
            FROM messages m
            LEFT JOIN contact_names c ON m.participants = c.address
            LEFT JOIN contact_names s ON m.sender_address = s.address
            WHERE m.body LIKE ?
            ORDER BY m.date DESC 
            LIMIT 100
        '''
        msg_rows = conn.execute(msg_query, (f'%{query_term}%',)).fetchall()
        messages = [dict(row) for row in msg_rows]

        return jsonify({'conversations': conversations, 'messages': messages})
    finally:
        if conn:
            conn.close()

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
