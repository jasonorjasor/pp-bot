"""
Archive older posted/grading records into monthly JSONL files.
Keeps unresolved and recent records in the active working files.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_ACTIVE_DIR = BASE_DIR / "data" / "active"
POSTED_PROPS_FILE = str(DATA_ACTIVE_DIR / "postedProps.jsonl")
GRADED_PROPS_FILE = str(DATA_ACTIVE_DIR / "gradedProps.jsonl")
ARCHIVE_ROOT = os.getenv("ARCHIVE_ROOT", "archive")
ARCHIVE_RETENTION_DAYS = int(os.getenv("ARCHIVE_RETENTION_DAYS", "14"))


def load_jsonl(path):
    if not os.path.exists(path):
        return []

    records = []
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def append_jsonl(path, records):
    if not records:
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def parse_datetime(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_alert_id(alert):
    if alert.get("alertId"):
        return alert["alertId"]
    return "|".join(
        [
            str(alert.get("propId", "")),
            str(alert.get("postedAt", "")),
            str(alert.get("line", "")),
            str(alert.get("recommendedSide", "")),
        ]
    )


def month_key(dt):
    return f"{dt.year:04d}-{dt.month:02d}"


def archive_path(kind, dt):
    return os.path.join(ARCHIVE_ROOT, kind, f"{month_key(dt)}.jsonl")


def latest_grade_by_alert(graded_records):
    latest = {}
    for record in graded_records:
        latest[record["alertId"]] = record
    return latest


def archive_records():
    posted_records = load_jsonl(POSTED_PROPS_FILE)
    graded_records = load_jsonl(GRADED_PROPS_FILE)
    latest_grades = latest_grade_by_alert(graded_records)
    cutoff = datetime.now(UTC) - timedelta(days=ARCHIVE_RETENTION_DAYS)

    posted_keep = []
    posted_archive = defaultdict(list)
    for alert in posted_records:
        posted_at = parse_datetime(alert.get("postedAt"))
        latest = latest_grades.get(build_alert_id(alert))
        should_keep = (
            posted_at is None
            or posted_at >= cutoff
            or latest is None
            or latest["result"] == "unresolved"
        )

        if should_keep:
            posted_keep.append(alert)
            continue

        posted_archive[archive_path("posted", posted_at)].append(alert)

    graded_keep = []
    graded_archive = defaultdict(list)
    for record in graded_records:
        graded_at = parse_datetime(record.get("gradedAt"))
        latest = latest_grades.get(record["alertId"])
        should_keep = (
            graded_at is None
            or graded_at >= cutoff
            or latest is None
            or latest["result"] == "unresolved"
        )

        if should_keep:
            graded_keep.append(record)
            continue

        graded_archive[archive_path("graded", graded_at)].append(record)

    for path, records in posted_archive.items():
        append_jsonl(path, records)

    for path, records in graded_archive.items():
        append_jsonl(path, records)

    write_jsonl(POSTED_PROPS_FILE, posted_keep)
    write_jsonl(GRADED_PROPS_FILE, graded_keep)

    summary = {
        "success": True,
        "retentionDays": ARCHIVE_RETENTION_DAYS,
        "archiveRoot": ARCHIVE_ROOT,
        "archivedPosted": sum(len(records) for records in posted_archive.values()),
        "archivedGraded": sum(len(records) for records in graded_archive.values()),
        "retainedPosted": len(posted_keep),
        "retainedGraded": len(graded_keep),
        "postedArchiveFiles": sorted(posted_archive.keys()),
        "gradedArchiveFiles": sorted(graded_archive.keys()),
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    try:
        archive_records()
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
