import csv, io, os, re
from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "block105")


def clean(v):
    return str(v or "").strip()


def ts(v):
    v = clean(v).replace(",", ".")
    if not v:
        return ""

    try:
        p = v.split(":")

        if len(p) == 3:
            h, m, s = p
        elif len(p) == 2:
            h, m, s = 0, p[0], p[1]
        else:
            h, m, s = 0, 0, p[0]

        n = int(round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000))

        h = n // 3600000
        n %= 3600000
        m = n // 60000
        n %= 60000
        s = n // 1000
        ms = n % 1000

        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    except Exception:
        return v


def get_pdf_fields(pdf_file):
    reader = PdfReader(pdf_file)
    data = {}

    # Read normal PDF form fields
    fields = reader.get_fields() or {}

    for key, value in fields.items():
        try:
            data[key] = clean(value.get("/V", ""))
        except Exception:
            data[key] = ""

    # Also read fields stored as PDF annotations/widgets
    for page in reader.pages:
        annotations = page.get("/Annots")

        if not annotations:
            continue

        for annotation_ref in annotations:
            try:
                annotation = annotation_ref.get_object()

                name = annotation.get("/T")
                value = annotation.get("/V", "")

                parent = annotation.get("/Parent")

                if parent:
                    parent_obj = parent.get_object()

                    if not name:
                        name = parent_obj.get("/T")

                    if not value:
                        value = parent_obj.get("/V", "")

                if name:
                    data[str(name)] = clean(value)

            except Exception:
                continue

    return data


@app.route("/", methods=["GET", "POST"])
def index():

    if request.method == "POST":

        f = request.files.get("pdf")

        if not f or not f.filename.lower().endswith(".pdf"):
            flash("Please select a completed BLOCK 105 cue sheet PDF.")
            return redirect(url_for("index"))

        try:
            d = get_pdf_fields(f)

        except Exception as e:
            flash(f"Could not read this PDF: {e}")
            return redirect(url_for("index"))

        rows = []

        for i in range(1, 26):

            timestamp = d.get(f"row_{i}_timestamp", "")
            segment = d.get(f"row_{i}_segment", "")
            artist = d.get(f"row_{i}_artist", "")
            album = d.get(f"row_{i}_album", "")

            if any([timestamp, segment, artist, album]):

                media_type = "music" if album else "talk"

                rows.append([
                    ts(timestamp),
                    media_type,
                    segment,
                    artist,
                    album,
                    ""
                ])

        if not rows:
            flash("No completed cue sheet rows were found.")
            return redirect(url_for("index"))

        out = io.StringIO(newline="")
        writer = csv.writer(out)

        writer.writerow([
            "offset",
            "media_type",
            "title",
            "artist",
            "album",
            "year"
        ])

        writer.writerows(rows)

        name = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            d.get("show_title") or "BLOCK_105_SHOW"
        ).strip("_")

        data = io.BytesIO(out.getvalue().encode("utf-8-sig"))
        data.seek(0)

        return send_file(
            data,
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"{name}_LIVE365.csv"
        )

    return render_template("index.html")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )
