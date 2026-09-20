import csv
import io
import os
import re

from flask import Flask, render_template, request, send_file, flash, redirect, url_for, session
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "block105")


# ---------------------------------------------------------
# BASIC CLEANUP
# ---------------------------------------------------------

def clean(value):
    text = str(value or "")

    # Remove non-printing control characters.
    text = re.sub(r"[\x00-\x1F\x7F]", "", text)

    return text.strip()


# ---------------------------------------------------------
# TIMESTAMP PARSER
# ---------------------------------------------------------

def parse_timestamp(value):
    """
    Accepts:
        00:04:01
        4:01
        0031:42  -> 00:31:42
        0040:03  -> 00:40:03
        00;04;01 -> 00:04:01
        decimal seconds

    Returns milliseconds.
    """

    value = clean(value)

    if not value:
        return None

    value = value.replace(",", ".")
    value = value.replace(";", ":")

    try:
        parts = value.split(":")

        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])

        elif len(parts) == 2:
            first = parts[0]
            seconds = float(parts[1])

            # Handles entries such as:
            # 0031:42 = 31 minutes, 42 seconds
            if first.isdigit() and len(first) > 2:
                total_minutes = int(first)
                hours = total_minutes // 60
                minutes = total_minutes % 60
            else:
                hours = 0
                minutes = int(first)

        else:
            hours = 0
            minutes = 0
            seconds = float(parts[0])

        total_ms = int(
            round(
                (
                    hours * 3600
                    + minutes * 60
                    + seconds
                ) * 1000
            )
        )

        return total_ms

    except Exception:
        return None


def format_timestamp(milliseconds):
    if milliseconds is None:
        return ""

    milliseconds = max(0, int(milliseconds))

    hours = milliseconds // 3600000
    milliseconds %= 3600000

    minutes = milliseconds // 60000
    milliseconds %= 60000

    seconds = milliseconds // 1000
    milliseconds %= 1000

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


# ---------------------------------------------------------
# METADATA NORMALIZATION
# ---------------------------------------------------------

ARTIST_NORMALIZATIONS = {
    "too short": "Too $hort",
    "too $hort": "Too $hort",
}

ALBUM_NORMALIZATIONS = {}


def normalize_artist(value):
    value = clean(value)

    key = value.lower()

    return ARTIST_NORMALIZATIONS.get(key, value)


def normalize_album(value):
    value = clean(value)

    key = value.lower()

    if key in ALBUM_NORMALIZATIONS:
        return ALBUM_NORMALIZATIONS[key]

    # Normalize common single formatting.
    if re.search(r"\s*-\s*single$", value, re.IGNORECASE):
        base = re.sub(
            r"\s*-\s*single$",
            "",
            value,
            flags=re.IGNORECASE
        ).strip()

        return f"{base} (Single)"

    if re.search(r"\s*\(\s*single\s*\)$", value, re.IGNORECASE):
        base = re.sub(
            r"\s*\(\s*single\s*\)$",
            "",
            value,
            flags=re.IGNORECASE
        ).strip()

        return f"{base} (Single)"

    if re.search(r"\s+single$", value, re.IGNORECASE):
        base = re.sub(
            r"\s+single$",
            "",
            value,
            flags=re.IGNORECASE
        ).strip()

        return f"{base} (Single)"

    return value


# ---------------------------------------------------------
# PDF FIELD READER
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# EXTRACT CUE SHEET ROWS
# ---------------------------------------------------------

