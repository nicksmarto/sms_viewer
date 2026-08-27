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
- **Media deduplication** — skips duplicate images (common across overlapping backups) by content hash, and sets each extracted file's date to when the message was received.
- **Robust parsing** — skips corrupted/malformed entries instead of failing the whole file.

---

## Requirements

- **Python 3.9+**
- A modern web browser
- One or more `.xml` backup files exported by **SMS Backup & Restore**

The GitHub repository *is* the application — to use it, you download the code and run it
locally on your own computer against your own backups. First, **get the code**: either
`git clone https://github.com/nicksmarto/sms_viewer.git`, or use the green
**Code → Download ZIP** button on GitHub and unzip it.

---

## Quick start (the easy way — no terminal)

Once you have the folder, just:

- **macOS:** double-click **`run.command`**
- **Windows:** double-click **`run.bat`**

The **first** launch sets everything up automatically (it creates an isolated Python
environment and installs the dependencies — this takes a minute). **Every** launch after
that starts the app instantly and opens your browser at **http://127.0.0.1:5000**. Keep
the small terminal window that appears open while you use the app; closing it stops the app.

> **macOS note:** if double-clicking shows a security warning (because the file was
> downloaded), right-click `run.command` → **Open** → **Open** — just the first time. If
> your Mac opens the file in a text editor instead of running it, right-click →
> **Open With → Terminal**.

You still need **Python 3** installed (see Requirements above); everything else is
automatic. Prefer to run things yourself, or curious what the launcher does under the
hood? See **Manual setup** below.

---

## Manual setup (optional)

The launcher above does all of this for you. Follow these steps only if you'd rather set
things up by hand or want to understand what's happening.

**1. Create a virtual environment and install dependencies**

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**2. (Optional) Set your own phone number**

This lets the app recognize *you* in group conversations. Copy the example env file and
fill it in — your `.env` stays local and is never committed:

```bash
cp .env.example .env
# then edit .env and set MY_PHONE_NUMBER=your10digitnumber
```

**3. Start the app**

```bash
python app.py
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

Double-click **`run.command`** (macOS) or **`run.bat`** (Windows) — or, if you did the
manual setup, run `python app.py` from your activated environment.

Your browser opens automatically at **http://127.0.0.1:5000** (if not, open that URL).

**2. Point it at your backups**

Click **Browse…** to choose the folder that contains your `.xml` files (and optional
`contacts.csv`) in a native folder picker — or paste the path — then load it.

- **First run:** the app parses the XML, builds the search index, and extracts MMS media
  (with a live progress readout).
- **Later runs:** it reuses the existing cache instantly. If your backup files have changed
  it offers to rebuild, and there's always a manual **Rebuild Index** button.

**3. Browse and search**

Pick a conversation on the left; read and search messages on the right. MMS images and
videos appear inline.

### Where your data and the cache live

The app **never writes into your backup folder.** It reads your `.xml` files (and an
optional `contacts.csv`) and writes everything it generates — the SQLite search index and
the extracted MMS media — into a **local cache** outside your archive, at
`~/Library/Application Support/SMSViewer/<per-archive>/` on macOS.

This is deliberate: it keeps your backups untouched and avoids database errors on
cloud-synced folders (e.g. Google Drive), where SQLite can't run reliably. The cache is
disposable — delete it, or click **Rebuild Index**, and it regenerates from your XML.

For a browsable, sender-organized copy of your media saved *into* your archive folder, use
the **Export media to folder** button in the app.

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

For *why* the tool is built the way it is — the design principles, key decisions, and
assumptions — see **[DESIGN.md](DESIGN.md)**.

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
