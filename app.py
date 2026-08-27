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
import sys
import subprocess
from datetime import datetime
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()  # Load variables from a local .env file (gitignored) if present.
DB_NAME = 'sms_messages.db'

# Bump this whenever the cache layout or DB schema changes in a way that makes
# an old cache unusable. assess_cache() rebuilds automatically on a mismatch.
# v3: unique_hash now derives from normalized fields (participants/sender)
#     instead of the raw address, so overlapping archives de-duplicate
#     correctly. Old caches hold duplicate rows and must be rebuilt.
CACHE_FORMAT_VERSION = 3

# Your own phone number (digits only), used to identify "you" in group
# conversations. Set MY_PHONE_NUMBER in a local .env file — never hardcode it.
MY_PHONE_NUMBER = os.environ.get('MY_PHONE_NUMBER', '')


# --- Local cache location -------------------------------------------------
# Generated data (the SQLite index and extracted media) is kept in a LOCAL
# app-data folder, never inside the archive folder the user points at. This
# avoids SQLite "disk I/O error" failures on cloud-synced folders (e.g. Google
# Drive) and keeps the user's backup folder pristine. Each archive gets its own
# cache keyed by the absolute path it was indexed from.
def get_cache_root():
    """Return the base folder for all caches, per-OS conventions."""
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support/SMSViewer')
    elif os.name == 'nt':
        base = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'SMSViewer')
    else:
        base = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')), 'SMSViewer')
    return base


def get_cache_dir_for(source_dir):
    """A stable, per-archive cache directory derived from the source path."""
    key = hashlib.sha256(os.path.abspath(source_dir).encode('utf-8')).hexdigest()[:16]
    return os.path.join(get_cache_root(), key)


def build_source_manifest(source_dir):
    """A fingerprint of the archive's XML files (name -> size + mtime).
    If this changes, the cache is stale and should be rebuilt."""
    manifest = {}
    try:
        for f in sorted(os.listdir(source_dir)):
            if f.lower().endswith('.xml'):
                try:
                    st = os.stat(os.path.join(source_dir, f))
                    manifest[f] = {'size': st.st_size, 'mtime': int(st.st_mtime)}
                except OSError:
                    continue
    except OSError:
        pass
    return manifest