def extract_rows(fields):
    rows = []

    for i in range(1, 26):

        timestamp_raw = clean(
            fields.get(f"row_{i}_timestamp", "")
        )

        title = clean(
            fields.get(f"row_{i}_segment", "")
        )

        artist = normalize_artist(
            fields.get(f"row_{i}_artist", "")
        )

        album = normalize_album(
            fields.get(f"row_{i}_album", "")
        )

        if not any([
            timestamp_raw,
            title,
            artist,
            album
        ]):
            continue

        timestamp_ms = parse_timestamp(timestamp_raw)

        if timestamp_ms is None:
            timestamp_ms = 0

        # Identify non-music segments.
        is_talk = title.lower() in [
            "talk segment",
            "talk",
            "commercial",
            "announcement",
            "station id",
            "station identification",
            "promo",
            "promotional",
        ]

        if is_talk:
            media_type = "talk"
            album = ""

            if not title:
                title = "Talk Segment"

            if not artist:
                artist = "Host"

        else:
            media_type = "music"

        rows.append({
            "timestamp_ms": timestamp_ms,
            "offset": format_timestamp(timestamp_ms),
            "media_type": media_type,
            "title": title,
            "artist": artist,
            "album": album,
            "year": "",
            "_protected": False,
            "_original_row": i,
        })

    rows.sort(key=lambda row: row["timestamp_ms"])

    return rows


# ---------------------------------------------------------
# SHOW TITLE MARKER
# ---------------------------------------------------------

def add_show_title_marker(rows, show_name, corrections):
    """
    Every BLOCK 105 cue sheet starts with:

    00:00:00.000
    talk
    SHOW TITLE
    SHOW TITLE

    If the first actual item is a song at 00:00:00,
    preserve the song and move it to 00:00:00.001.
    """

    show_name = clean(show_name) or "BLOCK 105 SHOW"

    # Find anything currently starting at zero.
    zero_rows = [
        row for row in rows
        if row["timestamp_ms"] == 0
    ]

    if zero_rows:

        first_zero = zero_rows[0]

        # If a music track starts at zero, preserve it.
        if first_zero["media_type"] == "music":

            first_zero["timestamp_ms"] = 1
            first_zero["offset"] = "00:00:00.001"

            corrections.append({
                "type": "Correction",
                "message": (
                    f"First music marker moved from "
                    f"00:00:00.000 to 00:00:00.001 "
                    f"to preserve the show-title marker."
                )
            })

            show_marker = {
                "timestamp_ms": 0,
                "offset": "00:00:00.000",
                "media_type": "talk",
                "title": show_name,
                "artist": show_name,
                "album": "",
                "year": "",
                "_protected": True,
                "_original_row": 0,
            }

            rows.insert(0, show_marker)

            corrections.append({
                "type": "Correction",
                "message": (
                    "Added protected show-title marker "
                    "at 00:00:00.000."
                )
            })

        else:
            # Existing non-music marker can become the show marker.
            first_zero["media_type"] = "talk"
            first_zero["title"] = show_name
            first_zero["artist"] = show_name
            first_zero["album"] = ""
            first_zero["year"] = ""
            first_zero["_protected"] = True

            corrections.append({
                "type": "Correction",
                "message": (
                    "Converted existing 00:00:00 marker "
                    "into the protected show-title marker."
                )
            })

    else:

        show_marker = {
            "timestamp_ms": 0,
            "offset": "00:00:00.000",
            "media_type": "talk",
            "title": show_name,
            "artist": show_name,
            "album": "",
            "year": "",
            "_protected": True,
            "_original_row": 0,
        }

        rows.insert(0, show_marker)

        corrections.append({
            "type": "Correction",
            "message": (
                "Added protected show-title marker "
                "at 00:00:00.000."
            )
        })

    rows.sort(key=lambda row: row["timestamp_ms"])

    return rows


# ---------------------------------------------------------
# DUPLICATE TIMESTAMPS
# ---------------------------------------------------------

def fix_duplicate_timestamps(rows, corrections):
    used = set()

    for row in rows:

        timestamp = row["timestamp_ms"]

        while timestamp in used:
            timestamp += 1

        if timestamp != row["timestamp_ms"]:

            old_offset = row["offset"]

            row["timestamp_ms"] = timestamp
            row["offset"] = format_timestamp(timestamp)

            corrections.append({
                "type": "Correction",
                "message": (
                    f"Duplicate timestamp {old_offset} "
                    f"moved to {row['offset']}."
                )
            })

        used.add(timestamp)

    rows.sort(key=lambda row: row["timestamp_ms"])

    return rows


# ---------------------------------------------------------
# LIVE365 MARKER DENSITY
# ---------------------------------------------------------

