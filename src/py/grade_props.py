"""
Grade posted props from data/active/postedProps.jsonl using official NBA player game logs.
Writes data/active/gradedProps.jsonl and reports/gradingSummary.json.
"""

import json
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from nba_api.stats.static import teams
from prop_history import (
    LEAGUE_TIMEZONE, alert_id, aware_datetime, dedupe_observations, finite_number,
    latest_grades as select_latest_grades, load_grade_history, scheduled_date, source_paths,
    jsonl_snapshot,
)

from prop_utils import CURRENT_SEASON, FANTASY_SCORING_VERSION, STAT_MAP, compute_game_total, find_player, get_game_log

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_ACTIVE_DIR = BASE_DIR / "data" / "active"
REPORTS_DIR = BASE_DIR / "reports"
POSTED_PROPS_FILE = str(DATA_ACTIVE_DIR / "postedProps.jsonl")
GRADED_PROPS_FILE = str(DATA_ACTIVE_DIR / "gradedProps.jsonl")
GRADING_SUMMARY_FILE = str(REPORTS_DIR / "gradingSummary.json")
DEFAULT_LOOKBACK_DAYS = int(os.getenv("GRADE_LOOKBACK_DAYS", "2"))
DEFAULT_SETTLEMENT_DELAY_HOURS = float(os.getenv("GRADE_SETTLEMENT_DELAY_HOURS", "4"))
SOURCE_LABEL = "nba_stats_player_game_log"
GRADING_VERSION = 2
GAME_DATE_TIMEZONE = os.getenv("GRADE_GAME_DATE_TIMEZONE", LEAGUE_TIMEZONE)
TEAM_ABBREVIATIONS = {team["abbreviation"] for team in teams.get_teams()}
TEAM_ALIASES = {"PHO": "PHX", "GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS"}


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


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf8") as handle:
        handle.write(json.dumps(record) + "\n")


def build_alert_id(alert):
    return alert_id(alert)


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
        return aware_datetime(raw)
    except ValueError:
        return None


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
    try:
        season = season_from_datetime(scheduled_date(alert, GAME_DATE_TIMEZONE))
    except (ValueError, TypeError):
        season = CURRENT_SEASON
    return alert["playerName"], season


def match_game_row(df, alert):
    if df.empty:
        return None, "No player game log rows found.", "no_game_rows"
    hint = str(alert.get("game") or "").strip().upper()
    hint = TEAM_ALIASES.get(hint, hint)
    if hint and hint not in TEAM_ABBREVIATIONS:
        return None, "Opponent must be a recognized NBA abbreviation.", "invalid_opponent"
    nba_id = alert.get("nbaGameId")
    if nba_id is not None:
        try:
            wanted = normalize_nba_game_id(nba_id)
            column = next(name for name in ("GAME_ID", "Game_ID") if name in df.columns)
            matches = df[df[column].map(normalize_nba_game_id) == wanted]
        except (ValueError, StopIteration):
            return None, "A valid NBA game ID is required in both alert and game log.", "invalid_game_id"
    else:
        if not hint:
            return None, "Date-based matching requires the expected opponent.", "missing_opponent"
        try:
            day = scheduled_date(alert, GAME_DATE_TIMEZONE)
        except (ValueError, TypeError):
            return None, "Scheduled event date needs a valid timezone-aware startTime or scheduledGameDate.", "invalid_event_time"
        if "GAME_DATE_DT" not in df.columns:
            return None, "Game log is missing official game dates.", "missing_game_date"
        matches = df[df["GAME_DATE_DT"].dt.date == day]
    if hint and not matches.empty:
        if "MATCHUP" not in matches.columns:
            return None, "Game log is missing matchup identity.", "missing_matchup"
        opponents = matches["MATCHUP"].map(opponent_from_matchup)
        matches = matches[opponents == hint]
    if matches.empty:
        return None, "No matching NBA event on the scheduled date/ID and opponent; no nearby-game fallback used.", "no_game_found"
    if len(matches) != 1:
        return None, "Multiple possible games matched; grading left unresolved.", "ambiguous_match"
    if "GAME_DATE_DT" not in matches or pd.isna(matches.iloc[0]["GAME_DATE_DT"]):
        return None, "Game log is missing the matched game's official date.", "missing_game_date"
    return matches.iloc[0], None, None


def normalize_nba_game_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,10}", str(value)):
        raise ValueError("NBA game ID must contain 1 to 10 digits")
    return str(value).zfill(10)


