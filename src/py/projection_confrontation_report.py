"""
src/py/projection_confrontation_report.py

Compare posted-side outcomes against projection-preferred outcomes.
Usage:
    python src/py/projection_confrontation_report.py [--days N] [--start-date YYYY-MM-DD]
        [--game-date YYYY-MM-DD] [--include-predeploy]
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from projection_report import (
    BUCKET_ORDER,
    CALIBRATION_ARTIFACT_FILE,
    DEFAULT_MIN_COUNT,
    GRADED_PROPS_FILE,
    derive_confidence_band,
    derive_family_status,
    dedupe_player_game,
    empty_bucket,
    finalize_bucket,
    first_projection_cutoff,
    format_bucket_line,
    get_family_policy_line,
    get_posted_side,
    get_projection_side,
    has_confidence,
    has_projection,
    choose_probability,
    load_jsonl,
    parse_dt,
    parse_date,
    safe_float,
    select_records,
)


DEFAULT_CONFRONTATION_MIN_COUNT = DEFAULT_MIN_COUNT


def parse_args():
    parser = argparse.ArgumentParser()
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
        default=DEFAULT_CONFRONTATION_MIN_COUNT,
        help="Minimum count for family highlighting.",
    )
    return parser.parse_args()


def empty_confrontation_bucket():
    return {
        "count": 0,
        "postedWins": 0,
        "postedLosses": 0,
        "postedCountable": 0,
        "projectionWins": 0,
        "projectionLosses": 0,
        "projectionCountable": 0,
        "ties": 0,
    }


def update_confrontation_bucket(bucket, posted_result, projection_result):
    bucket["count"] += 1
    if posted_result == "win":
        bucket["postedWins"] += 1
        bucket["postedCountable"] += 1
    elif posted_result == "loss":
        bucket["postedLosses"] += 1
        bucket["postedCountable"] += 1

    if projection_result == "win":
        bucket["projectionWins"] += 1
        bucket["projectionCountable"] += 1
    elif projection_result == "loss":
        bucket["projectionLosses"] += 1
        bucket["projectionCountable"] += 1
    elif projection_result == "tie":
        bucket["ties"] += 1


def finalize_confrontation_bucket(bucket):
    if bucket["postedCountable"] > 0:
        bucket["postedWinRate"] = round((bucket["postedWins"] / bucket["postedCountable"]) * 100, 1)
    else:
        bucket["postedWinRate"] = 0.0

    if bucket["projectionCountable"] > 0:
        bucket["projectionWinRate"] = round((bucket["projectionWins"] / bucket["projectionCountable"]) * 100, 1)
        bucket["winRateGap"] = round(bucket["projectionWinRate"] - bucket["postedWinRate"], 1)
    else:
        bucket["projectionWinRate"] = None
        bucket["winRateGap"] = None


def make_summary():
    return {
        "gradedCount": 0,
        "win": 0,
        "loss": 0,
        "push": 0,
        "void": 0,
        "unresolved": 0,
        "countable": 0,
        "recordsWithProjection": 0,
        "recordsWithConfidence": 0,
        "missingProjection": 0,
        "missingConfidence": 0,
        "invalidProbability": 0,
        "voidUngraded": 0,
        "projectionPreferredCountable": 0,
        "projectionPreferredWins": 0,
        "projectionPreferredLosses": 0,
        "projectionPreferredTies": 0,
        "agreementCounts": Counter(),
        "byAgreement": {
            "agree": empty_confrontation_bucket(),
            "disagree": empty_confrontation_bucket(),
            "tie": empty_confrontation_bucket(),
        },
        "bySide": {"over": empty_confrontation_bucket(), "under": empty_confrontation_bucket()},
        "byFamily": defaultdict(empty_confrontation_bucket),
        "byConfidenceBand": {"stable": empty_confrontation_bucket(), "medium": empty_confrontation_bucket(), "fragile": empty_confrontation_bucket()},
        "familyStatusCounts": defaultdict(int),
        "projectionSideCounts": Counter(),
    }


def projection_result_for_side(side, final_value, line):
    if side not in ("over", "under"):
        return "tie"
    if final_value is None or line is None:
        return "tie"
    if side == "over":
        if final_value > line:
            return "win"
        if final_value == line:
            return "push"
        return "loss"
    if final_value < line:
        return "win"
    if final_value == line:
        return "push"
    return "loss"


def summarize_records(records):
    summary = make_summary()

    for record in records:
        alert = record.get("alert") or {}
        analytics = alert.get("analytics") or {}
        result = record.get("result")
        side = get_posted_side(alert)
        projection_side = get_projection_side(analytics)
        final_value = safe_float(record.get("finalValue"))
        line = safe_float(alert.get("line"))
        has_proj = has_projection(alert)
        has_conf = has_confidence(alert)
        prob = choose_probability(alert)

        summary["gradedCount"] += 1
        if result in ("win", "loss", "push", "void", "unresolved"):
            summary[result] += 1
        else:
            summary["unresolved"] += 1

        if not has_proj:
            summary["missingProjection"] += 1
        else:
            summary["recordsWithProjection"] += 1

        if not has_conf:
            summary["missingConfidence"] += 1
        else:
            summary["recordsWithConfidence"] += 1

        if result in ("void", "unresolved"):
            summary["voidUngraded"] += 1

        if result not in ("win", "loss"):
            continue
        if not has_proj or not has_conf:
            continue

        if prob is None or prob < 0 or prob > 1:
            summary["invalidProbability"] += 1
            continue

        if side not in ("over", "under"):
            continue

        summary["countable"] += 1
        summary["projectionSideCounts"][projection_side if projection_side in ("over", "under", "tie") else "tie"] += 1

        if projection_side in ("over", "under"):
            projection_result = projection_result_for_side(projection_side, final_value, line)
        else:
            projection_result = "tie"

        if projection_result == "win":
            summary["projectionPreferredWins"] += 1
            summary["projectionPreferredCountable"] += 1
        elif projection_result == "loss":
            summary["projectionPreferredLosses"] += 1
            summary["projectionPreferredCountable"] += 1
        else:
            summary["projectionPreferredTies"] += 1

        if projection_side == side:
            agreement = "agree"
        elif projection_side == "tie":
            agreement = "tie"
        else:
            agreement = "disagree"

        update_confrontation_bucket(summary["byAgreement"][agreement], result, projection_result)

        update_confrontation_bucket(summary["bySide"][side], result, projection_result)

        family = alert.get("statType") or "unknown"
        update_confrontation_bucket(summary["byFamily"][family], result, projection_result)

        confidence_band = derive_confidence_band(alert)
        update_confrontation_bucket(summary["byConfidenceBand"][confidence_band], result, projection_result)

        summary["familyStatusCounts"][derive_family_status(alert)] += 1

    for bucket_map in (summary["byAgreement"], summary["bySide"], summary["byFamily"], summary["byConfidenceBand"]):
        for bucket in bucket_map.values():
            finalize_confrontation_bucket(bucket)

    summary["familyStatusCounts"] = dict(sorted(summary["familyStatusCounts"].items(), key=lambda item: (-item[1], item[0])))
    summary["projectionSideCounts"] = dict(summary["projectionSideCounts"])
    return summary


def family_policy_summary():
    return get_family_policy_line()


def print_bucket_block(title, bucket_map, min_count=None):
    print(title)
    items = bucket_map.items()
    if min_count is not None:
        items = [(name, bucket) for name, bucket in items if bucket["count"] >= min_count]
    items = sorted(items, key=lambda item: (-item[1]["count"], item[0]))
    if not items:
        print("  No rows met the minimum count threshold.")
        return
    for name, bucket in items:
        projection_rate = f"{bucket['projectionWinRate']}%" if bucket["projectionWinRate"] is not None else "n/a"
        gap = f"{bucket['winRateGap']:+.1f}" if bucket["winRateGap"] is not None else "n/a"
        print(
            f"  {name}: count {bucket['count']} | posted {bucket['postedWins']}-{bucket['postedLosses']} "
            f"({bucket['postedWinRate']}%) | projection {bucket['projectionWins']}-{bucket['projectionLosses']} "
            f"({projection_rate}) | gap {gap} | ties {bucket['ties']}"
        )


def print_summary(title, summary, filter_counts, deploy_cutoff, min_count=None):
    print(title)
    print(f"Selected / graded rows: {summary['gradedCount']}")
    print(
        f"Posted side: {summary['win']}-{summary['loss']} | "
        f"Projection preferred: {summary['projectionPreferredWins']}-{summary['projectionPreferredLosses']} | "
        f"Ties: {summary['projectionPreferredTies']}"
    )
    print(
        f"Coverage: {summary['recordsWithProjection']} with projection | "
        f"{summary['recordsWithConfidence']} with confidence | "
        f"{summary['missingProjection']} missing projection | "
        f"{summary['missingConfidence']} missing confidence | "
        f"{summary['invalidProbability']} invalid probability | "
        f"{summary['voidUngraded']} void/ungraded"
    )
    if deploy_cutoff is not None:
        print(f"Post-deploy cutoff: {deploy_cutoff.isoformat()}")
    if filter_counts:
        filter_bits = ", ".join(f"{key}={value}" for key, value in sorted(filter_counts.items()))
        print(f"Filters applied: {filter_bits}")
    print(f"Family policy: {family_policy_summary()}")
    print("")

    print("Overall confrontation summary:")
    posted_countable = summary["win"] + summary["loss"]
    projection_countable = summary["projectionPreferredCountable"]
    posted_rate = round((summary["win"] / posted_countable) * 100, 1) if posted_countable else 0.0
    projection_rate = round((summary["projectionPreferredWins"] / projection_countable) * 100, 1) if projection_countable else 0.0
    print(f"  Posted side win rate: {posted_rate}%")
    print(f"  Projection-preferred win rate: {projection_rate}%")
    print(f"  Difference: {round(projection_rate - posted_rate, 1):+.1f}")

    print("")
    print("Agreement split:")
    for label in ("agree", "disagree", "tie"):
        bucket = summary["byAgreement"][label]
        projection_rate = f"{bucket['projectionWinRate']}%" if bucket["projectionWinRate"] is not None else "n/a"
        gap = f"{bucket['winRateGap']:+.1f}" if bucket["winRateGap"] is not None else "n/a"
        print(
            f"  {label}: count {bucket['count']} | posted {bucket['postedWins']}-{bucket['postedLosses']} "
            f"({bucket['postedWinRate']}%) | projection {bucket['projectionWins']}-{bucket['projectionLosses']} "
            f"({projection_rate}) | gap {gap}"
        )

    print("")
    print("Side split:")
    for side in ("over", "under"):
        bucket = summary["bySide"][side]
        print(
            f"  {side}: count {bucket['count']} | posted {bucket['postedWins']}-{bucket['postedLosses']} "
            f"({bucket['postedWinRate']}%) | projection {bucket['projectionWins']}-{bucket['projectionLosses']} "
            f"({bucket['projectionWinRate']}%) | gap {bucket['winRateGap']:+.1f}"
        )

    print("")
    print("Family split:")
    family_items = [
        (name, bucket)
        for name, bucket in summary["byFamily"].items()
        if bucket["count"] >= (min_count or DEFAULT_CONFRONTATION_MIN_COUNT)
    ]
    if not family_items:
        print("  No families met the minimum count threshold.")
    else:
        for name, bucket in sorted(family_items, key=lambda item: (-item[1]["count"], item[0])):
            print(
                f"  {name}: count {bucket['count']} | posted {bucket['postedWins']}-{bucket['postedLosses']} "
                f"({bucket['postedWinRate']}%) | projection {bucket['projectionWins']}-{bucket['projectionLosses']} "
                f"({bucket['projectionWinRate']}%) | gap {bucket['winRateGap']:+.1f}"
            )

    print("")
    print("Confidence split:")
    for band in ("stable", "medium", "fragile"):
        bucket = summary["byConfidenceBand"][band]
        print(
            f"  {band}: count {bucket['count']} | posted {bucket['postedWins']}-{bucket['postedLosses']} "
            f"({bucket['postedWinRate']}%) | projection {bucket['projectionWins']}-{bucket['projectionLosses']} "
            f"({bucket['projectionWinRate']}%) | gap {bucket['winRateGap']:+.1f}"
        )

    print("")
    print("Family status audit:")
    if summary["familyStatusCounts"]:
        for status, count in summary["familyStatusCounts"].items():
            print(f"  {status}: {count}")
    else:
        print("  No family status records.")

    print("")
    print("Projection side counts:")
    if summary["projectionSideCounts"]:
        for side, count in summary["projectionSideCounts"].items():
            print(f"  {side}: {count}")
    else:
        print("  No projection side counts.")

    print("")


def main():
    args = parse_args()
    graded = load_jsonl(GRADED_PROPS_FILE)
    if not graded:
        print("No graded props found.")
        return

    deploy_cutoff = None if args.include_predeploy else first_projection_cutoff(graded)
    selected, filter_counts = select_records(graded, args, deploy_cutoff)
    if not selected:
        print("No records matched the selected filters.")
        return

    prop_level = summarize_records(selected)
    deduped = summarize_records(dedupe_player_game(selected))

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

    print(f"Projection confrontation report ({', '.join(window_desc)})")
    print("")
    print_summary("Prop-level confrontation", prop_level, filter_counts, deploy_cutoff, args.min_count)
    print("")
    print_summary("Player-game deduped confrontation", deduped, filter_counts, deploy_cutoff, args.min_count)
    print(f"Calibration artifact reference: {CALIBRATION_ARTIFACT_FILE}")


if __name__ == "__main__":
    main()
