"""
Grade posted props from data/active/postedProps.jsonl using official NBA player game logs.
Writes data/active/gradedProps.jsonl and reports/gradingSummary.json.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from prop_utils import CURRENT_SEASON, STAT_MAP, compute_game_total, find_player, get_game_log

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_ACTIVE_DIR = BASE_DIR / "data" / "active"
REPORTS_DIR = BASE_DIR / "reports"
POSTED_PROPS_FILE = str(DATA_ACTIVE_DIR / "postedProps.jsonl")
GRADED_PROPS_FILE = str(DATA_ACTIVE_DIR / "gradedProps.jsonl")
GRADING_SUMMARY_FILE = str(REPORTS_DIR / "gradingSummary.json")
VOID_MINUTES_THRESHOLD = float(os.getenv("GRADE_VOID_MINUTES_THRESHOLD", "5"))
DEFAULT_LOOKBACK_DAYS = int(os.getenv("GRADE_LOOKBACK_DAYS", "2"))
DEFAULT_SETTLEMENT_DELAY_HOURS = float(os.getenv("GRADE_SETTLEMENT_DELAY_HOURS", "4"))
SOURCE_LABEL = "official_nba_box_score"


def log_progress(message):
    print(f"[grading] {message}", file=sys.stderr, flush=True)


def init_record_bucket():
    return {
        "gradedCount": 0,
        "win": 0,
        "loss": 0,
        "push": 0,
        "void": 0,
        "unresolved": 0,
        "countable": 0,
        "winRate": 0,
    }


def update_record_bucket(bucket, result):
    bucket["gradedCount"] += 1
    bucket[result] += 1
    if result in ("win", "loss"):
        bucket["countable"] += 1


def finalize_record_buckets(buckets):
    for bucket in buckets.values():
        if bucket["countable"] > 0:
            bucket["winRate"] = round((bucket["win"] / bucket["countable"]) * 100, 1)


def get_score_band(score):
    if score is None:
        return "unknown"
    if score < 6.5:
        return "<6.5"
    if score < 7.0:
        return "6.5-6.9"
    if score < 7.5:
        return "7.0-7.4"
    if score < 8.0:
        return "7.5-7.9"
    return "8.0+"


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


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf8") as handle:
        handle.write(json.dumps(record) + "\n")


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


def season_from_datetime(dt):
    year = dt.year
    season_start_year = year if dt.month >= 7 else year - 1
    season_end_year = str((season_start_year + 1) % 100).zfill(2)
    return f"{season_start_year}-{season_end_year}"


def parse_alert_datetime(alert):
    raw = alert.get("startTime") or alert.get("postedAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_target_dates(alert_dt, window_days=1):
    if not alert_dt:
        return []
    base_date = alert_dt.date()
    return [
        base_date + timedelta(days=offset)
        for offset in range(-window_days, window_days + 1)
    ]


def normalize_error_code(notes):
    lower = (notes or "").lower()
    if "read timed out" in lower or "timeout" in lower:
        return "fetch_timeout"
    if "multiple possible games matched" in lower:
        return "ambiguous_match"
    if "no player game found near scheduled date" in lower:
        return "no_game_found"
    if "unsupported stat type" in lower:
        return "unsupported_stat"
    if "player not found" in lower:
        return "player_not_found"
    if "failed to fetch game log" in lower:
        return "fetch_failed"
    return "unknown"


def make_fetch_key(player_name, season):
    return f"{player_name}|{season}"


def make_group_key(alert):
    alert_dt = parse_alert_datetime(alert)
    season = season_from_datetime(alert_dt) if alert_dt else CURRENT_SEASON
    return alert["playerName"], season


def match_game_row(df, alert):
    alert_dt = parse_alert_datetime(alert)
    target_dates = get_target_dates(alert_dt, window_days=1)
    game_hint = str(alert.get("game") or "").upper()

    if df.empty:
        return None, "No player game log rows found.", "no_game_rows"

    by_date = []
    for target_date in target_dates:
        matches = df[df["GAME_DATE_DT"].dt.date == target_date]
        if not matches.empty:
            by_date.append(matches)

    if not by_date:
        fallback_dates = get_target_dates(alert_dt, window_days=2)
        fallback_matches = []
        for target_date in fallback_dates:
            matches = df[df["GAME_DATE_DT"].dt.date == target_date]
            if not matches.empty:
                fallback_matches.append(matches)

        if not fallback_matches:
            return None, "No player game found near scheduled date.", "no_game_found"

        for matches in fallback_matches:
            if game_hint:
                opponent_matches = matches[matches["MATCHUP"].str.contains(game_hint, case=False, na=False)]
                if len(opponent_matches) == 1:
                    return opponent_matches.iloc[0], None, None
            if len(matches) == 1:
                return matches.iloc[0], None, None

        return None, "Multiple possible games matched; grading left unresolved.", "ambiguous_match"

    for matches in by_date:
        if game_hint:
            opponent_matches = matches[matches["MATCHUP"].str.contains(game_hint, case=False, na=False)]
            if len(opponent_matches) == 1:
                return opponent_matches.iloc[0], None, None
        if len(matches) == 1:
            return matches.iloc[0], None, None

    return None, "Multiple possible games matched; grading left unresolved.", "ambiguous_match"


def settle_result(side, line, final_value):
    if side == "over":
        if final_value > line:
            return "win"
        if final_value == line:
            return "push"
        return "loss"

    if side == "under":
        if final_value < line:
            return "win"
        if final_value == line:
            return "push"
        return "loss"

    return "unresolved"


def build_graded_record(
    alert,
    *,
    result,
    notes,
    error_code=None,
    final_value=None,
    final_minutes=None,
    game_date=None,
):
    return {
        "alertId": build_alert_id(alert),
        "gradedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "result": result,
        "source": SOURCE_LABEL,
        "gameDate": game_date,
        "finalValue": final_value,
        "finalMinutes": final_minutes,
        "notes": notes,
        "errorCode": error_code,
        "alert": alert,
    }


def get_player_fetch(fetch_cache, player_name, season):
    fetch_key = make_fetch_key(player_name, season)
    if fetch_key in fetch_cache:
        return fetch_cache[fetch_key]

    try:
        player_id, _ = find_player(player_name)
        df = get_game_log(player_id, season=season)
        result = {"ok": True, "playerId": player_id, "df": df, "error": None, "errorCode": None}
    except Exception as exc:
        message = str(exc)
        result = {
            "ok": False,
            "playerId": None,
            "df": None,
            "error": message,
            "errorCode": normalize_error_code(message),
        }

    fetch_cache[fetch_key] = result
    return result


def grade_alert(alert, fetch_result):
    stat_type = alert.get("statType")
    stat_config = STAT_MAP.get(stat_type)
    if not stat_config:
        return build_graded_record(
            alert,
            result="unresolved",
            notes=f"Unsupported stat type: {stat_type}",
            error_code="unsupported_stat",
        )

    if not fetch_result["ok"]:
        return build_graded_record(
            alert,
            result="unresolved",
            notes=fetch_result["error"],
            error_code=fetch_result["errorCode"],
        )

    row, match_error, match_code = match_game_row(fetch_result["df"], alert)
    if row is None:
        if match_code == "no_game_found":
            return build_graded_record(
                alert,
                result="unresolved",
                notes="No player game log entry found near scheduled date.",
                error_code=match_code,
                final_value=None,
            )
        return build_graded_record(
            alert,
            result="unresolved",
            notes=match_error,
            error_code=match_code or normalize_error_code(match_error),
        )

    final_minutes = round(float(row.get("MIN_FLOAT", 0.0)), 1)
    game_date = row["GAME_DATE_DT"].date().isoformat()

    if final_minutes < VOID_MINUTES_THRESHOLD:
        return build_graded_record(
            alert,
            result="void",
            notes=f"Player logged {final_minutes} minutes, below void threshold {VOID_MINUTES_THRESHOLD}.",
            error_code="void_low_minutes",
            final_value=None,
            final_minutes=final_minutes,
            game_date=game_date,
        )

    final_value = round(float(compute_game_total(row, stat_config)), 1)
    result = settle_result(alert.get("recommendedSide"), float(alert.get("line", 0)), final_value)
    return build_graded_record(
        alert,
        result=result,
        notes=None,
        error_code=None,
        final_value=final_value,
        final_minutes=final_minutes,
        game_date=game_date,
    )


def summarize(records):
    summary = {
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "gradedCount": 0,
        "win": 0,
        "loss": 0,
        "push": 0,
        "void": 0,
        "unresolved": 0,
        "countable": 0,
        "winRate": 0,
        "unresolvedByReason": {},
        "byGameDate": {},
        "primaryGameDate": None,
        "bySide": {
            "over": {"win": 0, "loss": 0, "push": 0, "void": 0, "unresolved": 0},
            "under": {"win": 0, "loss": 0, "push": 0, "void": 0, "unresolved": 0},
        },
        "byTier": {
            "best_bet": {"win": 0, "loss": 0, "push": 0, "void": 0, "unresolved": 0},
            "watchlist": {"win": 0, "loss": 0, "push": 0, "void": 0, "unresolved": 0},
        },
        "byStatType": {},
        "byScoreBand": {},
    }

    unresolved_counter = Counter()
    game_date_counter = Counter()
    stat_type_buckets = defaultdict(init_record_bucket)
    score_band_buckets = defaultdict(init_record_bucket)

    for record in records:
        result = record["result"]
        alert = record["alert"]
        side = alert.get("recommendedSide")
        tier = alert.get("tier")
        stat_type = alert.get("statType") or "unknown"
        score_band = get_score_band(alert.get("score"))
        game_date = record.get("gameDate")
        summary["gradedCount"] += 1
        summary[result] += 1

        if result in ("win", "loss"):
            summary["countable"] += 1

        if result == "unresolved":
            unresolved_counter[record.get("errorCode") or "unknown"] += 1

        if side in summary["bySide"]:
            summary["bySide"][side][result] += 1
        if tier in summary["byTier"]:
            summary["byTier"][tier][result] += 1
        update_record_bucket(stat_type_buckets[stat_type], result)
        update_record_bucket(score_band_buckets[score_band], result)
        if game_date:
            game_date_counter[game_date] += 1

    if summary["countable"] > 0:
        summary["winRate"] = round((summary["win"] / summary["countable"]) * 100, 1)

    finalize_record_buckets(stat_type_buckets)
    finalize_record_buckets(score_band_buckets)
    summary["unresolvedByReason"] = dict(unresolved_counter)
    summary["byGameDate"] = dict(sorted(game_date_counter.items()))
    summary["byStatType"] = dict(
        sorted(
            stat_type_buckets.items(),
            key=lambda item: (-item[1]["countable"], item[0]),
        )
    )
    score_band_order = ["<6.5", "6.5-6.9", "7.0-7.4", "7.5-7.9", "8.0+", "unknown"]
    summary["byScoreBand"] = {
        band: score_band_buckets[band]
        for band in score_band_order
        if band in score_band_buckets
    }
    if game_date_counter:
        summary["primaryGameDate"] = game_date_counter.most_common(1)[0][0]
    return summary


def build_unique_line_key(record):
    alert = record["alert"]
    return (
        alert.get("playerName"),
        alert.get("statType"),
        alert.get("recommendedSide"),
        record.get("gameDate"),
        alert.get("line"),
    )


def get_record_posted_at(record):
    posted_at = parse_alert_datetime(record["alert"])
    if posted_at is None:
        return datetime.max.replace(tzinfo=UTC)
    if posted_at.tzinfo is None:
        return posted_at.replace(tzinfo=UTC)
    return posted_at


def dedupe_unique_lines(records):
    grouped = {}
    for record in records:
        key = build_unique_line_key(record)
        existing = grouped.get(key)
        if existing is None or get_record_posted_at(record) < get_record_posted_at(existing):
            grouped[key] = record
    return list(grouped.values())


def group_records_by_game_date(records):
    grouped = defaultdict(list)
    for record in records:
        game_date = record.get("gameDate")
        if game_date:
            grouped[game_date].append(record)
    return grouped


def build_report_views(records):
    unique_line_records = dedupe_unique_lines(records)
    grouped_alert_level = group_records_by_game_date(records)
    grouped_unique_line = group_records_by_game_date(unique_line_records)
    game_dates = sorted(set(grouped_alert_level) | set(grouped_unique_line))
    by_game_date = {}

    for game_date in game_dates:
        alert_records = grouped_alert_level.get(game_date, [])
        unique_records = grouped_unique_line.get(game_date, [])
        by_game_date[game_date] = {
            "alertLevel": summarize(alert_records),
            "uniqueLineLevel": summarize(unique_records),
            "duplicateAlertsRemoved": len(alert_records) - len(unique_records),
        }

    return {
        "alertLevel": summarize(records),
        "uniqueLineLevel": summarize(unique_line_records),
        "duplicateAlertsRemoved": len(records) - len(unique_line_records),
        "byGameDate": by_game_date,
        "gameDates": game_dates,
        "primaryGameDate": game_dates[-1] if game_dates else None,
    }


def filter_pending_alerts(posted_records, latest_grades):
    now_utc = datetime.now(UTC)
    earliest_allowed = now_utc - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    settlement_cutoff = now_utc - timedelta(hours=DEFAULT_SETTLEMENT_DELAY_HOURS)
    pending = []

    for alert in posted_records:
        alert_id = build_alert_id(alert)
        latest = latest_grades.get(alert_id)
        if latest and latest["result"] != "unresolved":
            continue

        posted_at = parse_alert_datetime(alert)
        if posted_at is None:
            continue

        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=UTC)

        if posted_at < earliest_allowed:
            continue

        if posted_at > settlement_cutoff:
            continue

        pending.append(alert)

    return pending


def should_append_record(alert_id, latest_grades, new_record):
    previous = latest_grades.get(alert_id)
    if not previous:
        return True

    if previous["result"] != "unresolved":
        if (
            previous["result"] == "void"
            and previous.get("errorCode") == "no_game_found"
            and new_record["result"] == "unresolved"
            and new_record.get("errorCode") == "no_game_found"
        ):
            return True
        return False

    if new_record["result"] != "unresolved":
        return True

    previous_code = previous.get("errorCode")
    new_code = new_record.get("errorCode")
    previous_notes = previous.get("notes")
    new_notes = new_record.get("notes")
    return not (previous_code == new_code and previous_notes == new_notes)


def group_alerts(alerts):
    grouped = defaultdict(list)
    for alert in alerts:
        grouped[make_group_key(alert)].append(alert)
    return grouped


def main():
    posted_records = load_jsonl(POSTED_PROPS_FILE)
    graded_records = load_jsonl(GRADED_PROPS_FILE)

    latest_grades = {}
    for record in graded_records:
        latest_grades[record["alertId"]] = record

    pending_alerts = filter_pending_alerts(posted_records, latest_grades)
    grouped_alerts = group_alerts(pending_alerts)
    fetch_cache = {}
    new_records = []
    total_groups = len(grouped_alerts)

    log_progress(
        f"Pending alerts={len(pending_alerts)} | player-season groups={total_groups}"
    )

    for index, ((player_name, season), alerts) in enumerate(grouped_alerts.items(), start=1):
        log_progress(f"[{index}/{total_groups}] Fetching {player_name} ({season}) for {len(alerts)} alerts")
        fetch_result = get_player_fetch(fetch_cache, player_name, season)
        if fetch_result["ok"]:
            log_progress(f"[{index}/{total_groups}] Fetched {player_name} ({season})")
        else:
            log_progress(
                f"[{index}/{total_groups}] Fetch failed for {player_name} ({season}): "
                f"{fetch_result['errorCode'] or 'unknown'}"
            )
        for alert in alerts:
            record = grade_alert(alert, fetch_result)
            alert_id = record["alertId"]
            if should_append_record(alert_id, latest_grades, record):
                append_jsonl(GRADED_PROPS_FILE, record)
                new_records.append(record)
            latest_grades[alert_id] = record
        log_progress(f"[{index}/{total_groups}] Graded {len(alerts)} alerts for {player_name}")

    batch_summary = build_report_views(new_records)
    batch_summary["newlyGraded"] = len(new_records)
    batch_summary["pendingChecked"] = len(pending_alerts)
    batch_summary["windowDays"] = DEFAULT_LOOKBACK_DAYS
    batch_summary["playerGroupsChecked"] = len(grouped_alerts)
    batch_summary["playerFetches"] = len(fetch_cache)

    overall_summary = build_report_views(list(latest_grades.values()))
    overall_summary["newlyGraded"] = len(new_records)
    overall_summary["pendingChecked"] = len(pending_alerts)

    summary = {
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "batch": batch_summary,
        "overall": overall_summary,
    }

    os.makedirs(os.path.dirname(GRADING_SUMMARY_FILE), exist_ok=True)
    with open(GRADING_SUMMARY_FILE, "w", encoding="utf8") as handle:
        json.dump(summary, handle, indent=2)

    log_progress(
        f"Done. Newly graded={len(new_records)} | "
        f"pending checked={len(pending_alerts)} | fetches={len(fetch_cache)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
