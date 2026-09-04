import csv
import io
import os
import re

from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "block105")


def clean(value):
    return str(value or "").strip()


def format_timestamp(value):
    value = clean(value).replace(",", ".")

    if not value:
        return ""

    try:
        parts = value.split(":")

        if len(parts) == 3:
            hours, minutes, seconds = parts
        elif len(parts) == 2:
            hours, minutes, seconds = "0", parts[0], parts[1]
        else:
            hours, minutes, seconds = "0", "0", parts[0]

        total_ms = int(
            round(
                (
                    int(hours) * 3600
                    + int(minutes) * 60
                    + float(seconds)
                ) * 1000
            )
        )

        hours = total_ms // 3600000
        total_ms %= 3600000

        minutes = total_ms // 60000
        total_ms %= 60000

        seconds = total_ms // 1000
        milliseconds = total_ms % 1000

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    except Exception:
        return value


def get_pdf_fields(pdf_file):
    reader = PdfReader(pdf_file)
    data = {}

    fields = reader.get_fields() or {}

    for key, field in fields.items():
        try:
            data[str(key)] = clean(field.get("/V", ""))
        except Exception:
            pass

    for page in reader.pages:
        annotations = page.get("/Annots")

        if not annotations:
            continue

        for annotation_ref in annotations:
            try:
                annotation = annotation_ref.get_object()

                name = annotation.get("/T")
                value = annotation.get("/V", "")

                if name:
                    data[str(name)] = clean(value)

            except Exception:
                continue

    return data


@app.route("/", methods=["GET", "POST"])
def index():

    if request.method == "POST":

        uploaded_file = request.files.get("pdf")

        if not uploaded_file or not uploaded_file.filename.lower().endswith(".pdf"):
            flash("Please select a completed BLOCK 105 cue sheet PDF.")
            return redirect(url_for("index"))

        try:
            fields = get_pdf_fields(uploaded_file)

        except Exception as error:
            flash(f"Could not read this PDF: {error}")
            return redirect(url_for("index"))

        rows = []

        for i in range(1, 26):

            timestamp = clean(fields.get(f"row_{i}_timestamp", ""))
            title = clean(fields.get(f"row_{i}_segment", ""))
            artist = clean(fields.get(f"row_{i}_artist", ""))
            album = clean(fields.get(f"row_{i}_album", ""))

            if not any([timestamp, title, artist, album]):
                continue

            # Identify TALK segments by their title.
            if title.lower() in ["talk segment", "talk", "commercial", "announcement"]:
                media_type = "talk"

                # Talk segments should not have music album metadata.
                album = ""

                # Make sure required metadata exists.
                if not title:
                    title = "Talk Segment"

                if not artist:
                    artist = "Host"

            else:
                media_type = "music"

            rows.append([
                format_timestamp(timestamp),
                media_type,
                title,
                artist,
                album,
                ""
            ])

        if not rows:
            flash("No completed cue-sheet rows were found.")
            return redirect(url_for("index"))

        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")

        writer.writerow([
            "offset",
            "media_type",
            "title",
            "artist",
            "album",
            "year"
        ])

        writer.writerows(rows)

        show_name = clean(fields.get("show_title", ""))

        filename = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            show_name or "BLOCK_105_SHOW"
        ).strip("_")

        file_data = io.BytesIO(
            output.getvalue().encode("utf-8")
        )

        file_data.seek(0)

        return send_file(
            file_data,
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"{filename}_LIVE365.csv"
        )

    return render_template("index.html")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )
