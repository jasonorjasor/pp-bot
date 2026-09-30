"""
src/py/projection_report.py

Summarize projection probabilities vs graded outcomes.
Usage:
    python src/py/projection_report.py [--days N] [--start-date YYYY-MM-DD]
        [--game-date YYYY-MM-DD] [--include-predeploy]
"""

from __future__ import annotations

import argparse
import json
import os
import math
from pathlib import Path
from prop_history import aware_datetime, dedupe_observations, load_grade_history, source_paths
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_ACTIVE_DIR = os.path.join(BASE_DIR, "data", "active")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

GRADED_PROPS_FILE = os.path.join(DATA_ACTIVE_DIR, "gradedProps.jsonl")
CALIBRATION_ARTIFACT_FILE = os.path.join(REPORTS_DIR, "projectionCalibration.json")
DEFAULT_MIN_COUNT = 25
DEFAULT_CALIBRATION_MIN_SIDE_COUNT = 250
DEFAULT_CALIBRATION_MIN_FAMILY_COUNT = 100
BUCKET_ORDER = ["0-49", "50-54", "55-59", "60-64", "65-69", "70-74", "75-79", "80+"]
STABLE_BANDS = ["stable", "medium", "fragile"]
NEAR_THRESHOLD_MIN = 0.50
NEAR_THRESHOLD_MAX = 0.60
PROJECTION_FAMILY_POLICY = {
    "Points": "watch_only",
    "Pts+Asts": "watch_only",
    "Pts+Rebs+Asts": "watch_only",
    "Pts+Rebs": "watch_only",
    "Rebounds": "watch_only",
    "Rebs+Asts": "watch_only",
    "Assists": "watch_only",
    "3PM": "watch_only",
    "Fantasy Score": "watch_only",
    "Turnovers": "watch_only",
    "Blocks": "watch_only",
    "Steals": "watch_only",
}


def parse_args():
    parser = argparse.ArgumentParser()
    add_history_args(parser)
    parser.add_argument("--days", type=int, default=7, help="Rolling lookback window based on gradedAt.")
    parser.add_argument("--start-date", type=str, help="Inclusive gradedAt cutoff in YYYY-MM-DD format.")
    parser.add_argument("--game-date", type=str, help="Exact slate date (gameDate) to analyze.")
    parser.add_argument(
        "--post-deploy-only",
        action="store_true",
        help="Only include records at or after the first projection-enabled record.",
    )
    parser.add_argument(
        "--include-predeploy",
        action="store_true",
        help="Include records before the first projection-enabled record.",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=DEFAULT_MIN_COUNT,
        help="Minimum count for calibration highlights.",
    )
    parser.add_argument(
        "--calibration-min-side-count",
        type=int,
        default=DEFAULT_CALIBRATION_MIN_SIDE_COUNT,
        help="Minimum stable-row count required to fit side calibration.",
    )
    parser.add_argument(
        "--calibration-min-family-count",
        type=int,
        default=DEFAULT_CALIBRATION_MIN_FAMILY_COUNT,
        help="Minimum stable-row count required to fit stat-family calibration.",
    )
    parser.add_argument(
        "--output-artifact",
        type=str,
        default=CALIBRATION_ARTIFACT_FILE,
        help="Path to write the calibration artifact JSON.",
    )
    return validate_report_args(parser, parser.parse_args())


def add_history_args(parser):
    parser.add_argument("--grades", type=Path, default=Path(GRADED_PROPS_FILE))
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--all-history", action="store_true", help="Disable the rolling time cutoff; source defaults include archives.")
    parser.add_argument("--include-legacy-fantasy", action="store_true", help="Include predictions/labels from the old formula in research metrics.")


def validate_report_args(parser, args):
    if args.all_history:
        args.days = None
    if args.days is not None and args.days < 0:
        parser.error("--days must be nonnegative")
    for field in ("start_date", "game_date"):
        if getattr(args, field) and parse_date(getattr(args, field)) is None:
            parser.error(f"--{field.replace('_', '-')} requires YYYY-MM-DD")
    if args.include_predeploy and args.post_deploy_only:
        parser.error("predeploy filter options conflict")
    for field in ("min_count", "calibration_min_side_count", "calibration_min_family_count"):
        if hasattr(args, field) and getattr(args, field) < 1:
            parser.error("minimum counts must be positive")
    return args


def read_report_history(args):
    root = args.archive_root
    default_archive = Path(BASE_DIR) / "archive"
    if root is None and args.grades.resolve() == Path(GRADED_PROPS_FILE).resolve() and default_archive.is_dir():
        root = default_archive
    return load_grade_history(source_paths(args.grades, root, active_only=args.active_only))


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


def parse_dt(raw):
    if not raw:
        return None
    try:
        return aware_datetime(raw)
    except ValueError:
        return None


def parse_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def has_projection(alert):
    analytics = alert.get("analytics") or {}
    return (
        analytics.get("projectionMethod") is not None
        or analytics.get("projectionMinutes") is not None
        or analytics.get("pOverAdjusted") is not None
        or analytics.get("pUnderAdjusted") is not None
    )


def has_confidence(alert):
    analytics = alert.get("analytics") or {}
    return any(
        analytics.get(key) is not None
        for key in (
            "projectionConfidenceBand",
            "projectionConfidenceReasons",
            "projectionLowConfidence",
            "projectionFamilyStatus",
        )
    )


def get_posted_side(alert):
    for key in ("postedSide", "recommendedSide"):
        side = (alert.get(key) or "").lower()
        if side in ("over", "under"):
            return side
    return None


def safe_float(value):
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def get_probability_for_side(analytics, side):
    if side not in ("over", "under"):
        return None
    side_key = "Over" if side == "over" else "Under"
    raw = analytics.get(f"p{side_key}Adjusted")
    if raw is None:
        raw = analytics.get(f"p{side_key}Full")
    prob = safe_float(raw)
    return prob if prob is not None and 0 <= prob <= 1 else None


def choose_probability(alert):
    side = get_posted_side(alert)
    analytics = alert.get("analytics") or {}
    if side is None:
        return None
    return get_probability_for_side(analytics, side)


def get_projection_side(analytics):
    over_prob = get_probability_for_side(analytics, "over")
    under_prob = get_probability_for_side(analytics, "under")

    if over_prob is None or under_prob is None:
        return None
    if abs(over_prob - under_prob) < 1e-12:
        return "tie"
    return "over" if over_prob > under_prob else "under"


def bucket_label(prob):
    if prob < 0.50:
        return "0-49"
    if prob < 0.55:
        return "50-54"
    if prob < 0.60:
        return "55-59"
    if prob < 0.65:
        return "60-64"
    if prob < 0.70:
        return "65-69"
    if prob < 0.75:
        return "70-74"
    if prob < 0.80:
        return "75-79"
    return "80+"


def empty_bucket():
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "prob_sum": 0.0,
        "brier_sum": 0.0,
    }


def update_bucket(bucket, prob, outcome):
    bucket["count"] += 1
    bucket["wins"] += int(outcome == 1)
    bucket["losses"] += int(outcome == 0)
    bucket["prob_sum"] += prob
    bucket["brier_sum"] += (prob - outcome) ** 2


def finalize_bucket(bucket):
    if bucket["count"] > 0:
        bucket["winRate"] = round((bucket["wins"] / bucket["count"]) * 100, 1)
        bucket["avgProb"] = round((bucket["prob_sum"] / bucket["count"]) * 100, 1)
        bucket["gap"] = round(bucket["winRate"] - bucket["avgProb"], 1)
        bucket["brier"] = round(bucket["brier_sum"] / bucket["count"], 4)
    else:
        bucket["winRate"] = 0.0
        bucket["avgProb"] = 0.0
        bucket["gap"] = 0.0
        bucket["brier"] = 0.0


def make_summary():
    return {
        "gradedCount": 0,
        "win": 0,
        "loss": 0,
        "push": 0,
        "void": 0,
        "unresolved": 0,
        "countable": 0,
        "eligibleWins": 0,
        "eligibleLosses": 0,
        "legacyFantasyExcluded": 0,
        "rawWinRate": 0.0,
        "winRate": 0.0,
        "brier": 0.0,
        "recordsWithProjection": 0,
        "recordsWithConfidence": 0,
        "recordsWithoutProjection": 0,
        "missingProjection": 0,
        "missingConfidence": 0,
        "invalidProbability": 0,
        "voidUngraded": 0,
        "storedLowConfidence": 0,
        "byBucket": {label: empty_bucket() for label in BUCKET_ORDER},
        "bySide": {"over": empty_bucket(), "under": empty_bucket()},
        "byTier": {"best_bet": empty_bucket(), "watchlist": empty_bucket()},
        "byStatType": defaultdict(empty_bucket),
        "byFamilyStatus": defaultdict(empty_bucket),
        "byConfidenceBand": {band: empty_bucket() for band in STABLE_BANDS},
        "byProjectionAgreement": {
            "agree": empty_bucket(),
            "disagree": empty_bucket(),
            "tie": empty_bucket(),
        },
        "nearThreshold": empty_bucket(),
        "methodCounts": Counter(),
        "confidenceReasonCounts": Counter(),
    }


def derive_confidence_band(alert):
    analytics = alert.get("analytics") or {}
    stored_band = str(analytics.get("projectionConfidenceBand") or "").lower()
    if stored_band in STABLE_BANDS:
        return stored_band

    method = str(analytics.get("projectionMethod") or "").lower()
    sample_full = safe_float(analytics.get("projectionSampleSizeFull")) or 0
    sample_limited = safe_float(analytics.get("projectionSampleSizeLimited")) or 0
    stored_low_conf = bool(analytics.get("projectionLowConfidence"))

    if stored_low_conf:
        return "fragile"
    if "fallback" in method or "single_full" in method:
        return "fragile"
    if sample_full < 4:
        return "fragile"
    if sample_full < 8 or sample_limited == 0:
        return "medium"
    return "stable"


def derive_family_status(alert):
    analytics = alert.get("analytics") or {}
    return str(analytics.get("projectionFamilyStatus") or "watch_only").lower()


def get_family_policy_line():
    return ", ".join(f"{family}={status}" for family, status in PROJECTION_FAMILY_POLICY.items())


def first_projection_cutoff(records):
    dates = []
    for record in records:
        alert = record.get("alert") or {}
        if not has_projection(alert):
            continue
        graded_at = parse_dt(record.get("gradedAt"))
        if graded_at is not None:
            dates.append(graded_at)
    if not dates:
        return None
    return min(dates)


def select_records(records, args, deploy_cutoff):
    selected = []
    filter_counts = Counter()
    now_utc = datetime.now(UTC)
    rolling_cutoff = None
    start_cutoff = None
    use_post_deploy = True

    if args.include_predeploy:
        use_post_deploy = False
    elif args.post_deploy_only:
        use_post_deploy = True

    if args.game_date:
        game_date = str(args.game_date)
    else:
        game_date = None
        if args.start_date:
            start_cutoff = parse_date(args.start_date)
        elif args.days is not None:
            rolling_cutoff = now_utc - timedelta(days=args.days)

    for record in records:
        alert = record.get("alert") or {}
        graded_at = parse_dt(record.get("gradedAt"))
        game_date_value = record.get("gameDate")

        if game_date:
            if game_date_value != game_date:
                filter_counts["game_date"] += 1
                continue
        else:
            if graded_at is None:
                filter_counts["missing_graded_at"] += 1
                continue
            if start_cutoff is not None and graded_at < start_cutoff:
                filter_counts["start_date"] += 1
                continue
            if rolling_cutoff is not None and graded_at < rolling_cutoff:
                filter_counts["lookback_window"] += 1
                continue

        if deploy_cutoff is not None and use_post_deploy:
            if graded_at is not None and graded_at < deploy_cutoff:
                filter_counts["predeploy"] += 1
                continue

        selected.append(record)

    return selected, filter_counts


