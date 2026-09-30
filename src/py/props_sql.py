"""Optional JSONL-to-SQLite analytics; sources are never modified."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

from prop_history import (
    RESULTS, alert_id, aware_datetime, canonical_json, event_hash, finite_number,
    first_post_order, jsonl_snapshot, normalize_identifier, observation_keys,
    source_paths, timestamp_us,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
SUMMARY_QUERY_PATH = REPO_ROOT / "sql" / "performance_summary.sql"
SCHEMA_VERSION = 2
DEFAULT_POSTED = REPO_ROOT / "data/active/postedProps.jsonl"
DEFAULT_GRADES = REPO_ROOT / "data/active/gradedProps.jsonl"


def iter_jsonl(path):
    return (record for _, record in jsonl_snapshot(path)[0])


def validate_posted(record, identity=None):
    identity = identity or alert_id(record)
    for field in ("playerName", "statType"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"{field} must be a nonempty string")
    if finite_number(record.get("line"), "line", required=True) < 0:
        raise ValueError("line must be nonnegative")
    if record.get("recommendedSide") not in ("over", "under"):
        raise ValueError("recommendedSide must be over or under")
    aware_datetime(record.get("postedAt"))
    if record.get("startTime") is not None:
        aware_datetime(record["startTime"])
    if record.get("scheduledGameDate") is not None:
        date.fromisoformat(record["scheduledGameDate"])
    if record.get("propId") is not None:
        normalize_identifier(record["propId"], "propId")
    finite_number(record.get("score"), "score")
    analytics = record.get("analytics")
    if analytics is not None and not isinstance(analytics, dict):
        raise ValueError("analytics must be an object")
    for key in ("pOverAdjusted", "pUnderAdjusted", "pOverFull", "pUnderFull"):
        if (analytics or {}).get(key) is not None:
            prob = finite_number(analytics[key], key, required=True)
            if not 0 <= prob <= 1:
                raise ValueError(f"{key} must be between 0 and 1")
    canonical_json(record)
    return identity


def _upsert_posted(connection, record, identity=None):
    identity = validate_posted(record, identity)
    raw = canonical_json(record)
    previous = connection.execute("SELECT raw_json FROM posted_props WHERE alert_id=?", (identity,)).fetchone()
    if previous and previous[0] == raw:
        return "unchangedPosts"
    connection.execute("""INSERT INTO posted_props (
        alert_id, prop_id, posted_at, player_name, stat_type, line, recommended_side,
        tier, score, game, start_time, analytics_json, raw_json, posted_at_us
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(alert_id) DO UPDATE SET
        prop_id=excluded.prop_id, posted_at=excluded.posted_at, player_name=excluded.player_name,
        stat_type=excluded.stat_type, line=excluded.line, recommended_side=excluded.recommended_side,
        tier=excluded.tier, score=excluded.score, game=excluded.game, start_time=excluded.start_time,
        analytics_json=excluded.analytics_json, raw_json=excluded.raw_json, posted_at_us=excluded.posted_at_us""",
        (identity, None if record.get("propId") is None else str(record["propId"]), record["postedAt"],
         record["playerName"], record["statType"], finite_number(record["line"], "line"),
         record["recommendedSide"], record.get("tier"), finite_number(record.get("score"), "score"),
         record.get("game"), record.get("startTime"), canonical_json(record.get("analytics") or {}),
         raw, timestamp_us(record["postedAt"])))
    return "updatedPosts" if previous else "insertedPosts"


def observation_columns(record):
    unique, player = observation_keys(record)
    return canonical_json(unique), canonical_json(player), first_post_order(record)[0]


def _insert_grade(connection, record, counts):
    identity = alert_id(record)
    graded_us = timestamp_us(record.get("gradedAt"))
    if record.get("result") not in RESULTS:
        raise ValueError("invalid grading result")
    final = finite_number(record.get("finalValue"), "finalValue")
    minutes = finite_number(record.get("finalMinutes"), "finalMinutes")
    if minutes is not None and minutes < 0:
        raise ValueError("finalMinutes must be nonnegative")
    if record.get("gameDate") is not None:
        date.fromisoformat(record["gameDate"])
    nested = record.get("alert")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ValueError("alert snapshot must be an object")
        # Derive legacy snapshot IDs with the original formula, never silently replace mismatches.
        nested_id = alert_id(nested) if nested.get("alertId") is not None or nested.get("propId") is not None else identity
        if nested_id != identity:
            raise ValueError("nested alertId does not match grade alertId")
        validate_posted(nested, identity)
    posted = connection.execute("SELECT raw_json FROM posted_props WHERE alert_id=?", (identity,)).fetchone()
    if posted is None:
        if nested is None:
            raise ValueError(f"grade for alert {identity} has no matching posted prop or nested alert snapshot")
        counts[_upsert_posted(connection, nested, identity)] += 1
        counts["snapshotPosts"] += 1
        snapshot = nested
    else:
        snapshot = nested or json.loads(posted[0])
    keyed = {**record, "alert": snapshot}
    unique, player, posted_us = observation_columns(keyed)
    digest = event_hash(record)
    exists = connection.execute("SELECT 1 FROM grade_events WHERE event_hash=?", (digest,)).fetchone()
    connection.execute("""INSERT INTO grade_events (
      event_hash, alert_id, graded_at, result, source, game_date, final_value, final_minutes,
      notes, error_code, graded_at_us, grade_json, unique_line_key, player_game_key, posted_at_us,
      grading_version, fantasy_scoring_version, settlement_basis
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_hash) DO UPDATE SET
      graded_at_us=excluded.graded_at_us, grade_json=excluded.grade_json,
      unique_line_key=excluded.unique_line_key, player_game_key=excluded.player_game_key,
      posted_at_us=excluded.posted_at_us, grading_version=excluded.grading_version,
      fantasy_scoring_version=excluded.fantasy_scoring_version, settlement_basis=excluded.settlement_basis""",
      (digest, identity, record["gradedAt"], record["result"], record.get("source"), record.get("gameDate"),
       final, minutes, record.get("notes"), record.get("errorCode"), graded_us, canonical_json(record),
       unique, player, posted_us, record.get("gradingVersion"), record.get("fantasyScoringVersion"), record.get("settlementBasis")))
    counts["duplicateGradeEvents" if exists else "insertedGradeEvents"] += 1


def import_sources(connection, posted_paths, grade_paths):
    counts = Counter({key: 0 for key in ("postedRecordsRead", "gradeEventsRead", "insertedPosts",
        "updatedPosts", "unchangedPosts", "snapshotPosts", "insertedGradeEvents", "duplicateGradeEvents")})
    sources = []
    with connection:
        for kind, paths in (("posted", posted_paths), ("graded", grade_paths)):
            for path in paths:
                rows, metadata = jsonl_snapshot(path)
                scanned = 0
                for line, record in rows:
                    try:
                        if kind == "posted":
                            counts[_upsert_posted(connection, record)] += 1
                            counts["postedRecordsRead"] += 1
                        else:
                            _insert_grade(connection, record, counts)
                            counts["gradeEventsRead"] += 1
                    except (ValueError, TypeError, sqlite3.Error) as exc:
                        raise ValueError(f"{path}:{line}: {exc}") from exc
                    scanned += 1
                sources.append({**metadata, "kind": kind, "records": scanned})
        connection.execute("INSERT INTO import_runs(completed_at,sources_json,counts_json,schema_version) VALUES (?,?,?,?)",
            (datetime.now(UTC).isoformat(), canonical_json(sources), canonical_json(dict(counts)), SCHEMA_VERSION))
    return {"counts": dict(counts), "sources": sources, "schemaVersion": SCHEMA_VERSION}


def import_jsonl(connection, posted_path, grades_path):
    result = import_sources(connection, [posted_path], [grades_path])["counts"]
    return result["postedRecordsRead"], result["gradeEventsRead"]


def open_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError(f"Unsupported future schema version {version}")
        base, views = SCHEMA_PATH.read_text(encoding="utf-8").split("-- ANALYTICS VIEWS")
        # executescript commits a pending transaction: begin inside the script, commit after backfill.
        connection.executescript("BEGIN IMMEDIATE;\n" + base)
        additions = {
            "posted_props": {"raw_json": "TEXT", "posted_at_us": "INTEGER"},
            "grade_events": {"graded_at_us": "INTEGER", "grade_json": "TEXT", "unique_line_key": "TEXT",
                "player_game_key": "TEXT", "posted_at_us": "INTEGER", "grading_version": "INTEGER",
                "fantasy_scoring_version": "INTEGER", "settlement_basis": "TEXT"},
        }
        for table, fields in additions.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column, kind in fields.items():
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT * FROM posted_props WHERE raw_json IS NULL").fetchall():
            legacy = {"alertId": row["alert_id"], "propId": row["prop_id"], "postedAt": row["posted_at"],
                "playerName": row["player_name"], "statType": row["stat_type"], "line": row["line"],
                "recommendedSide": row["recommended_side"], "game": row["game"], "startTime": row["start_time"],
                "tier": row["tier"], "score": row["score"], "analytics": json.loads(row["analytics_json"]),
                "sqlSnapshotReconstructed": True}
            try:
                posted_us = timestamp_us(row["posted_at"])
            except ValueError:
                posted_us = 2**63 - 1
            connection.execute("UPDATE posted_props SET raw_json=?,posted_at_us=? WHERE alert_id=?",
                (canonical_json(legacy), posted_us, row["alert_id"]))
        for row in connection.execute("SELECT g.*,p.raw_json FROM grade_events g JOIN posted_props p USING(alert_id) WHERE g.graded_at_us IS NULL OR g.unique_line_key IS NULL").fetchall():
            legacy = {"alertId": row["alert_id"], "gradedAt": row["graded_at"], "gameDate": row["game_date"], "alert": json.loads(row["raw_json"])}
            if row["result"] not in RESULTS:
                raise ValueError("Migration found an invalid legacy result; source repair/rebuild required")
            unique, player, posted_us = observation_columns(legacy)
            connection.execute("UPDATE grade_events SET graded_at_us=?,unique_line_key=?,player_game_key=?,posted_at_us=? WHERE id=?",
                (timestamp_us(row["graded_at"]), unique, player, posted_us, row["id"]))
        connection.row_factory = None
        # Execute individual view statements without executescript's implicit commit.
        for statement in views.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
        return connection
    except Exception:
        connection.rollback()
        connection.close()
        raise


def summarize(connection):
    return connection.execute(SUMMARY_QUERY_PATH.read_text(encoding="utf-8")).fetchall()


def database_health(connection):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign:
        raise ValueError(f"Database checks failed: integrity={integrity}, foreign keys={foreign}")
    counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("posted_props", "grade_events", "latest_grades", "unique_line_results", "player_game_stat_results")}
    counts["equalTimestampResultConflicts"] = connection.execute("""SELECT COUNT(*) FROM (
        SELECT alert_id,graded_at_us FROM grade_events GROUP BY alert_id,graded_at_us HAVING COUNT(DISTINCT result)>1)""").fetchone()[0]
    counts["eventsWithoutRawSnapshot"] = connection.execute("SELECT COUNT(*) FROM grade_events WHERE grade_json IS NULL").fetchone()[0]
    return {"integrity": integrity, "foreignKeyViolations": len(foreign), **counts}


def rebuild_database(path, posted_paths, grade_paths):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory allows atomic replacement. A failure leaves the existing DB intact.
    descriptor, name = tempfile.mkstemp(prefix="props-rebuild-", suffix=".sqlite3", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    connection = None
    try:
        connection = open_database(temporary)
        metadata = import_sources(connection, posted_paths, grade_paths)
        health = database_health(connection)
        connection.close()
        connection = None
        os.replace(temporary, path)
        return metadata, health
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Import active and archived JSONL into optional local analytics SQLite.")
    parser.add_argument("--posted", type=Path, default=DEFAULT_POSTED)
    parser.add_argument("--grades", type=Path, default=DEFAULT_GRADES)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="Replace the database atomically with exactly these sources.")
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "data/active/props_analytics.sqlite3")
    args = parser.parse_args()
    archive = args.archive_root
    if archive is None and args.posted.resolve() == DEFAULT_POSTED.resolve() and args.grades.resolve() == DEFAULT_GRADES.resolve() and (REPO_ROOT / "archive").is_dir():
        archive = REPO_ROOT / "archive"
    posted = source_paths(args.posted, archive, kind="posted", active_only=args.active_only)
    graded = source_paths(args.grades, archive, active_only=args.active_only)
    if args.database.resolve() in {*posted, *graded}:
        parser.error("database must not replace a source file")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    if args.rebuild:
        metadata, health = rebuild_database(args.database, posted, graded)
    else:
        connection = open_database(args.database)
        try:
            metadata = import_sources(connection, posted, graded)
            health = database_health(connection)
        finally:
            connection.close()
    print(json.dumps({"database": str(args.database.resolve()), **metadata, "health": health}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
