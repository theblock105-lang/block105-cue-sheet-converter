import base64
import csv
import io
import json
import os
import re
from collections import defaultdict

from flask import Flask, render_template, request, send_file, flash, redirect, url_for, session
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "block105")

# Live365's published MultiTrack marker limits.
MARKER_LIMITS = [
    (10 * 60, 5),
    (15 * 60, 7),
    (30 * 60, 13),
    (78 * 60, 33),
]

TALK_TYPES = {
    "talk", "talk segment", "commercial", "announcement",
    "promo", "id", "ad"
}

# Safe metadata normalizations. This dictionary can be expanded over time.
ARTIST_NORMALIZATIONS = {
    "too short": "Too $hort",
    "too $hort": "Too $hort",
}

ALBUM_NORMALIZATIONS = {}

def clean(value):
    value = str(value or "")
    # Remove non-printing control characters that can appear in PDF form-field
    # extraction (while preserving normal printable metadata).
    value = re.sub(r"[\x00-\x1F\x7F]", "", value)
    return value.strip()


def parse_timestamp(value):
    """Return timestamp in milliseconds, accepting HH:MM:SS, MM:SS and HHMM:SS."""
    value = clean(value).replace(",", ".").replace(";", ":")

    if not value:
        return None

    try:
        parts = value.split(":")

        # Handle compact hour/minute forms commonly entered on cue sheets,
        # e.g. 0031:42 = 00:31:42 and 0040:03 = 00:40:03.
        if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4:
            compact = parts[0]
            hours = int(compact[:2])
            minutes = int(compact[2:])
            seconds_float = float(parts[1])
        elif len(parts) == 3:
            hours, minutes = int(parts[0]), int(parts[1])
            seconds_float = float(parts[2])
        elif len(parts) == 2:
            hours, minutes = 0, int(parts[0])
            seconds_float = float(parts[1])
        elif len(parts) == 1:
            hours, minutes = 0, 0
            seconds_float = float(parts[0])
        else:
            return None

        if hours < 0 or minutes < 0 or minutes >= 60 or seconds_float < 0 or seconds_float >= 60:
            return None

        total_ms = int(round((hours * 3600 + minutes * 60 + seconds_float) * 1000))
        return total_ms

    except (ValueError, TypeError):
        return None


def timestamp_from_ms(total_ms):
    total_ms = max(0, int(total_ms))

    hours = total_ms // 3600000
    total_ms %= 3600000
    minutes = total_ms // 60000
    total_ms %= 60000
    seconds = total_ms // 1000
    milliseconds = total_ms % 1000

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def format_timestamp(value):
    parsed = parse_timestamp(value)
    if parsed is None:
        return clean(value)
    return timestamp_from_ms(parsed)


def normalize_text(value):
    value = clean(value)
    value = re.sub(r"\s+", " ", value)
    return value


def normalize_single(value):
    """Normalize common '(Single)' variations without changing the title otherwise."""
    value = normalize_text(value)
    value = re.sub(r"\s*[-–—]?\s*\(\s*single\s*\)\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[-–—]?\s*single\s*$", "", value, flags=re.I)

    if value:
        return f"{value} (Single)"
    return value


def normalize_artist(value):
    value = normalize_text(value)
    if not value:
        return ""

    key = value.lower()
    if key in ARTIST_NORMALIZATIONS:
        return ARTIST_NORMALIZATIONS[key]

    return value


def normalize_album(value):
    value = normalize_text(value)
    if not value:
        return ""

    key = value.lower()
    if key in ALBUM_NORMALIZATIONS:
        return ALBUM_NORMALIZATIONS[key]

    # Standardize a trailing Single label.
    if re.search(r"(?:^|\s|\-)\(?single\)?$", value, flags=re.I):
        return normalize_single(value)

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