def dedupe_player_game(records):
    return dedupe_observations(records, player_game=True)


def is_legacy_fantasy(record):
    alert = record.get("alert") or {}
    return alert.get("statType") in ("Fantasy Score", "Fantasy Points") and (
        record.get("fantasyScoringVersion") != 2 or (alert.get("analytics") or {}).get("fantasyScoringVersion") != 2
    )


def stable_representatives(records):
    return [record for record in records if has_projection(record.get("alert") or {})
            and has_confidence(record.get("alert") or {})
            and derive_confidence_band(record.get("alert") or {}) == "stable"]


def summarize_records(records, include_legacy_fantasy=False):
    summary = make_summary()

    for record in records:
        result = record.get("result")
        alert = record.get("alert") or {}
        analytics = alert.get("analytics") or {}
        stat_type = alert.get("statType") or "unknown"
        tier = alert.get("tier") or "unknown"
        side = get_posted_side(alert)
        has_projection_fields = has_projection(alert)
        has_confidence_fields = has_confidence(alert)
        prob = choose_probability(alert)

        summary["gradedCount"] += 1
        if result in ("win", "loss", "push", "void", "unresolved"):
            summary[result] += 1
        else:
            summary["unresolved"] += 1

        if not has_projection_fields:
            summary["missingProjection"] += 1
        else:
            summary["recordsWithProjection"] += 1

        if not has_confidence_fields:
            summary["missingConfidence"] += 1
        else:
            summary["recordsWithConfidence"] += 1

        if result in ("void", "unresolved"):
            summary["voidUngraded"] += 1

        if result not in ("win", "loss"):
            continue

        if is_legacy_fantasy(record) and not include_legacy_fantasy:
            summary["legacyFantasyExcluded"] += 1
            continue

        if not has_projection_fields:
            continue
        if not has_confidence_fields:
            continue

        if prob is None or prob < 0 or prob > 1:
            summary["invalidProbability"] += 1
            continue

        outcome = 1 if result == "win" else 0
        summary["countable"] += 1
        summary["eligibleWins"] += outcome
        summary["eligibleLosses"] += 1 - outcome
        summary["brier"] += (prob - outcome) ** 2

        update_bucket(summary["byBucket"][bucket_label(prob)], prob, outcome)
        if side in summary["bySide"]:
            update_bucket(summary["bySide"][side], prob, outcome)
        if tier in summary["byTier"]:
            update_bucket(summary["byTier"][tier], prob, outcome)
        update_bucket(summary["byStatType"][stat_type], prob, outcome)
        update_bucket(summary["byFamilyStatus"][derive_family_status(alert)], prob, outcome)

        confidence_band = derive_confidence_band(alert)
        update_bucket(summary["byConfidenceBand"][confidence_band], prob, outcome)

        if analytics.get("projectionLowConfidence"):
            summary["storedLowConfidence"] += 1

        projection_side = get_projection_side(analytics)
        if projection_side == "tie":
            update_bucket(summary["byProjectionAgreement"]["tie"], prob, outcome)
        elif projection_side is not None and side in ("over", "under"):
            if projection_side == side:
                update_bucket(summary["byProjectionAgreement"]["agree"], prob, outcome)
            else:
                update_bucket(summary["byProjectionAgreement"]["disagree"], prob, outcome)

        if NEAR_THRESHOLD_MIN <= prob < NEAR_THRESHOLD_MAX:
            update_bucket(summary["nearThreshold"], prob, outcome)

        method = analytics.get("projectionMethod")
        if method:
            summary["methodCounts"][method] += 1

        for reason in analytics.get("projectionConfidenceReasons") or []:
            summary["confidenceReasonCounts"][str(reason)] += 1

    if summary["countable"] > 0:
        summary["winRate"] = round((summary["eligibleWins"] / summary["countable"]) * 100, 1)
        summary["brier"] = round(summary["brier"] / summary["countable"], 4)

    for bucket in summary["byBucket"].values():
        finalize_bucket(bucket)
    for bucket in summary["bySide"].values():
        finalize_bucket(bucket)
    for bucket in summary["byTier"].values():
        finalize_bucket(bucket)
    for bucket in summary["byConfidenceBand"].values():
        finalize_bucket(bucket)
    for bucket in summary["byProjectionAgreement"].values():
        finalize_bucket(bucket)
    finalize_bucket(summary["nearThreshold"])
    for bucket in summary["byStatType"].values():
        finalize_bucket(bucket)
    for bucket in summary["byFamilyStatus"].values():
        finalize_bucket(bucket)

    summary["byStatType"] = dict(
        sorted(summary["byStatType"].items(), key=lambda item: (-item[1]["count"], item[0]))
    )
    summary["byFamilyStatus"] = dict(
        sorted(summary["byFamilyStatus"].items(), key=lambda item: (-item[1]["count"], item[0]))
    )
    summary["methodCounts"] = dict(summary["methodCounts"].most_common())
    summary["confidenceReasonCounts"] = dict(summary["confidenceReasonCounts"].most_common())
    summary["recordsWithoutProjection"] = summary["missingProjection"]
    raw_count = summary["win"] + summary["loss"]
    summary["rawWinRate"] = round(summary["win"] / raw_count * 100, 1) if raw_count else 0.0
    return summary


