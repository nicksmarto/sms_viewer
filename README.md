# SMS Backup Viewer

A local, web-based viewer for your text-message history. Point it at a folder of backup
files and browse, search, and read your entire SMS/MMS archive in a clean, modern
messaging-style interface — with MMS photos and videos extracted and shown inline.

> ### ⚠️ Built specifically for **SMS Backup & Restore**
> This tool reads the **XML files produced by the [SMS Backup & Restore](https://play.google.com/store/apps/details?id=com.riteshsahu.SMSBackupRestore&hl=en_US)
> Android app** (by Ritesh Sahu). It expects that app's XML schema. Backups from other
> apps or in other formats (e.g. iOS, PDF, or CSV-only exports) are **not** supported.

It runs entirely on your own machine — there is no hosted/online version and nothing is
ever uploaded. It's a Flask app you run locally that opens in your web browser.

---

## Features

- **High-performance indexing** — parses large, multi-gigabyte XML backups and stores them in a local SQLite database for instant browsing and search.
- **Familiar conversation view** — messages grouped by contact, styled like a modern messaging app, with timestamps and sender/receiver alignment.
- **MMS media** — automatically extracts images, videos, and audio from MMS and displays them inline.
- **Full-text search** — search across every message, and filter conversations by contact name.
- **Contact-name resolution** — turns phone numbers into names using the backup data plus an optional `contacts.csv`.
- **Group-chat support** — correctly identifies group conversations and their participants.
- **Media deduplication & EXIF handling** — skips duplicate images (common across overlapping backups) and preserves capture timestamps.
- **Robust parsing** — skips corrupted/malformed entries instead of failing the whole file.

---

## Requirements

- **Python 3.9+**
- A modern web browser
- One or more `.xml` backup files exported by **SMS Backup & Restore**

---

## Installation

The GitHub repository *is* the application — to use it, you download the code and run it
locally on your own computer against your own backups.

**1. Get the code**

```bash
git clone https://github.com/nicksmarto/sms_viewer.git
cd sms_viewer
```

(Or use the green **Code → Download ZIP** button on GitHub and unzip it.)

**2. Create a virtual environment and install dependencies**

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**3. (Optional) Set your own phone number**

This lets the app recognize *you* in group conversations. Copy the example env file and
fill it in — your `.env` stays local and is never committed:

```bash
cp .env.example .env
# then edit .env and set MY_PHONE_NUMBER=your10digitnumber
```

---

## Preparing your backups

1. In the **SMS Backup & Restore** app on your phone, create a backup of your messages and
   choose the **XML** format (this is the app's default).
2. Transfer the resulting `.xml` file(s) to a folder on your computer. You can put several
   backups in the same folder — the viewer merges and de-duplicates them.
3. *(Optional)* Export your contacts (e.g. from Google Contacts) as **`contacts.csv`** and
   place it in that same folder for nicer name resolution.

---

## Usage

**1. Start the app**

```bash
python3 app.py
```

Your browser opens automatically at **http://127.0.0.1:5000** (if not, open that URL).

**2. Point it at your backups**

In the app, enter the **full path to the folder** that contains your `.xml` files
(and optional `contacts.csv`), then start indexing.

- **First run:** the app parses the XML, builds a search index, and extracts MMS media.
- **Later runs:** it reuses the existing index instantly. There's a **rebuild** option if
  you add new backups.

**3. Browse and search**

Pick a conversation on the left; read and search messages on the right. MMS images and
videos appear inline.

### What gets created, and where

The app writes two things **inside the folder you point it at**:

| Item | What it is |
|------|-----------|
| `sms_messages.db` | The local SQLite search index |
| `mms_media/` | Extracted MMS attachments, organized by sender |

Both are regenerated from your XML and can be safely deleted (a rebuild recreates them).

---

## Privacy

- **This repository contains no personal message data** — only the application code.
- Your backups, the generated database, and extracted media **never leave your machine**.
- The included `.gitignore` prevents message archives (`*.xml`), the database (`*.db`),
  extracted media (`mms_media/`), `contacts.csv`, and your `.env` from ever being committed
  — so if you fork or push this project, your data stays private by default.

---

## How it works

A small **Flask** backend parses the XML with **lxml**, normalizes and de-duplicates
messages into **SQLite**, extracts MMS parts to disk, and resolves phone numbers to names.
The frontend is a single **HTML + Tailwind CSS + vanilla JavaScript** page that talks to a
small JSON API. See `app.py` for the backend and `index.html` for the UI.

## Development

Run the test suite:

```bash
python -m unittest discover -s test
```

## Roadmap

Planned improvements are tracked in [`BACKLOG.md`](BACKLOG.md).

## Versioning

This project uses [Semantic Versioning](https://semver.org/). Released versions are marked
with Git tags (`v1.0.0`, `v2.0.0`, …).
