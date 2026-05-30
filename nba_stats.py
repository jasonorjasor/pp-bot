"""
nba_stats.py

Fetches recent NBA player game logs, filters low-minute outliers, and returns
two-sided prop analytics for both the over and under.

Usage:
    python nba_stats.py "LeBron James" "Points" 25.5 "DAL" "2026-03-09T19:00:00.000-05:00"
"""

import sys
import json
import os

import numpy as np
from prop_utils import CURRENT_SEASON, STAT_MAP, compute_game_total, find_player, get_game_log
from team_context import (
    TEAM_CONTEXT_FILE,
    build_clean_game_metrics,
    build_context_summary,
    clamp,
    compute_context_for_prop,
    load_team_context_cache,
    parse_bool_env,
)

SAMPLE_SIZE = 30
HIT_RATE_GAMES = 10
LOW_MIN_THRESHOLD = 0.75
MIN_SAMPLE_SIZE_FOR_PICK = 6
MIN_SCORE_FOR_PICK = 5.5
RECENCY_WEIGHTS = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
CONTEXT_ENABLE_PACE = parse_bool_env(os.getenv("CONTEXT_ENABLE_PACE"), True)
CONTEXT_ENABLE_OPPONENT = parse_bool_env(os.getenv("CONTEXT_ENABLE_OPPONENT"), True)
CONTEXT_ENABLE_REST = parse_bool_env(os.getenv("CONTEXT_ENABLE_REST"), True)
CONTEXT_ENABLE_ROLE = parse_bool_env(os.getenv("CONTEXT_ENABLE_ROLE"), True)
PROJECTION_CONFIDENCE_STABLE_MIN_FULL = 8
PROJECTION_CONFIDENCE_MEDIUM_MIN_FULL = 4
PROJECTION_CONFIDENCE_STABLE_MAX_DISPERSION = 0.35
PROJECTION_CONFIDENCE_MEDIUM_MAX_DISPERSION = 0.65
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
    "FGM": "watch_only",
    "FGA": "watch_only",
    "Free Throws Made": "watch_only",
    "Free Throws Attempted": "watch_only",
    "2PM": "watch_only",
    "2PA": "watch_only",
}
PROJECTION_FAMILY_DEFAULT = "watch_only"


def get_projection_family_status(stat_type):
    return PROJECTION_FAMILY_POLICY.get(str(stat_type or "").strip(), PROJECTION_FAMILY_DEFAULT)


def is_home_game(matchup):
    return "vs." in matchup


def compute_rest_days(game_dates):
    rest = []
    for index in range(len(game_dates)):
        if index < len(game_dates) - 1:
            delta = (game_dates[index] - game_dates[index + 1]).days - 1
            rest.append(max(0, delta))
        else:
            rest.append(None)
    return rest


def compute_baseline_minutes(all_minutes, max_games=20):
    non_dnp = [mins for mins in all_minutes if mins >= 5]
    if not non_dnp:
        return 0.0
    sample = non_dnp[:max_games]
    return round(float(np.median(sample)), 1)


def classify_minutes_regime(min_played, baseline_minutes):
    if min_played < 5:
        return "dnp"
    if baseline_minutes > 0 and min_played < baseline_minutes * LOW_MIN_THRESHOLD:
        return "limited"
    return "full"


def compute_weighted_rate(flags, weights):
    if not flags:
        return 0.0
    use_weights = weights[: len(flags)]
    total = sum(use_weights)
    if total <= 0:
        return 0.0
    hit = sum(weight for weight, flag in zip(use_weights, flags) if flag)
    return round((hit / total) * 100, 1)


def weighted_mean(values, weights):
    if not values:
        return None
    use_weights = weights[: len(values)]
    total = sum(use_weights)
    if total <= 0:
        return None
    return float(
        sum(value * weight for value, weight in zip(values, use_weights)) / total
    )


def flag_reason(min_played, baseline_minutes):
    if min_played < 5:
        return "DNP"
    if baseline_minutes > 0 and min_played < baseline_minutes * LOW_MIN_THRESHOLD:
        shortfall = round(baseline_minutes - min_played, 1)
        return f"Low min ({round(min_played)}min, -{shortfall} vs baseline)"
    return None