def calibration_rows(bucket_map, min_count):
    rows = []
    for label, bucket in bucket_map.items():
        if bucket["count"] < min_count:
            continue
        rows.append((label, bucket))
    rows.sort(key=lambda item: (abs(item[1]["gap"]), item[1]["count"]), reverse=True)
    return rows


def build_family_tables(summary, min_family_count):
    family_tables = {}
    eligible_families = []
    skipped_families = {}
    for name, bucket in summary["byStatType"].items():
        count = bucket.get("count", 0)
        wins = bucket.get("wins", 0)
        losses = bucket.get("losses", 0)
        win_rate = bucket.get("winRate", 0.0)
        avg_prob = bucket.get("avgProb", 0.0)
        gap = bucket.get("gap", 0.0)
        brier = bucket.get("brier", 0.0)
        eligible = count >= min_family_count
        family_tables[name] = {
            "eligible": eligible,
            "minCount": min_family_count,
            "count": count,
            "wins": wins,
            "losses": losses,
            "winRate": win_rate,
            "avgProb": avg_prob,
            "gap": gap,
            "brier": brier,
            "skipReason": None if eligible else "below_min_stable_count",
        }
        if eligible:
            eligible_families.append(name)
        else:
            skipped_families[name] = {
                "reason": "below_min_stable_count",
                "count": count,
            }
    return family_tables, eligible_families, skipped_families


def build_side_tables(summary, min_side_count):
    side_tables = {}
    for side in ("over", "under"):
        bucket = summary["bySide"][side]
        count = bucket.get("count", 0)
        wins = bucket.get("wins", 0)
        losses = bucket.get("losses", 0)
        win_rate = bucket.get("winRate", 0.0)
        avg_prob = bucket.get("avgProb", 0.0)
        gap = bucket.get("gap", 0.0)
        brier = bucket.get("brier", 0.0)
        side_tables[side] = {
            "eligible": count >= min_side_count,
            "minCount": min_side_count,
            "count": count,
            "wins": wins,
            "losses": losses,
            "winRate": win_rate,
            "avgProb": avg_prob,
            "gap": gap,
            "brier": brier,
            "skipReason": None if count >= min_side_count else "below_min_stable_count",
        }
    return side_tables


def build_calibration_artifact(summary, deduped_summary, filter_counts, deploy_cutoff, args):
    side_tables = build_side_tables(summary, args.calibration_min_side_count)
    family_tables, eligible_families, skipped_families = build_family_tables(
        summary, args.calibration_min_family_count
    )
    deduped_side_tables = build_side_tables(deduped_summary, args.calibration_min_side_count)
    deduped_family_tables, deduped_eligible_families, deduped_skipped_families = build_family_tables(
        deduped_summary, args.calibration_min_family_count
    )

    return {
        "metadata": {
            "generatedAt": datetime.now(UTC).isoformat(),
            "postDeployCutoff": deploy_cutoff.isoformat() if deploy_cutoff else None,
            "sourceFilters": {
                "days": args.days,
                "startDate": args.start_date,
                "gameDate": args.game_date,
                "postDeployOnly": not args.include_predeploy,
                "includePredeploy": args.include_predeploy,
            },
            "fitSource": "dedupedStableFit",
        },
        "thresholds": {
            "sideMinCount": args.calibration_min_side_count,
            "familyMinCount": args.calibration_min_family_count,
        },
        "filters": dict(filter_counts),
        "fitSampleCounts": {
            "propLevelStableSelected": summary["gradedCount"],
            "propLevelStableCountable": summary["countable"],
            "dedupedStableSelected": deduped_summary["gradedCount"],
            "dedupedStableCountable": deduped_summary["countable"],
            "dedupedSideFitCount": deduped_summary["countable"],
            "dedupedFamilyFitCount": deduped_summary["countable"],
        },
        "stableFit": {
            "selectedRecords": summary["gradedCount"],
            "countable": summary["countable"],
            "winRate": summary["winRate"],
            "brier": summary["brier"],
            "bucketTables": summary["byBucket"],
            "sideTables": side_tables,
            "familyTables": family_tables,
            "eligibleFamilies": eligible_families,
            "skippedFamilies": skipped_families,
            "familyStatusCounts": summary["byFamilyStatus"],
            "confidenceBandCounts": summary["byConfidenceBand"],
            "confidenceReasonCounts": summary["confidenceReasonCounts"],
            "methodCounts": summary["methodCounts"],
        },
        "dedupedStableFit": {
            "selectedRecords": deduped_summary["gradedCount"],
            "countable": deduped_summary["countable"],
            "winRate": deduped_summary["winRate"],
            "brier": deduped_summary["brier"],
            "bucketTables": deduped_summary["byBucket"],
            "sideTables": deduped_side_tables,
            "familyTables": deduped_family_tables,
            "eligibleFamilies": deduped_eligible_families,
            "skippedFamilies": deduped_skipped_families,
            "familyStatusCounts": deduped_summary["byFamilyStatus"],
            "confidenceBandCounts": deduped_summary["byConfidenceBand"],
            "confidenceReasonCounts": deduped_summary["confidenceReasonCounts"],
            "methodCounts": deduped_summary["methodCounts"],
        },
        "skippedFamilies": deduped_skipped_families,
    }


def write_calibration_artifact(path, artifact):
    if Path(path).suffix.lower() != ".json":
        raise ValueError("Report artifacts require a .json path; source JSONL files are protected")
    sources = artifact.get("metadata", {}).get("sources", [])
    if Path(path).resolve() in {Path(source["path"]).resolve() for source in sources}:
        raise ValueError("Report output must not overwrite source history")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, allow_nan=False)


def format_bucket_line(label, bucket):
    return (
        f"  {label}: W {bucket['wins']} | L {bucket['losses']} | "
        f"Win {bucket['winRate']}% | Avg p {bucket['avgProb']}% | "
        f"Gap {bucket['gap']:+.1f} | Brier {bucket['brier']}"
    )


