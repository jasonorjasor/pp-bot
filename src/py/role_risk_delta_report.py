import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_ACTIVE_DIR = BASE_DIR / "data" / "active"
REPORTS_DIR = BASE_DIR / "reports"
POSTED_PROPS_FILE = str(DATA_ACTIVE_DIR / "postedProps.jsonl")
REPORT_FILE = str(REPORTS_DIR / "roleRiskDeltaReport.json")


MIN_SCORE_FOR_PICK = 5.5
WATCHLIST_SCORE = 6.5
BEST_BET_SCORE = 8.0
ROLE_UNDER_MULTIPLIER = 0.35
DEFAULT_DAYS = 3


STAT_FAMILY_MAP = {
    "points": "points",
    "assists": "assists",
    "rebounds": "rebounds",
    "offensive rebounds": "rebounds",
    "defensive rebounds": "rebounds",
    "turnovers": "turnovers",
    "steals": "stocks",
    "blocks": "stocks",
    "blocked attempts": "stocks",
    "blks+stls": "stocks",
    "fantasy score": "fantasy",
    "fantasy points": "fantasy",
    "pts+rebs": "points",
    "pts+asts": "assists",
    "rebs+asts": "rebounds",
    "pts+rebs+asts": "points",
    "field goals made": "fg_made",
    "fg made": "fg_made",
    "field goals attempted": "fg_att",
    "fg attempted": "fg_att",
    "ft made": "ft_made",
    "free throws made": "ft_made",
    "free throws": "ft_made",
    "ft attempted": "ft_att",
    "free throws attempted": "ft_att",
    "two pointers made": "two_pm",
    "2-pointers made": "two_pm",
    "2 pointers made": "two_pm",
    "two pointers attempted": "two_pa",
    "2-pointers attempted": "two_pa",
    "2 pointers attempted": "two_pa",
    "3-pt made": "three_pm",
    "3-pointers made": "three_pm",
    "3 pointers made": "three_pm",
    "3-pt attempted": "three_pa",
    "3-pointers attempted": "three_pa",
    "3 pointers attempted": "three_pa",
}


def parse_days(argv):
    if "--days" in argv:
        idx = argv.index("--days")
        if idx + 1 < len(argv):
            try:
                return int(argv[idx + 1])
            except ValueError:
                return DEFAULT_DAYS
    return DEFAULT_DAYS


def parse_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_tier(score):
    if score >= BEST_BET_SCORE:
        return "best_bet"
    if score >= WATCHLIST_SCORE:
        return "watchlist"
    if score >= MIN_SCORE_FOR_PICK:
        return "watchlist"
    return "skip"


def stat_family(stat_type):
    if not stat_type:
        return "unknown"
    key = str(stat_type).strip().lower()
    return STAT_FAMILY_MAP.get(key, "unknown")


def role_archetype(minutes_counts, dnp_rate, limited_rate):
    if not minutes_counts:
        return "unknown"
    dnp_count = minutes_counts.get("dnp", 0)
    limited_count = minutes_counts.get("limited", 0)
    full_count = minutes_counts.get("full", 0)
    if dnp_count >= 2 or dnp_rate >= 20.0:
        return "dnp_risk"
    if limited_count >= 3 or limited_rate >= 25.0:
        return "limited_risk"
    if full_count >= 8:
        return "stable_full"
    return "mixed_role"


def summarize_group(records):
    total = len(records)
    changed = sum(1 for r in records if r["decisionChanged"])
    tier_changed = sum(1 for r in records if r["tierChanged"])
    avg_penalty = round(
        sum(r["roleRiskPenalty"] for r in records) / total, 2
    ) if total else 0.0
    avg_score_delta = round(
        sum(r["scoreDelta"] for r in records) / total, 2
    ) if total else 0.0
    return {
        "total": total,
        "decisionChanged": changed,
        "tierChanged": tier_changed,
        "avgRoleRiskPenalty": avg_penalty,
        "avgScoreDelta": avg_score_delta,
    }


def main():
    days = parse_days(sys.argv)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    records = []
    with open(POSTED_PROPS_FILE, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            posted_at = parse_datetime(record.get("postedAt"))
            if not posted_at or posted_at < window_start:
                continue

            analytics = record.get("analytics") or {}
            over_score = analytics.get("overScore")
            under_score = analytics.get("underScore")
            role_risk_penalty = float(analytics.get("roleRiskPenalty", 0.0) or 0.0)
            dnp_rate = float(analytics.get("dnpRateWeighted", 0.0) or 0.0)
            limited_rate = float(analytics.get("limitedRateWeighted", 0.0) or 0.0)
            minutes_counts = analytics.get("minutesRegimeCounts") or {}
            context_delta = analytics.get("contextScoreDelta")
            recommended_side = record.get("recommendedSide") or analytics.get("recommendedSide")
            current_tier = record.get("tier") or analytics.get("tierCandidate")
            current_score = record.get("score") or analytics.get("finalScore") or 0.0

            has_role_fields = (
                analytics.get("roleRiskPenalty") is not None
                and analytics.get("minutesRegimeCounts") is not None
            )

            if isinstance(current_score, str):
                try:
                    current_score = float(current_score)
                except ValueError:
                    current_score = 0.0

            context_bias = 0.0
            if context_delta is not None and recommended_side in ("over", "under"):
                try:
                    context_delta = float(context_delta)
                    context_bias = context_delta if recommended_side == "over" else -context_delta
                except (TypeError, ValueError):
                    context_bias = 0.0

            pre_side = None
            pre_score = None
            pre_tier = None
            score_delta = 0.0
            decision_changed = False
            tier_changed = False

            if over_score is not None and under_score is not None:
                try:
                    over_score = float(over_score)
                    under_score = float(under_score)
                except (TypeError, ValueError):
                    over_score = None
                    under_score = None

            if current_tier is None:
                current_tier = classify_tier(float(current_score) if current_score else 0.0)

            if has_role_fields and over_score is not None and under_score is not None:
                over_pre = over_score + role_risk_penalty
                under_pre = under_score + (role_risk_penalty * ROLE_UNDER_MULTIPLIER)
                over_final_pre = over_pre + context_bias
                under_final_pre = under_pre - context_bias
                pre_side = "over" if over_final_pre >= under_final_pre else "under"
                pre_score = max(over_final_pre, under_final_pre)
                pre_tier = classify_tier(pre_score)
                score_delta = round(pre_score - float(current_score), 2)
                decision_changed = pre_side != recommended_side
                tier_changed = pre_tier != current_tier

            records.append({
                "postedAt": record.get("postedAt"),
                "playerName": record.get("playerName"),
                "statType": record.get("statType"),
                "statFamily": stat_family(record.get("statType")),
                "line": record.get("line"),
                "recommendedSide": recommended_side,
                "tier": current_tier,
                "score": current_score,
                "preSide": pre_side,
                "preTier": pre_tier,
                "preScore": pre_score,
                "scoreDelta": score_delta,
                "decisionChanged": decision_changed,
                "tierChanged": tier_changed,
                "roleRiskPenalty": role_risk_penalty,
                "dnpRateWeighted": dnp_rate,
                "limitedRateWeighted": limited_rate,
                "minutesRegimeCounts": minutes_counts,
                "roleArchetype": role_archetype(minutes_counts, dnp_rate, limited_rate),
                "hasRoleFields": has_role_fields,
            })

    # Coverage
    total = len(records)
    with_role = sum(1 for r in records if r["hasRoleFields"])
    missing_role = total - with_role

    # Group summaries
    by_side = defaultdict(list)
    by_family = defaultdict(list)
    by_archetype = defaultdict(list)
    by_changed = defaultdict(list)

    for rec in records:
        by_side[rec["recommendedSide"]].append(rec)
        by_family[rec["statFamily"]].append(rec)
        by_archetype[rec["roleArchetype"]].append(rec)
        key = "changed" if rec["decisionChanged"] else "unchanged"
        by_changed[key].append(rec)

    summary = summarize_group(records)
    side_summary = {k: summarize_group(v) for k, v in by_side.items()}
    family_summary = {k: summarize_group(v) for k, v in by_family.items()}
    archetype_summary = {k: summarize_group(v) for k, v in by_archetype.items()}
    changed_summary = {k: summarize_group(v) for k, v in by_changed.items()}

    # Highest penalties
    highest_penalties = sorted(
        records, key=lambda r: r["roleRiskPenalty"], reverse=True
    )[:25]

    # Near-threshold cases
    near_threshold = [
        r for r in records if abs((r["score"] or 0.0) - MIN_SCORE_FOR_PICK) <= 0.25
    ]

    # Changed decisions list (limit 50)
    changed_decisions = [r for r in records if r["decisionChanged"]][:50]

    output = {
        "generatedAt": now.isoformat(),
        "window": {
            "days": days,
            "from": window_start.isoformat(),
            "to": now.isoformat(),
            "timezone": "UTC",
        },
        "coverage": {
            "totalProps": total,
            "withRoleFields": with_role,
            "missingRoleFields": missing_role,
        },
        "summary": summary,
        "bySide": side_summary,
        "byStatFamily": family_summary,
        "byRoleArchetype": archetype_summary,
        "byChangedDecision": changed_summary,
        "changedDecisions": changed_decisions,
        "highestPenalties": highest_penalties,
        "nearThreshold": near_threshold[:50],
    }

    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)

    print(json.dumps({
        "windowDays": days,
        "totalProps": total,
        "withRoleFields": with_role,
        "missingRoleFields": missing_role,
        "decisionChanged": summary["decisionChanged"],
        "tierChanged": summary["tierChanged"],
        "reportFile": REPORT_FILE,
    }, indent=2))


if __name__ == "__main__":
    main()