def marker_violations(rows):
    """
    Live365 marker limits:

    5 markers / 10 minutes
    7 markers / 15 minutes
    13 markers / 30 minutes
    33 markers / 78 minutes
    """

    limits = [
        (10 * 60 * 1000, 5),
        (15 * 60 * 1000, 7),
        (30 * 60 * 1000, 13),
        (78 * 60 * 1000, 33),
    ]

    timestamps = [
        row["timestamp_ms"]
        for row in rows
    ]

    violations = []

    for window_ms, limit in limits:

        for start in timestamps:

            count = sum(
                1
                for timestamp in timestamps
                if start <= timestamp < start + window_ms
            )

            if count > limit:

                violations.append({
                    "window_ms": window_ms,
                    "limit": limit,
                    "count": count,
                    "start": start,
                    "end": start + window_ms,
                })

                break

    return violations


# ---------------------------------------------------------
# REMOVE SAFE NON-MUSIC MARKERS
# ---------------------------------------------------------

def remove_safe_nonmusic_markers(rows, corrections):

    while True:

        violations = marker_violations(rows)

        if not violations:
            break

        removable = []

        for index, row in enumerate(rows):

            if row["media_type"] != "talk":
                continue

            if row.get("_protected"):
                continue

            # Never remove the protected show marker.
            if row["timestamp_ms"] == 0:
                continue

            removable.append(index)

        if not removable:
            break

        # Remove the latest safe non-music marker first.
        index_to_remove = removable[-1]

        removed = rows.pop(index_to_remove)

        corrections.append({
            "type": "Correction",
            "message": (
                f"Removed safe non-music marker "
                f"at {removed['offset']} "
                f"to satisfy Live365 marker-density limits."
            )
        })

    return rows


# ---------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------

def validate_rows(rows):

    errors = []
    warnings = []

    if not rows:
        errors.append("No cue-sheet rows were found.")

        return errors, warnings

    # First marker must be show marker.
    first = rows[0]

    if first["timestamp_ms"] != 0:
        errors.append(
            "First marker is not at 00:00:00.000."
        )

    if first["media_type"] != "talk":
        errors.append(
            "First marker must be a talk/show-title marker."
        )

    # Duplicate timestamp check.
    seen = set()

    for row in rows:

        timestamp = row["timestamp_ms"]

        if timestamp in seen:
            errors.append(
                f"Duplicate timestamp detected: "
                f"{row['offset']}."
            )

        seen.add(timestamp)

        # Required fields.
        if not row["offset"]:
            errors.append("A row is missing its offset.")

        if row["media_type"] not in ["music", "talk"]:
            errors.append(
                f"Invalid media_type: {row['media_type']}."
            )

        if not row["title"]:
            warnings.append(
                f"Missing title at {row['offset']}."
            )

        if not row["artist"]:
            warnings.append(
                f"Missing artist at {row['offset']}."
            )

        # Music metadata warning.
        if row["media_type"] == "music":

            if not row["album"]:
                warnings.append(
                    f"Music marker at {row['offset']} "
                    f"has no album metadata."
                )

    density_violations = marker_violations(rows)

    for violation in density_violations:

        window_minutes = violation["window_ms"] / 60000

        warnings.append(
            f"Live365 marker-density limit remains exceeded: "
            f"{violation['count']} markers within "
            f"{window_minutes:g} minutes "
            f"(limit {violation['limit']})."
        )

    return errors, warnings


# ---------------------------------------------------------
# CSV CREATION
# ---------------------------------------------------------

def make_csv(rows):

    output = io.StringIO(newline="")

    writer = csv.writer(
        output,
        lineterminator="\n"
    )

    writer.writerow([
        "offset",
        "media_type",
        "title",
        "artist",
        "album",
        "year"
    ])

    for row in rows:

        writer.writerow([
            row["offset"],
            row["media_type"],
            row["title"],
            row["artist"],
            row["album"],
            row["year"],
        ])

    return output.getvalue()


# ---------------------------------------------------------
# REPORT
# ---------------------------------------------------------