def extract_rows(fields):
    rows = []

    for i in range(1, 26):
        original_timestamp = clean(fields.get(f"row_{i}_timestamp", ""))
        title = clean(fields.get(f"row_{i}_segment", ""))
        artist = clean(fields.get(f"row_{i}_artist", ""))
        album = clean(fields.get(f"row_{i}_album", ""))

        if not any([original_timestamp, title, artist, album]):
            continue

        title_lower = title.lower()

        if title_lower in TALK_TYPES:
            media_type = "talk"
            album = ""
            if not title:
                title = "Talk Segment"
            if not artist:
                artist = "Host"
        else:
            media_type = "music"

        rows.append({
            "row": i,
            "original_timestamp": original_timestamp,
            "timestamp_ms": parse_timestamp(original_timestamp),
            "offset": format_timestamp(original_timestamp),
            "media_type": media_type,
            "title": title,
            "artist": artist,
            "album": album,
            "year": "",
        })

    return rows


def correction(rows, row_number, field, original, new, reason, corrections):
    if original != new:
        corrections.append({
            "row": row_number,
            "field": field,
            "original": original,
            "new": new,
            "reason": reason,
        })


def add_show_title_marker(rows, show_name, corrections):
    """Always create a protected 00:00 show-title marker while preserving a song at zero."""
    show_name = clean(show_name) or "BLOCK 105 SHOW"

    zero_rows = [
        r for r in rows
        if r.get("timestamp_ms") is not None and r["timestamp_ms"] == 0
    ]

    # If a source marker already exists at 00:00, keep it as the first song/segment
    # and move it by 1ms so the protected show marker can occupy the required zero
    # position without creating duplicate timestamps.
    if zero_rows:
        for marker in zero_rows:
            old_offset = marker["offset"]
            marker["timestamp_ms"] = 1
            marker["offset"] = "00:00:00.001"
            corrections.append({
                "row": marker["row"],
                "field": "offset",
                "original": old_offset,
                "new": marker["offset"],
                "reason": "Reserved 00:00:00.000 for the protected show-title marker while preserving the opening content marker.",
            })

    marker = {
        "row": 0,
        "original_timestamp": "",
        "timestamp_ms": 0,
        "offset": "00:00:00.000",
        "media_type": "talk",
        "title": show_name,
        "artist": show_name,
        "album": "",
        "year": "",
        "_protected": True,
    }
    rows.insert(0, marker)

    corrections.append({
        "row": 0,
        "field": "show marker",
        "original": "NOT PRESENT",
        "new": f"00:00:00.000 / talk / {show_name} / {show_name}",
        "reason": "Automatically inserted the protected opening show-title marker.",
    })

    return rows


def optimize_rows(rows, show_name):
    corrections = []
    warnings = []

    # Every cue sheet begins with a protected show-title marker.
    rows = add_show_title_marker(rows, show_name, corrections)

    # Normalize timestamps and metadata.
    for item in rows:
        original = item["original_timestamp"]
        parsed = item["timestamp_ms"]

        # Explicitly repair the semicolon issue and other safe formatting issues.
        if parsed is not None:
            new_offset = timestamp_from_ms(parsed)
            correction(
                rows,
                item["row"],
                "offset",
                original,
                new_offset,
                "Normalized timestamp to Live365 XX:XX:XX.XXX format.",
                corrections,
            )
            item["offset"] = new_offset

        else:
            warnings.append(
                f"Row {item['row']}: invalid timestamp '{original}'."
            )

        old_artist = item["artist"]
        new_artist = normalize_artist(old_artist)
        correction(
            rows,
            item["row"],
            "artist",
            old_artist,
            new_artist,
            "Standardized artist metadata.",
            corrections,
        )
        item["artist"] = new_artist

        old_album = item["album"]
        new_album = normalize_album(old_album)
        correction(
            rows,
            item["row"],
            "album",
            old_album,
            new_album,
            "Standardized album metadata.",
            corrections,
        )
        item["album"] = new_album

        item["media_type"] = clean(item["media_type"]).lower()

    # Safely resolve duplicate timestamps by moving the later marker forward 1ms
    # only when the timestamp is duplicated. This prevents Live365 rejection while
    # preserving row order. A review warning is also recorded because the source
    # PDF did not provide enough information to know the exact intended timing.
    used = set()

    for item in rows:
        if item["timestamp_ms"] is None:
            continue

        original_ms = item["timestamp_ms"]
        new_ms = original_ms

        while new_ms in used:
            new_ms += 1

        if new_ms != original_ms:
            old_offset = item["offset"]
            item["timestamp_ms"] = new_ms
            item["offset"] = timestamp_from_ms(new_ms)

            correction(
                rows,
                item["row"],
                "offset",
                old_offset,
                item["offset"],
                "Resolved duplicate timestamp by moving the later marker forward 1ms.",
                corrections,
            )

            warnings.append(
                f"Row {item['row']}: duplicate timestamp adjusted by 1ms."
            )

        used.add(new_ms)

    # The opening show-title marker is always protected at 00:00:00.000.
    # Sort for validation/playback sequence.
    rows.sort(key=lambda r: (r["timestamp_ms"] is None, r["timestamp_ms"] or 0))

    return rows, corrections, warnings