def build_bar(pct):
    filled = round((pct / 100) * 10) if pct > 0 else 0
    filled = max(0, min(10, filled))
    return "█" * filled + "░" * (10 - filled)


def score_side(weighted_pct, edge_pct_abs, std_dev, mean_abs, sample_size):
    score = weighted_pct / 10.0
    score += min(2.0, edge_pct_abs / 12.0)

    if mean_abs > 0:
        cv = std_dev / mean_abs
        if cv > 0.25:
            score -= min(2.5, cv * 1.8)

    if sample_size < MIN_SAMPLE_SIZE_FOR_PICK:
        score -= 2.5
    elif sample_size < HIT_RATE_GAMES:
        score -= 0.8

    return round(max(1.0, min(10.0, score)), 1)


def classify_tier(score):
    if score >= 8.0:
        return "best_bet"
    if score >= 6.5:
        return "watchlist"
    return "skip"


def classify_projection_confidence(
    full_role_sample_size,
    projection_mean,
    projection_std,
    minutes_fallback_used,
    rate_fallback_used,
):
    reasons = []
    if full_role_sample_size < PROJECTION_CONFIDENCE_STABLE_MIN_FULL:
        reasons.append("small_sample")
    if minutes_fallback_used:
        reasons.append("minutes_fallback")
    if rate_fallback_used:
        reasons.append("rate_fallback")

    dispersion = None
    if projection_mean is not None and projection_std is not None and projection_mean > 0:
        dispersion = projection_std / projection_mean
        if dispersion > PROJECTION_CONFIDENCE_STABLE_MAX_DISPERSION:
            reasons.append("high_dispersion")
    else:
        reasons.append("high_dispersion")

    if (
        full_role_sample_size >= PROJECTION_CONFIDENCE_STABLE_MIN_FULL
        and not minutes_fallback_used
        and not rate_fallback_used
        and projection_mean is not None
        and projection_mean > 0
        and dispersion is not None
        and dispersion <= PROJECTION_CONFIDENCE_STABLE_MAX_DISPERSION
    ):
        return "stable", []

    if projection_mean is None or projection_mean <= 0:
        return "fragile", reasons

    if rate_fallback_used:
        return "fragile", reasons

    if full_role_sample_size < PROJECTION_CONFIDENCE_MEDIUM_MIN_FULL:
        return "fragile", reasons

    if dispersion is not None and dispersion > 0.45:
        return "fragile", reasons

    if (
        full_role_sample_size >= PROJECTION_CONFIDENCE_MEDIUM_MIN_FULL
        and full_role_sample_size < PROJECTION_CONFIDENCE_STABLE_MIN_FULL
    ) or minutes_fallback_used or (
        dispersion is not None
        and PROJECTION_CONFIDENCE_STABLE_MAX_DISPERSION < dispersion <= 0.45
    ):
        return "medium", reasons

    return "fragile", reasons


def build_reason_summary(
    recommended_side,
    side_score,
    weighted_pct,
    edge,
    sample_size,
    volatility,
    context_summary=None,
):
    lean = "Over" if recommended_side == "over" else "Under"
    edge_sign = "+" if edge >= 0 else ""
    summary = (
        f"{lean} rates better: score {side_score}/10, weighted hit rate {weighted_pct}%, "
        f"edge {edge_sign}{round(edge, 1)} vs line, sample {sample_size}, volatility {volatility.lower()}."
    )
    if context_summary:
        summary += f" Context: {context_summary}"
    return summary