def build_report(
    original_rows,
    corrected_rows,
    corrections,
    warnings,
    errors
):

    return {
        "original_count": len(original_rows),
        "corrected_count": len(corrected_rows),
        "corrections": corrections,
        "warnings": warnings,
        "errors": errors,
    }


# ---------------------------------------------------------
# MAIN ROUTE
# ---------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():

    if request.method == "POST":

        action = request.form.get("action", "analyze")

        # -------------------------------------------------
        # DOWNLOAD CORRECTED CSV
        # -------------------------------------------------

        if action == "download":

            csv_data = session.get("csv_data", "")
            filename = session.get(
                "csv_filename",
                "BLOCK_105_LIVE365.csv"
            )

            if not csv_data:
                flash(
                    "Your corrected CSV is no longer available. "
                    "Please upload the cue sheet again."
                )

                return redirect(url_for("index"))

            file_data = io.BytesIO(
                csv_data.encode("utf-8")
            )

            file_data.seek(0)

            return send_file(
                file_data,
                mimetype="text/csv",
                as_attachment=True,
                download_name=filename
            )

        # -------------------------------------------------
        # ANALYZE PDF
        # -------------------------------------------------

        uploaded_file = request.files.get("pdf")

        if (
            not uploaded_file
            or not uploaded_file.filename.lower().endswith(".pdf")
        ):
            flash(
                "Please select a completed BLOCK 105 cue sheet PDF."
            )

            return redirect(url_for("index"))

        try:

            fields = get_pdf_fields(uploaded_file)

        except Exception as error:

            flash(
                f"Could not read this PDF: {error}"
            )

            return redirect(url_for("index"))

        original_rows = extract_rows(fields)

        if not original_rows:

            flash(
                "No completed cue-sheet rows were found."
            )

            return redirect(url_for("index"))

        corrections = []

        show_name = clean(
            fields.get("show_title", "")
        )

        # -------------------------------------------------
        # NORMALIZATION REPORT
        # -------------------------------------------------

        for row in original_rows:

            original_artist = row["artist"]
            normalized_artist = normalize_artist(
                original_artist
            )

            if normalized_artist != original_artist:

                corrections.append({
                    "type": "Correction",
                    "message": (
                        f"Artist normalized: "
                        f"{original_artist} → "
                        f"{normalized_artist}"
                    )
                })

                row["artist"] = normalized_artist

            original_album = row["album"]
            normalized_album = normalize_album(
                original_album
            )

            if normalized_album != original_album:

                corrections.append({
                    "type": "Correction",
                    "message": (
                        f"Album normalized: "
                        f"{original_album} → "
                        f"{normalized_album}"
                    )
                })

                row["album"] = normalized_album

        # -------------------------------------------------
        # SHOW TITLE MARKER
        # -------------------------------------------------

        add_show_title_marker(
            original_rows,
            show_name,
            corrections
        )

        # -------------------------------------------------
        # DUPLICATE TIMESTAMPS
        # -------------------------------------------------

        fix_duplicate_timestamps(
            original_rows,
            corrections
        )

        # -------------------------------------------------
        # MARKER OPTIMIZATION
        # -------------------------------------------------

        corrected_rows = remove_safe_nonmusic_markers(
            original_rows,
            corrections
        )

        # -------------------------------------------------
        # FINAL VALIDATION
        # -------------------------------------------------

        errors, warnings = validate_rows(
            corrected_rows
        )

        # -------------------------------------------------
        # CREATE CSV
        # -------------------------------------------------

        csv_data = make_csv(
            corrected_rows
        )

        filename_base = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            show_name or "BLOCK_105_SHOW"
        ).strip("_")

        filename = (
            f"{filename_base}_LIVE365_CORRECTED.csv"
        )

        # Store only after analysis.
        session["csv_data"] = csv_data
        session["csv_filename"] = filename

        report = build_report(
            original_rows,
            corrected_rows,
            corrections,
            warnings,
            errors
        )

        return render_template(
           "converter.html"
            report=report
        )

    return render_template(
       "converter.html"
        report=None
    )


# ---------------------------------------------------------
# START SERVER
# ---------------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get("PORT", 8080)
        )
    )