def marker_violations(rows):
    """Check every scalable Live365 marker window."""
    violations = []

    valid = [r for r in rows if r["timestamp_ms"] is not None]

    for window_seconds, max_markers in MARKER_LIMITS:
        # For a 78-minute file, Live365 describes the limits as scalable:
        # no more than N markers can occur within any corresponding window.
        for i, start in enumerate(valid):
            start_ms = start["timestamp_ms"]
            end_ms = start_ms + window_seconds * 1000
            count = 0
            indices = []

            for j in range(i, len(valid)):
                if valid[j]["timestamp_ms"] <= end_ms:
                    count += 1
                    indices.append(j)
                else:
                    break

            if count > max_markers:
                violations.append({
                    "window": window_seconds,
                    "limit": max_markers,
                    "count": count,
                    "rows": indices,
                })

    return violations


def remove_safe_nonmusic_markers(rows, corrections, warnings):
    """
    Automatically reduce marker density by removing non-music markers only.
    Music markers are protected because removing one can cause the preceding
    song's metadata to remain displayed over the next song.
    """
    removed = True
    removed_items = []

    while removed:
        removed = False
        violations = marker_violations(rows)

        if not violations:
            break

        # Find candidates participating in a violation.
        candidate_indices = set()
        for violation in violations:
            candidate_indices.update(violation["rows"])

        candidates = [
            rows[i] for i in candidate_indices
            if rows[i]["media_type"] != "music"
            and not rows[i].get("_protected", False)
        ]

        if not candidates:
            break

        # Remove the latest safe non-music marker first. This preserves earlier
        # metadata and minimizes disruption to the playback sequence.
        candidate = max(
            candidates,
            key=lambda r: r["timestamp_ms"] if r["timestamp_ms"] is not None else -1
        )

        rows.remove(candidate)
        removed_items.append(candidate)

        corrections.append({
            "row": candidate["row"],
            "field": "marker",
            "original": f"{candidate['offset']} / {candidate['title']}",
            "new": "REMOVED",
            "reason": "Removed non-music marker to satisfy Live365 marker-density limits without deleting a song marker.",
        })

        removed = True

    if marker_violations(rows):
        warnings.append(
            "Marker density still exceeds Live365 limits after all safe non-music markers were removed. No music marker was deleted because doing so could cause incorrect song metadata to remain displayed."
        )

    return rows, removed_items


def validate_rows(rows):
    errors = []
    warnings = []

    seen = set()

    for item in rows:
        if item["timestamp_ms"] is None:
            errors.append(f"Row {item['row']} has an invalid timestamp.")
            continue

        if item["timestamp_ms"] in seen:
            errors.append(
                f"Row {item['row']} has a duplicate timestamp: {item['offset']}."
            )
        seen.add(item["timestamp_ms"])

        if not item["offset"]:
            errors.append(f"Row {item['row']} is missing offset.")

        if not item["media_type"]:
            errors.append(f"Row {item['row']} is missing media_type.")

        if not item["title"]:
            errors.append(f"Row {item['row']} is missing title.")

        if not item["artist"]:
            errors.append(f"Row {item['row']} is missing artist.")

        if item["media_type"] == "music" and not item["album"]:
            warnings.append(
                f"Row {item['row']}: music marker has no album metadata."
            )

    violations = marker_violations(rows)

    for violation in violations:
        minutes = violation["window"] // 60
        warnings.append(
            f"{violation['count']} markers occur within a {minutes}-minute window; Live365 allows {violation['limit']}."
        )

    return errors, warnings