# --- Cache management -----------------------------------------------------
def _dir_size_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _read_cache_meta(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        meta = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM cache_meta')}
        conn.close()
        return meta
    except sqlite3.Error:
        return {}


def _cache_dir_for_hash(cache_hash):
    """Resolve a cache hash to its directory, guarding against path traversal."""
    root = os.path.abspath(get_cache_root())
    cdir = os.path.abspath(os.path.join(root, cache_hash or ''))
    if os.path.dirname(cdir) != root or not os.path.isdir(cdir):
        return None
    return cdir


def list_caches():
    """Every cache under the cache root, with size and whether its source
    folder still exists."""
    root = get_cache_root()
    caches = []
    if not os.path.isdir(root):
        return caches
    current_db = app.config.get('DB_PATH')
    current_dir = os.path.dirname(current_db) if current_db else None
    for name in sorted(os.listdir(root)):
        cdir = os.path.join(root, name)
        db_path = os.path.join(cdir, DB_NAME)
        if not os.path.isdir(cdir) or not os.path.exists(db_path):
            continue
        meta = _read_cache_meta(db_path)
        src = meta.get('source_dir', '')
        caches.append({
            'hash': name,
            'source_dir': src,
            'source_exists': bool(src) and os.path.isdir(src),
            'indexed_at': meta.get('indexed_at', ''),
            'message_count': meta.get('message_count', ''),
            'size_bytes': _dir_size_bytes(cdir),
            'is_current': bool(current_dir) and os.path.abspath(cdir) == os.path.abspath(current_dir),
            'date_range': _cache_date_range(db_path, meta),
        })
    return caches


def _fmt_date(ms):
    """Epoch milliseconds -> 'YYYY-MM-DD' (or '' on bad input)."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0).strftime('%Y-%m-%d')
    except (ValueError, TypeError, OSError):
        return ''


def _cache_date_range(db_path, meta):
    """Human date span for a cache: prefer stored meta, fall back to a query."""
    dmin, dmax = meta.get('date_min'), meta.get('date_max')
    if not dmin or not dmax:
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            r = conn.execute('SELECT MIN(date), MAX(date) FROM messages').fetchone()
            conn.close()
            dmin, dmax = (r[0], r[1]) if r else (None, None)
        except sqlite3.Error:
            return ''
    a, b = _fmt_date(dmin), _fmt_date(dmax)
    return f"{a} to {b}" if a and b else ''


# --- Spam list (blacklist) ------------------------------------------------
# spamnumbers.txt lives in the ARCHIVE folder (next to the .xml / contacts.csv),
# so it is portable and travels with the archive. Numbers are matched with the
# same normalization as everything else (country code stripped, etc.), and
# contacts.csv acts as a whitelist that overrides the spam list.
SPAM_FILENAME = 'spamnumbers.txt'


def _spam_file_path(xml_directory):
    return os.path.join(xml_directory, SPAM_FILENAME)


def load_spam_numbers(xml_directory):
    """Normalized set of spam numbers from spamnumbers.txt (blank/# lines ignored)."""
    spam = set()
    if not xml_directory:
        return spam
    path = _spam_file_path(xml_directory)
    if not os.path.exists(path):
        return spam
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                n = normalize_number(line)
                if n:
                    spam.add(n)
    except OSError:
        pass
    return spam


def add_spam_number(xml_directory, number):
    """Append a normalized number to spamnumbers.txt if not already present."""
    n = normalize_number(number)
    if not n or n in load_spam_numbers(xml_directory):
        return None
    try:
        with open(_spam_file_path(xml_directory), 'a', encoding='utf-8') as f:
            f.write(n + '\n')
        return n
    except OSError:
        return None


def remove_spam_number(xml_directory, number):
    """Remove every line matching `number` (normalized); keep comments/blanks."""
    target = normalize_number(number)
    path = _spam_file_path(xml_directory)
    if not target or not os.path.exists(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        kept = [ln for ln in lines if normalize_number(ln.strip()) != target]
        if len(kept) == len(lines):
            return False
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(kept)
        return True
    except OSError:
        return False


def effective_spam_set(xml_directory, conn):
    """Spam numbers that are NOT whitelisted by a contacts.csv entry."""
    spam = load_spam_numbers(xml_directory)
    if not spam:
        return set()
    contacts = {r['address'] for r in conn.execute('SELECT address FROM contact_names')}
    return {n for n in spam if n not in contacts}

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
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # Robustness: WAL improves concurrent read/write and resilience; a busy
        # timeout prevents transient "database is locked" errors from failing.
        if db_path != ":memory:":
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA busy_timeout=30000')
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
            conn.execute('''
                CREATE TABLE IF NOT EXISTS cache_meta (
                    key TEXT PRIMARY KEY, value TEXT
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
    'progress': 0,          # files processed so far
    'total': 0,             # total files to process
    'current_file': '',
    'phase': 'idle',        # idle | starting | parsing | finalizing | done | error
    'messages_indexed': 0,
    'media_extracted': 0,
    'date_range': '',       # date span of the file currently being processed
    'error': None,
}


def assess_cache(source_dir):
    """Inspect the cache for `source_dir` and decide whether it can be reused.

    Returns a dict with a `status`:
      absent   - no cache yet (first run) -> build
      ready    - healthy and matches the current backups -> use as-is
      stale    - backups changed since indexing -> rebuild recommended
      outdated - built by an older cache format -> rebuild
      corrupt  - failed the integrity check -> rebuild
    """
    cache_dir = get_cache_dir_for(source_dir)
    db_path = os.path.join(cache_dir, DB_NAME)
    if not os.path.exists(db_path):
        return {'status': 'absent'}
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        integrity = conn.execute('PRAGMA quick_check').fetchone()[0]
        if integrity != 'ok':
            return {'status': 'corrupt', 'reason': 'failed integrity check'}
        has_meta = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cache_meta'"
        ).fetchone()
        if not has_meta:
            return {'status': 'outdated', 'reason': 'built by an older version (no metadata)'}
        meta = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM cache_meta')}
        if str(meta.get('format_version')) != str(CACHE_FORMAT_VERSION):
            return {'status': 'outdated', 'reason': 'cache format changed'}
        stored = json.loads(meta.get('source_manifest') or '{}')
        if stored != build_source_manifest(source_dir):
            return {'status': 'stale', 'reason': 'backup files changed since last index',
                    'message_count': meta.get('message_count'), 'indexed_at': meta.get('indexed_at')}
        return {'status': 'ready', 'message_count': meta.get('message_count'),
                'indexed_at': meta.get('indexed_at')}
    except (sqlite3.Error, ValueError) as e:
        return {'status': 'corrupt', 'reason': str(e)}
    finally:
        if conn:
            conn.close()

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
    """Stable identity for a logical message, used to de-duplicate across
    overlapping archives (the same conversation history often lands in several
    backups — e.g. carried between phones by a restore).

    Built ONLY from NORMALIZED fields, never the raw `address`: different
    backup apps / phones format the same number differently ('(412) 656-4424'
    vs '4126564424') and list group participants in a different order
    ('A~B' vs 'B~A'). The raw address therefore varies for what is really the
    same message, which defeated de-duplication. `participants` is already
    normalized and sorted, and `sender_address` is already normalized, so the
    same message hashes identically no matter which archive it came from.
    """
    data = "-".join(str(x) for x in (
        msg.get('participants', '') or '',
        msg.get('date', '') or '',
        msg.get('body', '') or '',
        msg.get('type', '') or '',
        msg.get('sender_address', '') or '',
        part_identifier,
    ))
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
    indexing_status.update({
        'running': True, 'progress': 0, 'current_file': '', 'phase': 'starting',
        'messages_indexed': 0, 'media_extracted': 0, 'date_range': '', 'error': None,
    })

    xml_directory = app.config.get("XML_DIRECTORY")
    media_dir = app.config.get("MEDIA_DIR")
    os.makedirs(media_dir, exist_ok=True)

    xml_files = [f for f in os.listdir(xml_directory) if f.lower().endswith('.xml')]
    indexing_status['total'] = len(xml_files)

    conn = get_db_connection()
    all_contact_names = load_contacts_from_csv(xml_directory)
    # Spam numbers (blacklist) minus contacts (whitelist wins). Spam media is
    # still extracted, but segregated into a flat 'Spam/' folder.
    effective_spam = {n for n in load_spam_numbers(xml_directory) if n not in all_contact_names}
    total_mms_count, total_media_files_found = 0, 0
    processed_media_hashes = set()
    
    if all_contact_names:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO contact_names (address, name) VALUES (?, ?)",
                all_contact_names.items()
            )
    
    for i, filename in enumerate(xml_files):
        indexing_status.update({'current_file': filename, 'progress': i + 1, 'phase': 'parsing'})
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
                        participant_addrs = set()
                        for addr in participant_nodes:
                            address_val = addr.get('address')
                            if address_val and not re.search('[a-zA-Z]', address_val):
                                participant_addrs.add(normalize_number(address_val))
                        
                        if MY_PHONE_NUMBER:
                            participant_addrs.discard(MY_PHONE_NUMBER)
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
                            dt_object = datetime.fromtimestamp(date / 1000.0)
                            date_name = dt_object.strftime('%Y-%m-%d %H-%M-%S')

                            if from_address in effective_spam:
                                # All spam media in one flat 'Spam' folder, filename
                                # prefixed with the spam number for identification.
                                sender_folder = "Spam"
                                base_filename = f"{from_address}_{date_name}"
                            else:
                                sender_folder = "Sent" if message_type == '2' else (sender_name or "Unknown Sender")
                                base_filename = date_name
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
            indexing_status['messages_indexed'] += len(messages_to_insert)

        min_date_str = datetime.fromtimestamp(min_date / 1000.0).strftime('%Y-%m-%d') if min_date else "N/A"
        max_date_str = datetime.fromtimestamp(max_date / 1000.0).strftime('%Y-%m-%d') if max_date else "N/A"
        indexing_status.update({
            'media_extracted': total_media_files_found,
            'date_range': f"{min_date_str} to {max_date_str}",
        })
        app.logger.info(f"  File '{filename}' ({i+1}/{len(xml_files)}) complete. Contains: {min_date_str} to {max_date_str}.")

    indexing_status['phase'] = 'finalizing'
    app.logger.info(f"\nProcessed {total_mms_count} MMS messages, found {total_media_files_found} media files")

    with conn:
        total_messages = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        total_mms_media = conn.execute('SELECT COUNT(*) FROM messages WHERE mms_media_path IS NOT NULL').fetchone()[0]
        drow = conn.execute('SELECT MIN(date), MAX(date) FROM messages').fetchone()
        # Record cache metadata so this cache can be validated/reused next time.
        meta = {
            'format_version': str(CACHE_FORMAT_VERSION),
            'source_dir': xml_directory,
            'source_manifest': json.dumps(build_source_manifest(xml_directory)),
            'indexed_at': datetime.now().isoformat(timespec='seconds'),
            'message_count': str(total_messages),
            'date_min': str(drow[0]) if drow and drow[0] else '',
            'date_max': str(drow[1]) if drow and drow[1] else '',
        }
        conn.executemany('INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)', list(meta.items()))
        app.logger.info(f"\n=== INDEXING COMPLETE ===")
        app.logger.info(f"Total messages in DB: {total_messages}")
        app.logger.info(f"MMS media files in DB: {total_mms_media}")

    conn.close()
    indexing_status.update({
        'messages_indexed': total_messages, 'media_extracted': total_media_files_found,
        'phase': 'done', 'current_file': '',
    })

def run_indexing():
    """Wrapper around process_xml_files that never leaves the status stuck
    'running' if something unexpected fails."""
    global indexing_status
    try:
        process_xml_files()
    except Exception as e:
        app.logger.error(f"Indexing failed: {e}", exc_info=True)
        indexing_status.update({'phase': 'error', 'error': str(e)})
    finally:
        indexing_status['running'] = False


# --- Flask API Endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/browse-folder', methods=['POST'])
def browse_folder():
    """Open a NATIVE folder chooser on the machine running the server (this is a
    local app, so that's the user's own computer) and return the chosen path.
    Returns {'path': ''} if the user cancels."""
    start = (request.json or {}).get('start') or os.path.expanduser('~')
    if not os.path.isdir(start):
        start = os.path.expanduser('~')
    try:
        if sys.platform == 'darwin':
            # AppleScript: reliable, no extra dependencies. `default location`
            # seeds the dialog at the current path if there is one.
            script = (
                'set startFolder to POSIX file "%s" as alias\n'
                'POSIX path of (choose folder with prompt '
                '"Select your SMS backup folder" default location startFolder)'
                % start.replace('"', '\\"')
            )
            r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
            # Non-zero return code is normal when the user clicks Cancel.
            path = r.stdout.strip() if r.returncode == 0 else ''
        elif os.name == 'nt':
            ps = (
                'Add-Type -AssemblyName System.Windows.Forms;'
                '$d = New-Object System.Windows.Forms.FolderBrowserDialog;'
                'if ($d.ShowDialog() -eq "OK") { Write-Output $d.SelectedPath }'
            )
            r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                               capture_output=True, text=True)
            path = r.stdout.strip() if r.returncode == 0 else ''
        else:
            # Linux/other: fall back to a Tk dialog if available.
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw()
            path = filedialog.askdirectory(initialdir=start) or ''
            root.destroy()
        return jsonify({'path': path.rstrip('/') if path else ''})
    except Exception as e:
        app.logger.error(f"Folder picker failed: {e}")
        return jsonify({'error': 'The folder picker is unavailable; paste a path instead.'}), 500

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

    # Generated data lives in a LOCAL cache, never inside the (possibly
    # cloud-synced) archive folder. This is what fixes the "disk I/O error".
    cache_dir = get_cache_dir_for(path)
    os.makedirs(cache_dir, exist_ok=True)
    app.config['XML_DIRECTORY'] = path
    app.config['DB_PATH'] = os.path.join(cache_dir, DB_NAME)
    app.config['MEDIA_DIR'] = os.path.join(cache_dir, 'media')
    os.makedirs(app.config['MEDIA_DIR'], exist_ok=True)
    app.logger.info(f"XML_DIRECTORY set to: {path}")
    app.logger.info(f"Cache directory: {cache_dir}")

    try:
        # Assess the existing cache BEFORE init_db creates an empty database.
        assessment = assess_cache(path)
        app.logger.info(f"Cache status: {assessment}")

        # Ensure a schema-current DB exists, unless the existing one is corrupt
        # (in which case a rebuild will delete and recreate it).
        if assessment['status'] != 'corrupt':
            if not init_db():
                app.logger.error("Failed to initialize database.")
                return jsonify({'error': 'Failed to initialize database. Check server logs for details.'}), 500

        return jsonify({'cache': assessment, 'cache_dir': cache_dir})
    except Exception as e:
        app.logger.error(f"An unexpected error occurred in set_directory: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected server error occurred.'}), 500
    
@app.route('/api/start-indexing', methods=['POST'])
def start_indexing():
    if indexing_status['running']: return jsonify({'message': 'Indexing is already in progress.'}), 409
    if request.json.get('rebuild', False):
        db_path = app.config.get("DB_PATH")
        media_dir = app.config.get("MEDIA_DIR")

        # Remove the DB and its WAL sidecar files, plus all extracted media.
        if db_path:
            for suffix in ('', '-wal', '-shm'):
                p = db_path + suffix
                if os.path.exists(p):
                    os.remove(p)

        if media_dir and os.path.exists(media_dir):
            shutil.rmtree(media_dir)
        if media_dir:
            os.makedirs(media_dir, exist_ok=True)

        init_db()
    Thread(target=run_indexing).start()
    return jsonify({'message': 'Indexing started.'})

@app.route('/api/indexing-status')
def get_indexing_status():
    return jsonify(indexing_status)

@app.route('/api/caches')
def get_caches():
    caches = list_caches()
    return jsonify({
        'cache_root': get_cache_root(),
        'total_size_bytes': sum(c['size_bytes'] for c in caches),
        'caches': caches,
    })

@app.route('/api/caches/delete', methods=['POST'])
def delete_cache():
    cache_hash = (request.json or {}).get('hash', '')
    cdir = _cache_dir_for_hash(cache_hash)
    if not cdir:
        return jsonify({'error': 'Unknown cache.'}), 400
    freed = _dir_size_bytes(cdir)
    try:
        shutil.rmtree(cdir)
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    app.logger.info(f"Deleted cache {cache_hash} (freed {freed} bytes)")
    return jsonify({'removed': cache_hash, 'freed_bytes': freed})

@app.route('/api/caches/prune', methods=['POST'])
def prune_caches():
    """Delete caches whose source folder no longer exists (self-healing)."""
    removed, freed = [], 0
    for c in list_caches():
        if not c['source_exists']:
            cdir = _cache_dir_for_hash(c['hash'])
            if cdir:
                try:
                    shutil.rmtree(cdir)
                    removed.append(c['hash'])
                    freed += c['size_bytes']
                except OSError:
                    pass
    app.logger.info(f"Pruned {len(removed)} orphaned cache(s), freed {freed} bytes")
    return jsonify({'removed': removed, 'freed_bytes': freed})

@app.route('/api/spam')
def get_spam():
    xml_dir = app.config.get('XML_DIRECTORY')
    if not xml_dir:
        return jsonify({'error': 'No archive is open.'}), 400
    spam = sorted(load_spam_numbers(xml_dir))
    names = {}
    conn = get_db_connection()
    if conn is not None:
        try:
            names = {r['address']: r['name'] for r in conn.execute('SELECT address, name FROM contact_names')}
        finally:
            conn.close()
    # A spam number that is also a contact is "whitelisted" — shown anyway.
    items = [{'number': n, 'contact_name': names.get(n), 'whitelisted': n in names} for n in spam]
    return jsonify({'spam': items, 'file': _spam_file_path(xml_dir)})

@app.route('/api/spam/add', methods=['POST'])
def add_spam():
    xml_dir = app.config.get('XML_DIRECTORY')
    if not xml_dir:
        return jsonify({'error': 'No archive is open.'}), 400
    body = request.json or {}
    added = []
    if body.get('participants'):
        # A conversation's participants string is '~'-joined normalized numbers.
        for part in str(body['participants']).split('~'):
            if part.strip():
                n = add_spam_number(xml_dir, part)
                if n:
                    added.append(n)
    elif body.get('number') is not None:
        # A single number, possibly with spaces/punctuation — normalize as one.
        n = add_spam_number(xml_dir, str(body['number']))
        if n:
            added.append(n)
    return jsonify({'added': added})

@app.route('/api/spam/remove', methods=['POST'])
def remove_spam():
    xml_dir = app.config.get('XML_DIRECTORY')
    if not xml_dir:
        return jsonify({'error': 'No archive is open.'}), 400
    number = (request.json or {}).get('number', '')
    return jsonify({'removed': remove_spam_number(xml_dir, number)})

@app.route('/api/stats')
def get_stats():
    conn = get_db_connection()
    if conn is None: return jsonify({'error': 'Could not connect to database.'}), 500
    try:
        drow = conn.execute('SELECT MIN(date), MAX(date) FROM messages').fetchone()
        date_min, date_max = (drow[0], drow[1]) if drow else (None, None)
        stats = {
            'total_messages': conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
            'total_conversations': conn.execute('SELECT COUNT(DISTINCT participants) FROM messages').fetchone()[0],
            'total_with_names': conn.execute('SELECT COUNT(DISTINCT address) FROM contact_names').fetchone()[0],
            'total_mms_images': conn.execute('SELECT COUNT(*) FROM messages WHERE mms_media_path IS NOT NULL').fetchone()[0],
            'db_size_mb': round(os.path.getsize(app.config["DB_PATH"]) / (1024*1024), 2),
            'date_range': f"{_fmt_date(date_min)} to {_fmt_date(date_max)}" if date_min and date_max else '',
        }
        return jsonify(stats)
    except sqlite3.OperationalError:
         return jsonify({'error': 'Database is not initialized correctly.'}), 500
    finally:
        conn.close()

@app.route('/api/media/<path:filename>')
def get_media(filename):
    media_dir = app.config.get("MEDIA_DIR")
    return send_from_directory(media_dir, filename)

@app.route('/api/export-media', methods=['POST'])
def export_media():
    """Copy the sender-organized media out of the local cache into a browsable
    `mms_media/` folder (defaults to the archive folder). On-demand only."""
    media_dir = app.config.get("MEDIA_DIR")
    xml_dir = app.config.get("XML_DIRECTORY")
    if not media_dir or not os.path.isdir(media_dir) or not os.listdir(media_dir):
        return jsonify({'error': 'No extracted media to export yet. Index an archive first.'}), 400

    dest_root = (request.json or {}).get('destination') or xml_dir
    if not dest_root or not os.path.isdir(dest_root):
        return jsonify({'error': 'Export destination folder not found.'}), 400
    dest = os.path.join(dest_root, 'mms_media')

    try:
        count = 0
        for root, _dirs, files in os.walk(media_dir):
            rel = os.path.relpath(root, media_dir)
            target_dir = dest if rel == '.' else os.path.join(dest, rel)
            os.makedirs(target_dir, exist_ok=True)
            for fn in files:
                shutil.copy2(os.path.join(root, fn), os.path.join(target_dir, fn))
                count += 1
        app.logger.info(f"Exported {count} media files to {dest}")
        return jsonify({'exported': count, 'destination': dest})
    except OSError as e:
        app.logger.error(f"Media export failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/conversations')
def get_conversations():
    conn = get_db_connection()
    if conn is None: return jsonify({'error': 'Database not available.'}), 500
    
    try:
        eff_spam = effective_spam_set(app.config.get('XML_DIRECTORY'), conn)
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

            # Hide conversations whose participants are all spam (contacts override).
            if eff_spam and all(p in eff_spam for p in participants_list):
                continue

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
        eff_spam = effective_spam_set(app.config.get('XML_DIRECTORY'), conn)

        def _is_spam(participants_str):
            parts = (participants_str or '').split('~')
            return bool(eff_spam) and bool(parts) and all(p in eff_spam for p in parts)

        conv_rows = conn.execute(conv_query, (f'%{query_term}%',)).fetchall()
        conversations = [dict(row) for row in conv_rows if not _is_spam(row['address'])]

        # Search for messages by body content
        msg_query = '''
            SELECT
                m.id, m.address, m.date, m.type, m.body, m.mms_media_path, m.mms_media_type, m.readable_date,
                m.participants,
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
        messages = [dict(row) for row in msg_rows if not _is_spam(row['participants'])]

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
