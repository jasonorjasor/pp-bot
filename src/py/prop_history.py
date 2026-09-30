"""Shared history validation, chronological grade selection, and observation keys."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LEAGUE_TIMEZONE = "America/New_York"
RESULTS = {"win", "loss", "push", "void", "unresolved"}
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def canonical_json(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def event_hash(record):
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


def aware_datetime(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a nonempty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp requires an explicit timezone: {value!r}")
    return parsed.astimezone(UTC)


def timestamp_us(value):
    delta = aware_datetime(value) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def normalize_identifier(value, field="identifier"):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be a nonempty string or integer")
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} must be nonempty")
    return result


def alert_id(record):
    if record.get("alertId") is not None:
        return normalize_identifier(record["alertId"], "alertId")
    # These alerts predate persisted alertId; preserve the original grader's formula.
    fields = ("propId", "postedAt", "line", "recommendedSide")
    if any(record.get(key) is None for key in fields):
        raise ValueError("record is missing alertId and the fields needed for a legacy ID")
    normalize_identifier(record["propId"], "propId")
    aware_datetime(record["postedAt"])
    finite_number(record["line"], "line", required=True)
    if record["recommendedSide"] not in ("over", "under"):
        raise ValueError("recommendedSide must be over or under")
    return "|".join(str(record[key]) for key in fields)


def finite_number(value, field, *, required=False):
    if value is None or value == "":
        if required:
            raise ValueError(f"{field} is required")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric, not boolean")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def jsonl_snapshot(path):
    """Read one exact byte snapshot, rejecting missing/non-object/non-finite input."""
    path = Path(path)
    content = path.read_bytes()
    metadata = {"path": str(path.resolve()), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}

    def reject_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    def records():
        for line_number, line in enumerate(content.decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line, parse_constant=reject_constant)
                if not isinstance(record, dict):
                    raise ValueError("expected a JSON object")
                canonical_json(record)  # Also rejects finite-looking exponents overflowing float.
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON record: {exc}") from exc
            yield line_number, record
    return records(), metadata


def source_paths(active_path, archive_root=None, *, kind="graded", active_only=False):
    active = Path(active_path)
    if not active.is_file():
        raise FileNotFoundError(f"Required active source does not exist: {active}")
    paths = []
    if not active_only and archive_root is not None:
        root = Path(archive_root)
        if not root.is_dir():
            raise FileNotFoundError(f"Archive root does not exist: {root}")
        paths.extend(sorted((root / kind).glob("*.jsonl")))
    paths.append(active)
    return list(dict.fromkeys(path.resolve() for path in paths))


def grade_order(record):
    return timestamp_us(record.get("gradedAt")), event_hash(record)


def latest_grades(records):
    latest = {}
    for record in records:
        key = alert_id(record)
        order = grade_order(record)
        if key not in latest or order > grade_order(latest[key]):
            latest[key] = record
    return latest


def scheduled_date(alert, timezone=LEAGUE_TIMEZONE):
    if alert.get("scheduledGameDate"):
        return date.fromisoformat(alert["scheduledGameDate"])
    return aware_datetime(alert.get("startTime")).astimezone(ZoneInfo(timezone)).date()


def observation_keys(record):
    """Return unique-line and player-game/stat keys, with no outcome-based choices."""
    alert = record.get("alert") or record
    key = alert_id(record) if record.get("alert") is not None else alert_id(alert)
    player = " ".join(str(alert.get("playerName") or "").casefold().split())
    stat = str(alert.get("statType") or "").strip()
    nba_id = record.get("nbaGameId") or alert.get("nbaGameId")
    if nba_id:
        event = ("nba", str(nba_id).zfill(10))
    else:
        try:
            day = date.fromisoformat(record["gameDate"]) if record.get("gameDate") else scheduled_date(alert)
            event = ("date", day.isoformat(), str(alert.get("game") or "").strip().upper())
        except (ValueError, TypeError):
            event = ("alert", key)
    if not player or not stat:
        event = ("alert", key)
    player_game = (player, stat, *event)
    line = finite_number(alert.get("line"), "line")
    return (*player_game, alert.get("recommendedSide"), line), player_game


def first_post_order(record):
    alert = record.get("alert") or record
    try:
        posted = timestamp_us(alert.get("postedAt"))
    except ValueError:
        posted = 2**63 - 1
    return posted, alert_id(record) if record.get("alert") is not None else alert_id(alert)


def dedupe_observations(records, *, player_game=False):
    grouped = {}
    for record in records:
        key = observation_keys(record)[int(player_game)]
        if key not in grouped or first_post_order(record) < first_post_order(grouped[key]):
            grouped[key] = record
    return list(grouped.values())


def load_grade_history(paths):
    records, sources = [], []
    timestamps = {}
    conflicts = set()
    for path in paths:
        rows, metadata = jsonl_snapshot(path)
        count = 0
        for line, record in rows:
            try:
                identity = alert_id(record)
                order = grade_order(record)
                if record.get("result") not in RESULTS:
                    raise ValueError("invalid grading result")
                timestamp_key = identity, order[0]
                if timestamp_key in timestamps and timestamps[timestamp_key] != record.get("result"):
                    conflicts.add(timestamp_key)
                timestamps[timestamp_key] = record.get("result")
            except ValueError as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
            records.append(record)
            count += 1
        sources.append({**metadata, "records": count})
    latest = latest_grades(records)
    return list(latest.values()), {
        "sources": sources, "gradeEventsRead": len(records), "latestAlerts": len(latest),
        "supersededOrDuplicateEvents": len(records) - len(latest), "equalTimestampResultConflicts": len(conflicts),
        "chronology": "gradedAt UTC microseconds, then canonical event hash",
        "observationPolicy": "earliest posted alert per unique line or player/game/stat",
    }