def opponent_from_matchup(value):
    match = re.fullmatch(r"\s*[A-Z]{2,3}\s+(?:vs\.?|@)\s+([A-Z]{2,3})\s*", str(value), re.IGNORECASE)
    if not match:
        return None
    opponent = match.group(1).upper()
    return TEAM_ALIASES.get(opponent, opponent)


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
    match_metadata=None,
):
    return {
        "alertId": build_alert_id(alert),
        "gradedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "result": result,
        "source": SOURCE_LABEL,
        "gradingVersion": GRADING_VERSION,
        "settlementBasis": "nba_box_score_comparison",
        "platformSettlementVerified": False,
        "gameDate": game_date,
        "finalValue": final_value,
        "finalMinutes": final_minutes,
        "notes": notes,
        "errorCode": error_code,
        "alert": alert,
        **(match_metadata or {}),
        **(
            {"fantasyScoringVersion": FANTASY_SCORING_VERSION}
            if final_value is not None and STAT_MAP.get(alert.get("statType"), {}).get("type") == "fantasy"
            else {}
        ),
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

    try:
        line = finite_number(alert.get("line"), "line", required=True)
        if line < 0 or alert.get("recommendedSide") not in ("over", "under"):
            raise ValueError("line must be nonnegative and recommendedSide must be over or under")
    except ValueError as exc:
        return build_graded_record(alert, result="unresolved", notes=str(exc), error_code="invalid_alert")

    if not fetch_result["ok"]:
        return build_graded_record(
            alert,
            result="unresolved",
            notes=fetch_result["error"],
            error_code=fetch_result["errorCode"],
        )

    row, match_error, match_code = match_game_row(fetch_result["df"], alert)
    if row is None:
        return build_graded_record(
            alert,
            result="unresolved",
            notes=match_error,
            error_code=match_code or normalize_error_code(match_error),
        )

    match_metadata = {
        "matchMethod": "nba_game_id" if alert.get("nbaGameId") is not None else "league_date_and_opponent" if alert.get("game") else "league_date_only",
        "gameDateTimezone": GAME_DATE_TIMEZONE,
    }
    game_id_value = row.get("GAME_ID", row.get("Game_ID"))
    if game_id_value is not None:
        try:
            match_metadata["nbaGameId"] = normalize_nba_game_id(game_id_value)
        except ValueError:
            return build_graded_record(alert, result="unresolved", notes="Matched row has an invalid NBA game ID.", error_code="invalid_game_id")
    if fetch_result.get("playerId") is not None:
        match_metadata["nbaPlayerId"] = int(fetch_result["playerId"])
    try:
        final_minutes = finite_number(row.get("MIN_FLOAT"), "minutes", required=True)
        if final_minutes < 0:
            raise ValueError("minutes must be nonnegative")
    except ValueError as exc:
        return build_graded_record(alert, result="unresolved", notes=str(exc), error_code="missing_participation", match_metadata=match_metadata)
    game_date = row["GAME_DATE_DT"].date().isoformat()
    if final_minutes == 0:
        return build_graded_record(
            alert,
            result="unresolved",
            notes="Zero participation; platform DNP/settlement evidence is required.",
            error_code="participation_unverified",
            final_value=None,
            final_minutes=final_minutes,
            game_date=game_date,
            match_metadata=match_metadata,
        )
    try:
        final_value = round(finite_number(compute_game_total(row, stat_config), "finalValue", required=True), 1)
    except (KeyError, ValueError, TypeError) as exc:
        return build_graded_record(alert, result="unresolved", notes=f"Invalid or missing stat data: {exc}", error_code="missing_stat_data", game_date=game_date, match_metadata=match_metadata)
    result = settle_result(alert.get("recommendedSide"), line, final_value)
    return build_graded_record(
        alert,
        result=result,
        notes=None,
        error_code=None,
        final_value=final_value,
        final_minutes=round(final_minutes, 3),
        game_date=game_date,
        match_metadata=match_metadata,
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
    from prop_history import observation_keys
    return observation_keys(record)[0]


def dedupe_unique_lines(records):
    return dedupe_observations(records)


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
        "playerGameStatLevel": summarize(dedupe_observations(records, player_game=True)),
        "duplicateAlertsRemoved": len(records) - len(unique_line_records),
        "byGameDate": by_game_date,
        "gameDates": game_dates,
        "primaryGameDate": game_dates[-1] if game_dates else None,
    }


def filter_pending_alerts(posted_records, latest_grades, *, lookback_days=DEFAULT_LOOKBACK_DAYS,
                          settlement_delay_hours=DEFAULT_SETTLEMENT_DELAY_HOURS, regrade=False):
    now_utc = datetime.now(UTC)
    earliest_allowed = now_utc - timedelta(days=lookback_days) if lookback_days is not None else None
    settlement_cutoff = now_utc - timedelta(hours=settlement_delay_hours)
    pending = []
    seen = set()

    for alert in posted_records:
        alert_id = build_alert_id(alert)
        if alert_id in seen:
            continue
        seen.add(alert_id)
        latest = latest_grades.get(alert_id)
        if not regrade and latest and latest["result"] != "unresolved":
            continue

        posted_at = parse_alert_datetime(alert)
        if posted_at is None:
            try:
                posted_at = aware_datetime(alert.get("postedAt"))
            except ValueError:
                # Keep invalid event metadata visible in an explicit all-history review.
                if lookback_days is None:
                    pending.append(alert)
                continue

        if earliest_allowed is not None and posted_at < earliest_allowed:
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


def parse_args():
    parser = argparse.ArgumentParser(description="Evaluate NBA box scores; platform settlement is not verified.")
    parser.add_argument("--posted", type=Path, default=Path(POSTED_PROPS_FILE))
    parser.add_argument("--grades", type=Path, default=Path(GRADED_PROPS_FILE))
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--all-history", action="store_true", help="Read archived posted alerts and disable the age cutoff.")
    parser.add_argument("--regrade", action="store_true", help="Reconsider settled records; requires separate output/dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Write proposals and comparison to separate artifacts.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--game-logs", type=Path, help="Offline NBA rows keyed by player name and season; never falls back to live fetches.")
    parser.add_argument("--lookback-days", type=float, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--settlement-delay-hours", type=float, default=DEFAULT_SETTLEMENT_DELAY_HOURS)
    args = parser.parse_args()
    if args.regrade and not (args.dry_run or args.output_dir):
        parser.error("--regrade requires --dry-run or --output-dir to preserve original outcomes")
    if finite_number(args.lookback_days, "lookback days", required=True) < 0 or finite_number(args.settlement_delay_hours, "settlement delay", required=True) < 0:
        parser.error("lookback and settlement delay must be nonnegative")
    if (args.posted.resolve() != Path(POSTED_PROPS_FILE).resolve() or args.grades.resolve() != Path(GRADED_PROPS_FILE).resolve()) and not (args.dry_run or args.output_dir):
        parser.error("custom sources require --dry-run or --output-dir to protect live history")
    return args


def cached_player_fetch(cache, player_name, season):
    entry = cache.get("players", {}).get(make_fetch_key(player_name, season))
    if entry is None:
        return {"ok": False, "playerId": None, "df": None, "error": "No supplied NBA game-log data for this player/season.", "errorCode": "cached_game_log_missing"}
    frame = pd.DataFrame(entry.get("games", []))
    if not frame.empty:
        if "GAME_DATE_DT" in frame:
            frame["GAME_DATE_DT"] = pd.to_datetime(frame["GAME_DATE_DT"])
        elif "GAME_DATE" in frame:
            frame["GAME_DATE_DT"] = pd.to_datetime(frame["GAME_DATE"], format="mixed")
        if "MIN_FLOAT" not in frame and "MIN" in frame:
            from prop_utils import parse_minutes
            frame["MIN_FLOAT"] = frame["MIN"].map(parse_minutes)
    return {"ok": True, "playerId": entry.get("playerId"), "df": frame, "error": None, "errorCode": None}


def run_grading(posted_records, graded_records, args, offline_cache=None):
    latest_grades = select_latest_grades(graded_records)
    pending_alerts = filter_pending_alerts(
        posted_records, latest_grades, lookback_days=None if args.all_history else args.lookback_days,
        settlement_delay_hours=args.settlement_delay_hours, regrade=args.regrade,
    )
    grouped_alerts = group_alerts(pending_alerts)
    fetch_cache = {}
    new_records = []
    total_groups = len(grouped_alerts)
    proposals, comparisons = [], []

    log_progress(
        f"Pending alerts={len(pending_alerts)} | player-season groups={total_groups}"
    )

    for index, ((player_name, season), alerts) in enumerate(grouped_alerts.items(), start=1):
        log_progress(f"[{index}/{total_groups}] Fetching {player_name} ({season}) for {len(alerts)} alerts")
        if offline_cache is not None:
            fetch_result = cached_player_fetch(offline_cache, player_name, season)
            fetch_cache[make_fetch_key(player_name, season)] = fetch_result
        else:
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
            proposals.append(record)
            previous = latest_grades.get(alert_id)
            compared_fields = ("result", "finalValue", "finalMinutes", "gameDate")
            changed = previous is not None and any(previous.get(key) != record.get(key) for key in compared_fields)
            needs_review = previous is not None and previous["result"] != "unresolved" and record["result"] == "unresolved"
            comparisons.append({
                "alertId": alert_id, "status": "needs_review" if needs_review else "changed" if changed else "new" if previous is None else "unchanged",
                "before": {key: previous.get(key) for key in compared_fields} if previous else None,
                "after": {key: record.get(key) for key in compared_fields}, "errorCode": record.get("errorCode"),
            })
            if not needs_review and (args.regrade or should_append_record(alert_id, latest_grades, record)):
                new_records.append(record)
                latest_grades[alert_id] = record
        log_progress(f"[{index}/{total_groups}] Graded {len(alerts)} alerts for {player_name}")

    batch_summary = build_report_views(new_records)
    batch_summary["newlyGraded"] = len(new_records)
    batch_summary["pendingChecked"] = len(pending_alerts)
    batch_summary["windowDays"] = None if args.all_history else args.lookback_days
    batch_summary["playerGroupsChecked"] = len(grouped_alerts)
    batch_summary["playerFetches"] = len(fetch_cache)

    overall_summary = build_report_views(list(latest_grades.values()))
    overall_summary["newlyGraded"] = len(new_records)
    overall_summary["pendingChecked"] = len(pending_alerts)

    summary = {
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "batch": batch_summary,
        "overall": overall_summary,
        "gradingVersion": GRADING_VERSION,
        "settlementBasis": "nba_box_score_comparison",
        "platformSettlementVerified": False,
        "dryRun": args.dry_run or args.output_dir is not None,
    }
    comparison = {"counts": dict(Counter(row["status"] for row in comparisons)), "records": comparisons}
    return new_records, proposals, summary, comparison


def main():
    args = parse_args()
    archive_root = args.archive_root
    if archive_root is None and args.grades.resolve() == Path(GRADED_PROPS_FILE).resolve() and (BASE_DIR / "archive").is_dir():
        archive_root = BASE_DIR / "archive"
    if args.grades.resolve() == Path(GRADED_PROPS_FILE).resolve() and not args.grades.exists():
        # A new installation has no grade history yet; explicit missing sources still fail.
        grade_paths = [] if args.active_only or archive_root is None else sorted((archive_root / "graded").glob("*.jsonl"))
    else:
        grade_paths = source_paths(args.grades, archive_root, active_only=args.active_only)
    graded_records, source_metadata = load_grade_history(grade_paths)
    posted_paths = source_paths(args.posted, archive_root, kind="posted", active_only=args.active_only or not args.all_history)
    posted_records = [record for path in posted_paths for _, record in jsonl_snapshot(path)[0]]
    offline_cache = json.loads(args.game_logs.read_text(encoding="utf-8-sig")) if args.game_logs else None
    output_dir = args.output_dir or (REPORTS_DIR / "gradingDryRun" if args.dry_run else None)
    if output_dir is not None:
        targets = [output_dir / name for name in ("proposedGrades.jsonl", "acceptedGrades.jsonl", "gradingSummary.json", "gradingComparison.json")]
        protected = {path.resolve() for path in [*grade_paths, *posted_paths, Path(GRADING_SUMMARY_FILE)]}
        if args.game_logs:
            protected.add(args.game_logs.resolve())
        if any(path.resolve() in protected for path in targets):
            raise ValueError("Separate output must not overwrite source history or the live grading summary")
    new_records, proposals, summary, comparison = run_grading(posted_records, graded_records, args, offline_cache)
    summary["historySources"] = source_metadata
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "proposedGrades.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in proposals), encoding="utf-8")
        (output_dir / "acceptedGrades.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in new_records), encoding="utf-8")
        (output_dir / "gradingComparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        summary_path = output_dir / "gradingSummary.json"
    else:
        for record in new_records:
            append_jsonl(GRADED_PROPS_FILE, record)
        summary_path = Path(GRADING_SUMMARY_FILE)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    log_progress(
        f"{'Dry run' if args.dry_run else 'Done'}. Proposed={len(proposals)} | "
        f"comparison={comparison['counts']} | summary={summary_path}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