def compute_analytics(df, stat_type, line, game_hint=None, start_time=None):
    stat_config = STAT_MAP.get(stat_type)
    if not stat_config:
        raise ValueError(
            f"Unknown stat type: '{stat_type}'. Add it to STAT_MAP in nba_stats.py."
        )

    game_dates = list(df["GAME_DATE_DT"])
    rest_days = compute_rest_days(game_dates)

    all_mins = [float(row["MIN_FLOAT"]) for _, row in df.iterrows()]
    valid_mins = [mins for mins in all_mins if mins >= 5]
    avg_minutes = round(float(np.mean(valid_mins)), 1) if valid_mins else 0.0
    baseline_minutes = compute_baseline_minutes(all_mins, max_games=20)

    all_game_details = []
    clean_totals = []
    clean_flags = []

    for index, (_, row) in enumerate(df.iterrows()):
        min_played = float(row["MIN_FLOAT"])
        minutes_regime = classify_minutes_regime(min_played, baseline_minutes)
        flag = flag_reason(min_played, baseline_minutes)
        home = is_home_game(str(row["MATCHUP"]))
        rest = rest_days[index]

        try:
            total = compute_game_total(row, stat_config)
        except KeyError as exc:
            raise ValueError(f"Missing column: {exc}")

        over = total > line
        under = total < line

        game_detail = {
            "date": row["GAME_DATE"],
            "matchup": row["MATCHUP"],
            "wl": row["WL"],
            "value": round(total, 1),
            "over": over,
            "under": under,
            "push": not over and not under,
            "home": home,
            "restDays": rest,
            "minutes": round(min_played, 1),
            "minutesRegime": minutes_regime,
            "flagged": flag is not None,
            "flagReason": flag,
        }
        all_game_details.append(game_detail)

        if flag is None:
            clean_totals.append(total)
            clean_flags.append(True)
        else:
            clean_flags.append(False)

    clean_for_hitrate = []
    for game in all_game_details:
        if not game["flagged"]:
            clean_for_hitrate.append(game)
        if len(clean_for_hitrate) == HIT_RATE_GAMES:
            break

    recent_games = all_game_details[:HIT_RATE_GAMES]
    dnp_flags = [game["minutesRegime"] == "dnp" for game in recent_games]
    limited_flags = [game["minutesRegime"] == "limited" for game in recent_games]
    dnp_rate_weighted = compute_weighted_rate(dnp_flags, RECENCY_WEIGHTS)
    limited_rate_weighted = compute_weighted_rate(limited_flags, RECENCY_WEIGHTS)
    minutes_regime_counts = {
        "full": sum(1 for game in recent_games if game["minutesRegime"] == "full"),
        "limited": sum(1 for game in recent_games if game["minutesRegime"] == "limited"),
        "dnp": sum(1 for game in recent_games if game["minutesRegime"] == "dnp"),
    }

    sample_size = len(clean_totals)
    hit_sample_size = len(clean_for_hitrate)
    flagged_count = sum(1 for game in all_game_details if game["flagged"])

    over_hits = sum(1 for game in clean_for_hitrate if game["over"])
    under_hits = sum(1 for game in clean_for_hitrate if game["under"])
    pushes = sum(1 for game in clean_for_hitrate if game["push"])

    over_hit_rate = round((over_hits / hit_sample_size) * 100) if hit_sample_size else 0
    under_hit_rate = round((under_hits / hit_sample_size) * 100) if hit_sample_size else 0

    weights = RECENCY_WEIGHTS[:hit_sample_size]
    total_weight = sum(weights)
    over_weighted_hits = sum(
        weights[index] for index, game in enumerate(clean_for_hitrate) if game["over"]
    )
    under_weighted_hits = sum(
        weights[index] for index, game in enumerate(clean_for_hitrate) if game["under"]
    )
    over_weighted_pct = (
        round((over_weighted_hits / total_weight) * 100) if total_weight > 0 else 0
    )
    under_weighted_pct = (
        round((under_weighted_hits / total_weight) * 100) if total_weight > 0 else 0
    )

    mean = round(float(np.mean(clean_totals)), 1) if sample_size else 0.0
    median = round(float(np.median(clean_totals)), 1) if sample_size else 0.0
    std_dev = round(float(np.std(clean_totals)), 1) if sample_size else 0.0
    mean_abs = abs(mean)
    cv = round(std_dev / mean_abs, 2) if mean_abs > 0 else 0.0

    if cv < 0.2:
        volatility = "Low"
    elif cv < 0.4:
        volatility = "Medium"
    else:
        volatility = "High"

    edge = round(mean - line, 1)
    edge_pct = round((edge / line) * 100, 1) if line > 0 else 0.0
    edge_pct_abs = abs(edge_pct)

    over_score = score_side(
        over_weighted_pct, edge_pct_abs, std_dev, mean_abs, hit_sample_size
    )
    under_score = score_side(
        under_weighted_pct, edge_pct_abs, std_dev, mean_abs, hit_sample_size
    )

    dnp_penalty = min(2.0, (dnp_rate_weighted / 100.0) * 5.0)
    limited_penalty = min(1.0, (limited_rate_weighted / 100.0) * 2.0)
    role_risk_penalty = round(dnp_penalty + limited_penalty, 2)
    over_score = round(max(1.0, over_score - role_risk_penalty), 1)
    under_score = round(max(1.0, under_score - (role_risk_penalty * 0.35)), 1)

    full_role_games = [
        game
        for game in all_game_details
        if game["minutesRegime"] == "full" and game["minutes"] > 0
    ]
    limited_games = [
        game
        for game in all_game_details
        if game["minutesRegime"] == "limited" and game["minutes"] > 0
    ]
    non_dnp_games = [game for game in all_game_details if game["minutes"] >= 5]
    any_minutes_games = [game for game in all_game_details if game["minutes"] > 0]

    full_minutes = [game["minutes"] for game in full_role_games]
    minutes_last5 = (
        round(float(np.mean(full_minutes[:5])), 1) if full_minutes else None
    )
    minutes_last10 = (
        round(float(np.mean(full_minutes[:10])), 1) if full_minutes else None
    )
    minutes_sample_full = (
        round(float(np.mean(full_minutes)), 1) if full_minutes else None
    )

    projection_minutes = None
    minutes_fallback_used = False
    weighted_minutes = 0.0
    minutes_weight_total = 0.0
    for value, weight in (
        (minutes_last5, 0.5),
        (minutes_last10, 0.3),
        (minutes_sample_full, 0.2),
    ):
        if value is not None:
            weighted_minutes += value * weight
            minutes_weight_total += weight
    if minutes_weight_total > 0:
        projection_minutes = weighted_minutes / minutes_weight_total
    else:
        minutes_fallback_used = True
        projection_minutes = avg_minutes if avg_minutes > 0 else baseline_minutes
    projection_minutes = round(float(clamp(projection_minutes, 0.0, 48.0)), 2)

    full_rates = [
        (game["value"] / game["minutes"])
        for game in full_role_games
        if game["minutes"] > 0
    ]
    non_dnp_rates = [
        (game["value"] / game["minutes"])
        for game in non_dnp_games
        if game["minutes"] > 0
    ]
    any_minutes_rates = [
        (game["value"] / game["minutes"])
        for game in any_minutes_games
        if game["minutes"] > 0
    ]

    projection_sample_size_full = len(full_rates)
    projection_sample_size_limited = len(limited_games)
    rate_fallback_used = False

    recent_rate = weighted_mean(full_rates[:HIT_RATE_GAMES], RECENCY_WEIGHTS)
    sample_full_rate = float(np.mean(full_rates)) if full_rates else None
    projection_rate = None
    projection_method = None

    if projection_sample_size_full >= 2:
        if recent_rate is None:
            recent_rate = sample_full_rate
        if sample_full_rate is None:
            sample_full_rate = recent_rate
        if recent_rate is not None and sample_full_rate is not None:
            projection_rate = 0.7 * recent_rate + 0.3 * sample_full_rate
        projection_method = "scaled_rate_empirical"
    elif projection_sample_size_full == 1:
        projection_rate = full_rates[0]
        projection_method = "scaled_rate_empirical_single_full"
        rate_fallback_used = True
    else:
        rate_fallback_used = True
        fallback_rates = non_dnp_rates if non_dnp_rates else any_minutes_rates
        fallback_recent = (
            weighted_mean(fallback_rates[:HIT_RATE_GAMES], RECENCY_WEIGHTS)
            if fallback_rates
            else None
        )
        fallback_sample = float(np.mean(fallback_rates)) if fallback_rates else None
        if fallback_recent is None:
            fallback_recent = fallback_sample
        if fallback_sample is None:
            fallback_sample = fallback_recent
        if fallback_recent is not None and fallback_sample is not None:
            projection_rate = 0.7 * fallback_recent + 0.3 * fallback_sample
        projection_method = "scaled_rate_empirical_fallback_non_dnp"
        recent_rate = fallback_recent
        sample_full_rate = fallback_sample

    projection_mean = None
    projection_std = None
    p_over_full = None
    p_under_full = None
    if projection_sample_size_full >= 2:
        proj_totals = [rate * projection_minutes for rate in full_rates]
    elif projection_sample_size_full == 1:
        proj_totals = [full_rates[0] * projection_minutes]
    else:
        fallback_rates = non_dnp_rates if non_dnp_rates else any_minutes_rates
        proj_totals = (
            [rate * projection_minutes for rate in fallback_rates] if fallback_rates else []
        )

    if proj_totals:
        projection_mean = round(float(np.mean(proj_totals)), 2)
        projection_std = (
            round(float(np.std(proj_totals)), 2) if len(proj_totals) >= 2 else None
        )
        p_over_full = round(
            sum(1 for total in proj_totals if total > line) / len(proj_totals), 3
        )
        p_under_full = round(
            sum(1 for total in proj_totals if total < line) / len(proj_totals), 3
        )

    limited_totals = [game["value"] for game in limited_games]
    if limited_totals:
        p_over_limited = round(
            sum(1 for total in limited_totals if total > line) / len(limited_totals),
            3,
        )
        p_under_limited = round(
            sum(1 for total in limited_totals if total < line) / len(limited_totals),
            3,
        )
    else:
        p_over_limited = 0.0
        p_under_limited = 0.0

    p_dnp = round(dnp_rate_weighted / 100.0, 3)
    p_limited = round(limited_rate_weighted / 100.0, 3)
    p_full = round(max(0.0, 1.0 - p_dnp - p_limited), 3)
    p_over_adjusted = (
        round(p_full * p_over_full + p_limited * p_over_limited, 3)
        if p_over_full is not None
        else None
    )
    p_under_adjusted = (
        round(p_full * p_under_full + p_limited * p_under_limited, 3)
        if p_under_full is not None
        else None
    )
    p_void = p_dnp

    projection_confidence_band, projection_confidence_reasons = classify_projection_confidence(
        projection_sample_size_full,
        projection_mean,
        projection_std,
        minutes_fallback_used,
        rate_fallback_used,
    )
    projection_low_confidence = projection_confidence_band != "stable"
    projection_family_status = get_projection_family_status(stat_type)

    base_recommended_side = "over" if over_score >= under_score else "under"
    base_score = over_score if base_recommended_side == "over" else under_score
    base_pass_reason = None
    if sample_size < MIN_SAMPLE_SIZE_FOR_PICK:
        base_pass_reason = f"Pass: only {sample_size} clean games in sample."
        base_pass_kind = "sample"
    elif edge_pct_abs < 3.0:
        base_pass_reason = f"Pass: edge is too small at {edge_pct}% vs line."
        base_pass_kind = "edge"
    elif base_score < MIN_SCORE_FOR_PICK:
        base_pass_kind = "score"
    else:
        base_pass_kind = None

    clean_metrics = build_clean_game_metrics(df, clean_flags)
    context_cache = load_team_context_cache(TEAM_CONTEXT_FILE)
    context = compute_context_for_prop(
        df,
        clean_metrics,
        stat_type,
        game_hint,
        start_time,
        context_cache,
        enable_pace=CONTEXT_ENABLE_PACE,
        enable_opponent=CONTEXT_ENABLE_OPPONENT,
        enable_rest=CONTEXT_ENABLE_REST,
        enable_role=CONTEXT_ENABLE_ROLE,
    )

    over_final_score = round(clamp(over_score + context["contextBias"], 1.0, 10.0), 1)
    under_final_score = round(clamp(under_score - context["contextBias"], 1.0, 10.0), 1)
    recommended_side = "over" if over_final_score >= under_final_score else "under"
    base_score_for_selected = over_score if recommended_side == "over" else under_score
    recommendation_strength = (
        over_final_score if recommended_side == "over" else under_final_score
    )
    winning_weighted_pct = (
        over_weighted_pct if recommended_side == "over" else under_weighted_pct
    )
    signed_edge = edge if recommended_side == "over" else -edge
    signed_adjustments = {
        "paceAdjustment": context["paceBias"] if recommended_side == "over" else -context["paceBias"],
        "opponentAdjustment": context["opponentBias"] if recommended_side == "over" else -context["opponentBias"],
        "restAdjustment": context["restBias"] if recommended_side == "over" else -context["restBias"],
        "roleAdjustment": context["roleBias"] if recommended_side == "over" else -context["roleBias"],
    }
    context_score_delta = round(sum(signed_adjustments.values()), 2)
    opponent_detail = None
    opponent_inputs = (context.get("inputs") or {}).get("opponent") or {}
    if opponent_inputs:
        if opponent_inputs.get("fallbackUsed"):
            reason = opponent_inputs.get("fallbackReason") or "playtype unavailable"
            opponent_detail = f"baseline only; {reason}"
        elif opponent_inputs.get("baselineBias") is not None and opponent_inputs.get("playtypeBias") is not None:
            base_bias = float(opponent_inputs.get("baselineBias") or 0.0)
            play_bias = float(opponent_inputs.get("playtypeBias") or 0.0)
            if recommended_side == "under":
                base_bias = -base_bias
                play_bias = -play_bias
            opponent_detail = f"base {base_bias:+.2f}, playtype {play_bias:+.2f}"
    context_summary = build_context_summary(signed_adjustments, opponent_detail=opponent_detail)

    hard_pass = base_pass_kind in ("sample", "edge")
    if hard_pass:
        recommended_side = "pass"
        reason_summary = base_pass_reason
        tier_candidate = "skip"
    elif recommendation_strength < MIN_SCORE_FOR_PICK:
        recommended_side = "pass"
        reason_summary = (
            f"Pass: stronger side only scored {recommendation_strength}/10 after volatility, sample, and context adjustments."
        )
        tier_candidate = "skip"
    else:
        tier_candidate = classify_tier(recommendation_strength)
        reason_summary = build_reason_summary(
            recommended_side,
            recommendation_strength,
            winning_weighted_pct,
            signed_edge,
            sample_size,
            volatility,
            context_summary=context_summary,
        )
        if role_risk_penalty > 0:
            reason_summary += (
                f" Role risk: DNP {dnp_rate_weighted}% | limited {limited_rate_weighted}%."
            )

    home_totals = [game["value"] for game in clean_for_hitrate if game["home"]]
    away_totals = [game["value"] for game in clean_for_hitrate if not game["home"]]
    home_avg = round(float(np.mean(home_totals)), 1) if home_totals else None
    away_avg = round(float(np.mean(away_totals)), 1) if away_totals else None
    home_gp = len(home_totals)
    away_gp = len(away_totals)
    home_over_pct = (
        round((sum(1 for value in home_totals if value > line) / home_gp) * 100)
        if home_gp > 0
        else None
    )
    away_over_pct = (
        round((sum(1 for value in away_totals if value > line) / away_gp) * 100)
        if away_gp > 0
        else None
    )
    home_under_pct = (
        round((sum(1 for value in home_totals if value < line) / home_gp) * 100)
        if home_gp > 0
        else None
    )
    away_under_pct = (
        round((sum(1 for value in away_totals if value < line) / away_gp) * 100)
        if away_gp > 0
        else None
    )

    return {
        "sampleSize": sample_size,
        "flaggedGames": flagged_count,
        "minutesBaseline": baseline_minutes,
        "minutesRegimeCounts": minutes_regime_counts,
        "dnpRateWeighted": dnp_rate_weighted,
        "limitedRateWeighted": limited_rate_weighted,
        "roleRiskPenalty": role_risk_penalty,
        "projectionMethod": projection_method,
        "projectionConfidenceBand": projection_confidence_band,
        "projectionFamilyStatus": projection_family_status,
        "projectionMinutes": projection_minutes,
        "projectionRate": round(projection_rate, 4) if projection_rate is not None else None,
        "projectionMean": projection_mean,
        "projectionStd": projection_std,
        "projectionSampleSizeFull": projection_sample_size_full,
        "projectionSampleSizeLimited": projection_sample_size_limited,
        "pOverFull": p_over_full,
        "pUnderFull": p_under_full,
        "pOverAdjusted": p_over_adjusted,
        "pUnderAdjusted": p_under_adjusted,
        "pVoid": p_void,
        "projectionLowConfidence": projection_low_confidence,
        "projectionConfidenceReasons": projection_confidence_reasons,
        "projectionInputs": {
            "minutesLast5": minutes_last5,
            "minutesLast10": minutes_last10,
            "minutesSampleFull": minutes_sample_full,
            "recentRate": round(recent_rate, 4) if recent_rate is not None else None,
            "sampleFullRate": round(sample_full_rate, 4) if sample_full_rate is not None else None,
        },
        "avgMinutes": avg_minutes,
        "hitSampleSize": hit_sample_size,
        "total": hit_sample_size,
        "pushes": pushes,
        "mean": mean,
        "median": median,
        "stdDev": std_dev,
        "volatility": volatility,
        "edge": edge,
        "edgePct": edge_pct,
        "overHitRate": over_hit_rate,
        "underHitRate": under_hit_rate,
        "overWeightedPct": over_weighted_pct,
        "underWeightedPct": under_weighted_pct,
        "overBar": build_bar(over_hit_rate),
        "underBar": build_bar(under_hit_rate),
        "overWeightedBar": build_bar(over_weighted_pct),
        "underWeightedBar": build_bar(under_weighted_pct),
        "overScore": over_score,
        "underScore": under_score,
        "baseScore": base_score_for_selected,
        "finalScore": recommendation_strength,
        "paceAdjustment": signed_adjustments["paceAdjustment"],
        "opponentAdjustment": signed_adjustments["opponentAdjustment"],
        "restAdjustment": signed_adjustments["restAdjustment"],
        "roleAdjustment": signed_adjustments["roleAdjustment"],
        "contextScoreDelta": context_score_delta,
        "contextSummary": context_summary,
        "contextInputs": context["inputs"],
        "recommendedSide": recommended_side,
        "recommendationStrength": recommendation_strength,
        "tierCandidate": tier_candidate,
        "reasonSummary": reason_summary,
        "homeAvg": home_avg,
        "awayAvg": away_avg,
        "homeGP": home_gp,
        "awayGP": away_gp,
        "homeOverPct": home_over_pct,
        "awayOverPct": away_over_pct,
        "homeUnderPct": home_under_pct,
        "awayUnderPct": away_under_pct,
        "games": clean_for_hitrate[:5],
        "flaggedList": [game for game in all_game_details if game["flagged"]][:3],
    }


def main():
    if len(sys.argv) < 4:
        print(
            json.dumps(
                {
                    "error": (
                        "Usage: python nba_stats.py <player_name> <stat_type> <line> "
                        "[opponent_hint] [start_time]"
                    )
                }
            )
        )
        sys.exit(1)

    player_name = sys.argv[1]
    stat_type = sys.argv[2]
    line = float(sys.argv[3])
    game_hint = sys.argv[4] if len(sys.argv) > 4 else None
    start_time = sys.argv[5] if len(sys.argv) > 5 else None

    try:
        player_id, full_name = find_player(player_name)
        dataframe = get_game_log(player_id, limit=SAMPLE_SIZE)
        result = compute_analytics(dataframe, stat_type, line, game_hint, start_time)

        print(json.dumps({
            "success": True,
            "playerName": full_name,
            "statType": stat_type,
            "line": line,
            "analytics": result,
        }))

    except Exception as exc:
        print(json.dumps({
            "success": False,
            "error": str(exc),
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
