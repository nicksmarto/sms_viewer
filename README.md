# SMS Backup Viewer

## 1. Overview

SMS Backup Viewer is a web-based application designed to provide a fast, searchable, and user-friendly interface for browsing large archives of SMS and MMS messages backed up from mobile devices. Standard backup formats are often multi-gigabyte XML files that are slow and difficult to navigate. This tool solves that problem by indexing the messages into an efficient local database, extracting media attachments, and presenting the conversations in a familiar, modern messaging interface.

## 2. Core Features

*   **High-Performance Indexing:** Parses large, multi-gigabyte XML backup files (`sms.xml`) quickly and stores the data in a local SQLite database for instant access.
*   **Rich Conversation View:** Displays conversations grouped by contact, mimicking the look and feel of modern messaging applications.
*   **MMS Media Support:** Automatically extracts images, videos, and audio from MMS messages, saves them to a local cache, and displays them inline.
*   **Full-Text Search:** Provides a powerful and fast search function to find messages across all conversations.
*   **Contact Name Resolution:** Intelligently resolves phone numbers to contact names using data from the XML files and an optional `contacts.csv` file for enrichment.
*   **Group Chat Display:** Correctly identifies group chats and displays the name of the sender for each message.
*   **Robust Error Handling:** Gracefully handles corrupted XML files and malformed messages, ensuring that a few bad entries do not prevent the entire archive from being indexed.
*   **Web-Based UI:** A clean, modern, and intuitive user interface that runs in any web browser.

## 3. Technical Stack

*   **Backend:** Python with Flask
*   **Database:** SQLite
*   **Frontend:** HTML, Tailwind CSS, and vanilla JavaScript
*   **XML Parsing:** Python's built-in `xml.etree.ElementTree`

## 4. Setup and Usage

### Prerequisites

*   Python 3.x
*   A web browser