def print_summary(
    title,
    summary,
    args,
    filter_counts,
    deploy_cutoff,
    note=None,
    stat_min_count=None,
):
    stat_min_count = args.min_count if stat_min_count is None else stat_min_count
    print(title)
    if note:
        print(note)
    print(f"Selected / graded rows: {summary['gradedCount']}")
    print(f"Eligible projection results: W {summary['eligibleWins']} | L {summary['eligibleLosses']} | Legacy fantasy excluded: {summary['legacyFantasyExcluded']}")
    print(
        f"Countable plays: {summary['countable']} | "
        f"Win rate: {summary['winRate']}% | Brier: {summary['brier']}"
    )
    print(
        f"Result mix: W {summary['win']} | L {summary['loss']} | P {summary['push']} | "
        f"V {summary['void']} | U {summary['unresolved']}"
    )
    print(
        f"Projection coverage: {summary['recordsWithProjection']} with projection | "
        f"{summary['recordsWithConfidence']} with confidence | "
        f"{summary['missingProjection']} missing projection | "
        f"{summary['missingConfidence']} missing confidence | "
        f"{summary['invalidProbability']} invalid probability | "
        f"Stored low confidence: {summary['storedLowConfidence']}"
    )
    print(
        f"Skipped rows: missing_projection={summary['missingProjection']}, "
        f"missing_confidence={summary['missingConfidence']}, "
        f"invalid_probability={summary['invalidProbability']}, "
        f"void_ungraded={summary['voidUngraded']}"
    )

    if deploy_cutoff is not None:
        print(f"Post-deploy cutoff: {deploy_cutoff.isoformat()}")

    if filter_counts:
        filter_bits = ", ".join(f"{key}={value}" for key, value in sorted(filter_counts.items()))
        print(f"Filters applied: {filter_bits}")

    print("")
    print("Calibration buckets:")
    for label in BUCKET_ORDER:
        print(format_bucket_line(label, summary["byBucket"][label]))

    print("")
    print("Best / worst gaps:")
    rows = calibration_rows(summary["byBucket"], args.min_count)
    if rows:
        worst = rows[:3]
        best = sorted(rows, key=lambda item: (item[1]["gap"], item[1]["count"]), reverse=True)[:3]
        print("  Worst calibration gaps:")
        for label, bucket in worst:
            print(f"    {label}: gap {bucket['gap']:+.1f} on {bucket['count']} plays")
        print("  Strongest positive gaps:")
        for label, bucket in best:
            print(f"    {label}: gap {bucket['gap']:+.1f} on {bucket['count']} plays")
    else:
        print("  No bucket has enough volume for calibration highlighting.")

    print("")
    print("By side:")
    for side in ("over", "under"):
        print(format_bucket_line(side.title(), summary["bySide"][side]))

    print("")
    print("By tier:")
    for tier in ("best_bet", "watchlist"):
        print(format_bucket_line(tier, summary["byTier"][tier]))

    print("")
    print("By confidence band:")
    for band in STABLE_BANDS:
        print(format_bucket_line(band, summary["byConfidenceBand"][band]))

    print("")
    print("Projection agreement:")
    for label in ("agree", "disagree", "tie"):
        print(format_bucket_line(label, summary["byProjectionAgreement"][label]))

    print("")
    print(
        f"Near-threshold plays ({int(NEAR_THRESHOLD_MIN * 100)}-{int(NEAR_THRESHOLD_MAX * 100)}%):"
    )
    print(format_bucket_line("near-threshold", summary["nearThreshold"]))

    print("")
    print("Confidence audit:")
    print(f"  Stored low confidence total: {summary['storedLowConfidence']}")
    if summary["confidenceReasonCounts"]:
        for reason, count in summary["confidenceReasonCounts"].items():
            print(f"  Reason {reason}: {count}")
    else:
        print("  No stored confidence reasons recorded.")

    print("")
    print("Family status audit:")
    print(f"  Family policy: {get_family_policy_line()}")
    if summary["byFamilyStatus"]:
        for status, bucket in summary["byFamilyStatus"].items():
            print(format_bucket_line(status, bucket))
    else:
        print("  No family status records.")

    print("")
    print("Projection method audit:")
    if summary["methodCounts"]:
        for method, count in summary["methodCounts"].items():
            print(f"  {method}: {count}")
    else:
        print("  No projection methods recorded.")

    print("")
    print("By stat type:")
    stat_rows = [
        (name, bucket)
        for name, bucket in summary["byStatType"].items()
        if bucket["count"] >= stat_min_count
    ]
    if not stat_rows:
        print("  No stat types met the minimum count threshold.")
    else:
        for name, bucket in stat_rows:
            print(format_bucket_line(name, bucket))

    print("")


