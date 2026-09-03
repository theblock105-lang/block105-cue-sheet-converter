import csv
import io
import re
from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = "change-this-before-production"

def clean(value):
    return str(value or "").strip()

def normalize_timestamp(value):
    """Convert common timestamps to HH:MM:SS.000."""
    v = clean(value)
    if not v:
        return ""
    v = v.replace(",", ".")
    # Already HH:MM:SS.xxx
    parts = v.split(":")
    try:
        if len(parts) == 3:
            hh, mm, ss = parts
        elif len(parts) == 2:
            hh, mm = parts
            ss = "0"
        else:
            # allow seconds only
            hh, mm, ss = "0", "0", parts[0]

        hh = int(hh)
        mm = int(mm)
        sec = float(ss)

        total_ms = int(round(((hh * 3600) + (mm * 60) + sec) * 1000))
        hh = total_ms // 3600000
        total_ms %= 3600000
        mm = total_ms // 60000
        total_ms %= 60000
        ss = total_ms // 1000
        ms = total_ms % 1000

        return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"
    except Exception:
        return v

def extract_fields(file_obj):
    reader = PdfReader(file_obj)
    fields = reader.get_fields() or {}
    data = {}
    for name, field in fields.items():
        data[name] = clean(field.get("/V", ""))
    return data

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        uploaded = request.files.get("pdf")
        if not uploaded or not uploaded.filename.lower().endswith(".pdf"):
            flash("Please choose a completed BLOCK 105 cue sheet PDF.")
            return redirect(url_for("index"))

        try:
            fields = extract_fields(uploaded)
        except Exception as e:
            flash(f"Could not read that PDF: {e}")
            return redirect(url_for("index"))

        rows = []
        for i in range(1, 26):
            timestamp = clean(fields.get(f"row_{i}_timestamp"))
            segment = clean(fields.get(f"row_{i}_segment"))
            artist = clean(fields.get(f"row_{i}_artist"))
            album = clean(fields.get(f"row_{i}_album"))

            # Skip completely empty rows
            if not any([timestamp, segment, artist, album]):
                continue

            # Current BLOCK 105 cue-sheet rule:
            # Album blank = talk segment; Album filled in = music.
            media_type = "music" if album else "talk"

            rows.append([
                normalize_timestamp(timestamp),
                media_type,
                segment,
                artist,
                album,
                ""  # year left blank
            ])

        if not rows:
            flash("No completed cue-sheet rows were found in this PDF.")
            return redirect(url_for("index"))

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["offset", "media_type", "title", "artist", "album", "year"])
        writer.writerows(rows)

        csv_bytes = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        csv_bytes.seek(0)

        show_title = clean(fields.get("show_title")) or "BLOCK_105_SHOW"
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", show_title).strip("_")
        filename = f"{safe_name}_LIVE365.csv"

        return send_file(
            csv_bytes,
            mimetype="text/csv",
            as_attachment=True,
            download_name=filename
        )

    return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=True)