def make_csv(rows):
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

    for item in rows:
        writer.writerow([
            item["offset"],
            item["media_type"],
            item["title"],
            item["artist"],
            item["album"],
            item["year"],
        ])

    return output.getvalue()


def build_report(rows_before, rows_after, corrections, warnings, errors):
    checks = []

    valid_times = [
        r["timestamp_ms"] for r in rows_after if r["timestamp_ms"] is not None
    ]

    checks.append({
        "name": "Timestamp format",
        "status": "PASS" if not errors or all("timestamp" not in e.lower() for e in errors) else "FAIL"
    })

    duplicate_times = len(valid_times) != len(set(valid_times))
    checks.append({
        "name": "Duplicate timestamps",
        "status": "FAIL" if duplicate_times else "PASS"
    })

    density_fail = bool(marker_violations(rows_after))
    checks.append({
        "name": "Live365 marker density",
        "status": "FAIL" if density_fail else "PASS"
    })

    checks.append({
        "name": "Required metadata",
        "status": "FAIL" if errors else "PASS"
    })

    first_marker = min(valid_times) if valid_times else None
    checks.append({
        "name": "First marker at 00:00:00",
        "status": "PASS" if first_marker == 0 else "WARNING"
    })

    return {
        "original_count": len(rows_before),
        "final_count": len(rows_after),
        "corrections": corrections,
        "warnings": warnings,
        "errors": errors,
        "checks": checks,
        "ready": not errors and not density_fail,
    }


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        action = request.form.get("action", "analyze")

        if action == "download":
            csv_data = session.get("csv_data")
            filename = session.get("csv_filename", "BLOCK_105_LIVE365.csv")

            if not csv_data:
                flash("Your analysis session has expired. Please upload the PDF again.")
                return redirect(url_for("index"))

            file_data = io.BytesIO(csv_data.encode("utf-8"))
            file_data.seek(0)

            return send_file(
                file_data,
                mimetype="text/csv",
                as_attachment=True,
                download_name=filename,
            )

        uploaded_file = request.files.get("pdf")

        if not uploaded_file or not uploaded_file.filename.lower().endswith(".pdf"):
            flash("Please select a completed BLOCK 105 cue sheet PDF.")
            return redirect(url_for("index"))

        try:
            fields = get_pdf_fields(uploaded_file)
        except Exception as error:
            flash(f"Could not read this PDF: {error}")
            return redirect(url_for("index"))

        rows = extract_rows(fields)

        if not rows:
            flash("No completed cue-sheet rows were found.")
            return redirect(url_for("index"))

        original_rows = [dict(r) for r in rows]

        rows, corrections, warnings = optimize_rows(rows, clean(fields.get("show_title", "")))

        rows, removed_items = remove_safe_nonmusic_markers(
            rows, corrections, warnings
        )

        errors, validation_warnings = validate_rows(rows)
        warnings.extend(validation_warnings)

        # Remove duplicate warning text while preserving order.
        warnings = list(dict.fromkeys(warnings))

        report = build_report(
            original_rows,
            rows,
            corrections,
            warnings,
            errors,
        )

        show_name = clean(fields.get("show_title", ""))

        filename = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            show_name or "BLOCK_105_SHOW"
        ).strip("_")

        csv_data = make_csv(rows)

        # Store only the final validated CSV for the explicit download action.
        session["csv_data"] = csv_data
        session["csv_filename"] = f"{filename}_LIVE365.csv"

        return render_template(
            "index.html",
            report=report,
            show_name=show_name or "BLOCK 105 SHOW",
        )

    return render_template("index.html", report=None, show_name="")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )
