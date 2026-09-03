# THE BLOCK 105 RADIO – Cue Sheet Converter

## What it does
Upload a completed BLOCK 105 fillable cue-sheet PDF and download a CSV.

## Current conversion rules
- TIME STAMP -> offset
- SEGMENT -> title
- ARTIST -> artist
- ALBUM -> album
- ALBUM blank -> media_type = talk
- ALBUM filled -> media_type = music
- year is left blank

## Run locally
1. Install Python 3.10+
2. Open a terminal in this folder
3. Run: pip install -r requirements.txt
4. Run: python app.py
5. Open: http://127.0.0.1:5000

## Deploy
This app is ready for a simple Python host such as Railway.
Build command:
pip install -r requirements.txt

Start command:
gunicorn app:app

IMPORTANT:
Before public use, add password protection or access control if you want the converter private.