def main():
    args = parse_args()
    graded, history_metadata = read_report_history(args)
    if not graded:
        print("No graded props found.")
        return

    deploy_cutoff = None if args.include_predeploy else first_projection_cutoff(graded)
    selected, filter_counts = select_records(graded, args, deploy_cutoff)
    if not selected:
        print("No records matched the selected filters.")
        return

    summarize = lambda rows: summarize_records(rows, args.include_legacy_fantasy)
    prop_level = summarize(selected)
    stable_prop_level = summarize(stable_representatives(selected))
    representatives = dedupe_player_game(selected)
    stable_deduped = summarize(stable_representatives(representatives))
    deduped = summarize(representatives)
    unique_lines = summarize(dedupe_observations(selected))

    window_desc = []
    if args.game_date:
        window_desc.append(f"game-date={args.game_date}")
    elif args.start_date:
        window_desc.append(f"start-date={args.start_date}")
    elif args.days is not None:
        window_desc.append(f"last {args.days} days")
    if not args.include_predeploy:
        window_desc.append("post-deploy only")
    else:
        window_desc.append("including pre-deploy")

    print(f"Projection report ({', '.join(window_desc)})")
    print("")
    print_summary("Latest-alert view", prop_level, args, filter_counts, deploy_cutoff,
                  note="Historical box-score labels; platform settlement unverified. Different stats/games can remain correlated.")
    print("")
    print_summary(
        "Calibration fit (stable post-deploy rows only)",
        stable_prop_level,
        args,
        filter_counts,
        deploy_cutoff,
        note=(
            f"Calibration gates: side >= {args.calibration_min_side_count} stable rows; "
            f"family >= {args.calibration_min_family_count} stable rows; "
            f"fit source = deduped stable rows"
        ),
        stat_min_count=args.calibration_min_family_count,
    )
    print_summary("Player-game deduped view", deduped, args, filter_counts, deploy_cutoff)
    print_summary("Unique-line view", unique_lines, args, filter_counts, deploy_cutoff)

    artifact = build_calibration_artifact(
        stable_prop_level,
        stable_deduped,
        filter_counts,
        deploy_cutoff,
        args,
    )
    artifact["metadata"].update(history_metadata)
    artifact["metadata"].update({"includeLegacyFantasy": args.include_legacy_fantasy,
        "settlementBasis": "historical/internal box-score evaluations; platform settlement unverified",
        "legacyGradingAlerts": sum(record.get("gradingVersion") != 2 for record in selected),
        "probabilitySemantics": "existing estimator; void/push probability redesign pending step 5"})
    artifact["populationSummaries"] = {"latestAlert": prop_level, "uniqueLine": unique_lines, "playerGameStat": deduped}
    write_calibration_artifact(args.output_artifact, artifact)
    print(f"Calibration artifact written to {args.output_artifact}")


if __name__ == "__main__":
    main()
