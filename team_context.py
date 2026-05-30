"""
Team context cache and conservative matchup adjustments for prop analytics.
"""

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from nba_api.stats.endpoints import leaguegamelog

from prop_utils import CURRENT_SEASON
from playtype_context import compute_playtype_bias

BASE_DIR = Path(__file__).resolve().parent
DATA_ACTIVE_DIR = BASE_DIR / "data" / "active"
TEAM_CONTEXT_FILE = str(DATA_ACTIVE_DIR / "teamContextCache.json")
SOURCE_LABEL = "official_nba_leaguegamelog"

ALLOWANCE_KEYS = [
    "points",
    "assists",
    "rebounds",
    "turnovers",
    "stocks",
    "fantasy",
    "fgMade",
    "fgAttempted",
    "ftMade",
    "ftAttempted",
    "twoPM",
    "twoPA",
    "threePM",
    "threePA",
]

PACE_SENSITIVE_KEYS = {
    "Points",
    "Assists",
    "Pts+Asts",
    "Pts+Rebs+Asts",
    "FG Attempted",
    "Field Goals Attempted",
    "3-PT Attempted",
    "3-Pointers Attempted",
    "3 Pointers Attempted",
    "Fantasy Score",
    "Fantasy Points",
}

ALLOWANCE_MAP = {
    "Points": "points",
    "Assists": "assists",
    "Rebounds": "rebounds",
    "Offensive Rebounds": "rebounds",
    "Defensive Rebounds": "rebounds",
    "Turnovers": "turnovers",
    "Steals": "stocks",
    "Blocks": "stocks",
    "Blocked Attempts": "stocks",
    "Blks+Stls": "stocks",
    "Fantasy Score": "fantasy",
    "Fantasy Points": "fantasy",
    "Pts+Rebs": "points",
    "Pts+Asts": "assists",
    "Rebs+Asts": "rebounds",
    "Pts+Rebs+Asts": "points",
    "Field Goals Made": "fgMade",
    "FG Made": "fgMade",
    "Field Goals Attempted": "fgAttempted",
    "FG Attempted": "fgAttempted",
    "FT Made": "ftMade",
    "Free Throws Made": "ftMade",
    "Free Throws": "ftMade",
    "FT Attempted": "ftAttempted",
    "Free Throws Attempted": "ftAttempted",
    "Two Pointers Made": "twoPM",
    "2-Pointers Made": "twoPM",
    "2 Pointers Made": "twoPM",
    "Two Pointers Attempted": "twoPA",
    "2-Pointers Attempted": "twoPA",
    "2 Pointers Attempted": "twoPA",
    "3-PT Made": "threePM",
    "3-Pointers Made": "threePM",
    "3 Pointers Made": "threePM",
    "3-PT Attempted": "threePA",
    "3-Pointers Attempted": "threePA",
    "3 Pointers Attempted": "threePA",
}

REST_SENSITIVITY = {
    "points": 1.0,
    "assists": 1.0,
    "rebounds": 0.5,
    "turnovers": 0.0,
    "stocks": 0.0,
    "fantasy": 0.8,
    "fgMade": 1.0,
    "fgAttempted": 1.0,
    "ftMade": 0.7,
    "ftAttempted": 0.7,
    "twoPM": 1.0,
    "twoPA": 1.0,
    "threePM": 0.9,
    "threePA": 1.0,
}

ROLE_SECONDARY_MAP = {
    "assists": "astPerMin",
    "rebounds": "rebPerMin",
    "stocks": "stocksPerMin",
}


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def parse_bool_env(value, default=True):
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def parse_positive_number_env(value, default):
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def parse_positive_int_env(value, default):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


ENABLE_PLAYTYPE_OPPONENT_BIAS = parse_bool_env(os.getenv("ENABLE_PLAYTYPE_OPPONENT_BIAS"), True)
PLAYTYPE_CACHE_TTL_HOURS = parse_positive_number_env(os.getenv("PLAYTYPE_CACHE_TTL_HOURS"), 24)
OPPONENT_BASELINE_WEIGHT = parse_positive_number_env(os.getenv("OPPONENT_BASELINE_WEIGHT"), 0.7)
OPPONENT_PLAYTYPE_WEIGHT = parse_positive_number_env(os.getenv("OPPONENT_PLAYTYPE_WEIGHT"), 0.3)
OPPONENT_PLAYTYPE_MIN_SHARE = parse_positive_number_env(os.getenv("OPPONENT_PLAYTYPE_MIN_SHARE"), 0.08)
OPPONENT_PLAYTYPE_MIN_POSS = parse_positive_number_env(os.getenv("OPPONENT_PLAYTYPE_MIN_POSS"), 25)
OPPONENT_PLAYTYPE_MAX_TYPES = parse_positive_int_env(os.getenv("OPPONENT_PLAYTYPE_MAX_TYPES"), 3)


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


def parse_game_hint(game_hint):
    if not game_hint:
        return None
    tokens = re.findall(r"[A-Z]{2,4}", str(game_hint).upper())
    return tokens[0] if tokens else None


def parse_team_from_matchup(matchup):
    if not matchup:
        return None
    text = str(matchup).upper()
    tokens = re.findall(r"[A-Z]{2,4}", text)
    return tokens[0] if tokens else None


def compute_possessions(df):
    return df["FGA"] - df["OREB"] + df["TOV"] + (0.44 * df["FTA"])


def _per100(numerator, denominator):
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 1)


def compute_allowance_metrics(team_df):
    metrics = {key: [] for key in ALLOWANCE_KEYS}
    for _, row in team_df.iterrows():
        opp_poss = float(row["OPP_POSS"])
        opponent_points = float(row["OPP_PTS"])
        opponent_rebounds = float(row["OPP_REB"])
        opponent_assists = float(row["OPP_AST"])
        opponent_turnovers = float(row["OPP_TOV"])
        opponent_stocks = float(row["OPP_STL"]) + float(row["OPP_BLK"])
        opponent_fg_made = float(row["OPP_FGM"])
        opponent_fg_attempted = float(row["OPP_FGA"])
        opponent_ft_made = float(row["OPP_FTM"])
        opponent_ft_attempted = float(row["OPP_FTA"])
        opponent_two_pm = float(row["OPP_FGM"]) - float(row["OPP_FG3M"])
        opponent_two_pa = float(row["OPP_FGA"]) - float(row["OPP_FG3A"])
        opponent_three_pm = float(row["OPP_FG3M"])
        opponent_three_pa = float(row["OPP_FG3A"])
        opponent_fantasy = (
            opponent_points
            + (opponent_rebounds * 1.2)
            + (opponent_assists * 1.5)
            + ((float(row["OPP_STL"]) + float(row["OPP_BLK"])) * 3.0)
        )

        metrics["points"].append(_per100(opponent_points, opp_poss))
        metrics["assists"].append(_per100(opponent_assists, opp_poss))
        metrics["rebounds"].append(_per100(opponent_rebounds, opp_poss))
        metrics["turnovers"].append(_per100(opponent_turnovers, opp_poss))
        metrics["stocks"].append(_per100(opponent_stocks, opp_poss))
        metrics["fantasy"].append(_per100(opponent_fantasy, opp_poss))
        metrics["fgMade"].append(_per100(opponent_fg_made, opp_poss))
        metrics["fgAttempted"].append(_per100(opponent_fg_attempted, opp_poss))
        metrics["ftMade"].append(_per100(opponent_ft_made, opp_poss))
        metrics["ftAttempted"].append(_per100(opponent_ft_attempted, opp_poss))
        metrics["twoPM"].append(_per100(opponent_two_pm, opp_poss))
        metrics["twoPA"].append(_per100(opponent_two_pa, opp_poss))
        metrics["threePM"].append(_per100(opponent_three_pm, opp_poss))
        metrics["threePA"].append(_per100(opponent_three_pa, opp_poss))

    return {
        key: round(sum(values) / len(values), 1) if values else 0.0
        for key, values in metrics.items()
    }


def blend_metric_dicts(season_metrics, last10_metrics):
    return {
        key: round((last10_metrics.get(key, 0.0) * 0.6) + (season_metrics.get(key, 0.0) * 0.4), 1)
        for key in ALLOWANCE_KEYS
    }


def fetch_team_game_log(season=CURRENT_SEASON, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            time.sleep(1)
            endpoint = leaguegamelog.LeagueGameLog(
                season=season,
                season_type_all_star="Regular Season",
                player_or_team_abbreviation="T",
                timeout=30,
            )
            df = endpoint.get_data_frames()[0].copy()
            if df.empty:
                raise ValueError("No team game log data returned.")
            df["GAME_DATE_DT"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
            if df["GAME_DATE_DT"].isna().any():
                raise ValueError("Failed to parse one or more team game dates from league game logs.")
            df["POSS"] = compute_possessions(df)
            return df
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise ValueError(f"Failed to fetch team game logs: {last_error}")


def attach_opponent_rows(df):
    opponent = df[
        [
            "GAME_ID",
            "TEAM_ID",
            "TEAM_ABBREVIATION",
            "TEAM_NAME",
            "PTS",
            "REB",
            "AST",
            "TOV",
            "STL",
            "BLK",
            "FGM",
            "FGA",
            "FG3M",
            "FG3A",
            "FTM",
            "FTA",
            "POSS",
        ]
    ].rename(
        columns={
            "TEAM_ID": "OPP_TEAM_ID",
            "TEAM_ABBREVIATION": "OPP_TEAM_ABBREVIATION",
            "TEAM_NAME": "OPP_TEAM_NAME",
            "PTS": "OPP_PTS",
            "REB": "OPP_REB",
            "AST": "OPP_AST",
            "TOV": "OPP_TOV",
            "STL": "OPP_STL",
            "BLK": "OPP_BLK",
            "FGM": "OPP_FGM",
            "FGA": "OPP_FGA",
            "FG3M": "OPP_FG3M",
            "FG3A": "OPP_FG3A",
            "FTM": "OPP_FTM",
            "FTA": "OPP_FTA",
            "POSS": "OPP_POSS",
        }
    )
    merged = df.merge(opponent, on="GAME_ID", how="inner")
    return merged[merged["TEAM_ID"] != merged["OPP_TEAM_ID"]].copy()


def build_team_context_cache(season=CURRENT_SEASON):
    team_games = attach_opponent_rows(fetch_team_game_log(season=season))
    teams = {}

    for team_abbrev, rows in team_games.groupby("TEAM_ABBREVIATION"):
        ordered = rows.sort_values("GAME_DATE_DT", ascending=False).reset_index(drop=True)
        last10 = ordered.head(10)
        season_pace = round(float(ordered["POSS"].mean()), 1)
        last10_pace = round(float(last10["POSS"].mean()), 1) if not last10.empty else season_pace
        pace_blended = round((last10_pace * 0.6) + (season_pace * 0.4), 1)
        season_allowance = compute_allowance_metrics(ordered)
        last10_allowance = compute_allowance_metrics(last10)
        blended_allowance = blend_metric_dicts(season_allowance, last10_allowance)

        teams[team_abbrev] = {
            "teamName": ordered.iloc[0]["TEAM_NAME"],
            "pace": {
                "season": season_pace,
                "last10": last10_pace,
                "blended": pace_blended,
            },
            "opponentAllowance": {
                "season": season_allowance,
                "last10": last10_allowance,
                "blended": blended_allowance,
            },
            "recentGameDates": [
                value.date().isoformat()
                for value in ordered["GAME_DATE_DT"].head(10).tolist()
            ],
            "latestGameDate": ordered.iloc[0]["GAME_DATE_DT"].date().isoformat(),
        }

    league_pace_values = [team["pace"]["season"] for team in teams.values()]
    league_last10_pace_values = [team["pace"]["last10"] for team in teams.values()]
    league_blended_pace_values = [team["pace"]["blended"] for team in teams.values()]

    def league_allowance_for(scope):
        return {
            key: round(
                sum(team["opponentAllowance"][scope][key] for team in teams.values()) / len(teams),
                1,
            )
            for key in ALLOWANCE_KEYS
        }

    cache = {
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "season": season,
        "source": SOURCE_LABEL,
        "teams": teams,
        "league": {
            "pace": {
                "season": round(sum(league_pace_values) / len(league_pace_values), 1),
                "last10": round(sum(league_last10_pace_values) / len(league_last10_pace_values), 1),
                "blended": round(sum(league_blended_pace_values) / len(league_blended_pace_values), 1),
            },
            "opponentAllowance": {
                "season": league_allowance_for("season"),
                "last10": league_allowance_for("last10"),
                "blended": league_allowance_for("blended"),
            },
        },
    }
    return cache


def save_team_context_cache(cache, path=TEAM_CONTEXT_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as handle:
        json.dump(cache, handle, indent=2)


def load_team_context_cache(path=TEAM_CONTEXT_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def summarize_schedule(team_context, scheduled_dt):
    if not team_context or not scheduled_dt:
        return {
            "restDays": None,
            "backToBack": False,
            "threeInFour": False,
            "fourInSix": False,
        }

    recent_dates = [
        datetime.fromisoformat(value).date()
        for value in team_context.get("recentGameDates", [])
    ]
    schedule_date = scheduled_dt.date()
    prior_dates = [game_date for game_date in recent_dates if game_date < schedule_date]
    prior_dates.sort(reverse=True)

    rest_days = None
    if prior_dates:
        rest_days = max(0, (schedule_date - prior_dates[0]).days - 1)

    games_last_4 = sum(1 for game_date in prior_dates if (schedule_date - game_date).days <= 3)
    games_last_6 = sum(1 for game_date in prior_dates if (schedule_date - game_date).days <= 5)
    return {
        "restDays": rest_days,
        "backToBack": rest_days == 0 if rest_days is not None else False,
        "threeInFour": games_last_4 >= 2,
        "fourInSix": games_last_6 >= 3,
    }


def build_clean_game_metrics(df, clean_flags):
    metrics = []
    for (_, row), is_clean in zip(df.iterrows(), clean_flags):
        if not is_clean:
            continue
        minutes = float(row["MIN_FLOAT"])
        usage_proxy = float(row["FGA"]) + (0.44 * float(row["FTA"])) + float(row["TOV"])
        assists = float(row["AST"])
        rebounds = float(row["REB"])
        stocks = float(row["STL"]) + float(row["BLK"])
        metrics.append(
            {
                "minutes": minutes,
                "usageProxy": usage_proxy,
                "ast": assists,
                "reb": rebounds,
                "stocks": stocks,
                "usagePerMin": round(usage_proxy / minutes, 3) if minutes > 0 else 0.0,
                "astPerMin": round(assists / minutes, 3) if minutes > 0 else 0.0,
                "rebPerMin": round(rebounds / minutes, 3) if minutes > 0 else 0.0,
                "stocksPerMin": round(stocks / minutes, 3) if minutes > 0 else 0.0,
            }
        )
    return metrics


def _average(values):
    return sum(values) / len(values) if values else 0.0


def _percent_delta(current, baseline):
    if not baseline:
        return 0.0
    return ((current - baseline) / baseline) * 100.0


def get_allowance_key(stat_type):
    return ALLOWANCE_MAP.get(stat_type)


def compute_pace_bias(stat_type, team_context, opponent_context, enabled=True):
    if not enabled or stat_type not in PACE_SENSITIVE_KEYS:
        return 0.0, {}
    if not team_context or not opponent_context:
        return 0.0, {}

    team_pace = float(team_context["pace"]["blended"])
    opponent_pace = float(opponent_context["pace"]["blended"])
    if team_pace <= 0:
        return 0.0, {}

    expected_pace = (team_pace + opponent_pace) / 2.0
    delta_pct = _percent_delta(expected_pace, team_pace)
    if abs(delta_pct) < 1.5:
        return 0.0, {
            "expectedPace": round(expected_pace, 1),
            "teamPace": round(team_pace, 1),
            "opponentPace": round(opponent_pace, 1),
            "paceDeltaPct": round(delta_pct, 1),
        }

    adjustment = round(clamp(delta_pct / 10.0, -0.4, 0.4), 2)
    return adjustment, {
        "expectedPace": round(expected_pace, 1),
        "teamPace": round(team_pace, 1),
        "opponentPace": round(opponent_pace, 1),
        "paceDeltaPct": round(delta_pct, 1),
    }


def compute_opponent_bias(
    stat_type,
    opponent_context,
    league_context,
    *,
    player_id=None,
    opponent_team=None,
    team_name_map=None,
    enabled=True,
):
    allowance_key = get_allowance_key(stat_type)
    baseline_bias = 0.0
    baseline_inputs = {}

    if allowance_key and opponent_context and league_context:
        opponent_value = float(opponent_context["opponentAllowance"]["blended"].get(allowance_key, 0.0))
        league_value = float(league_context["opponentAllowance"]["blended"].get(allowance_key, 0.0))
        if league_value > 0:
            delta_pct = _percent_delta(opponent_value, league_value)
            baseline_inputs = {
                "allowanceKey": allowance_key,
                "opponentAllowance": round(opponent_value, 1),
                "leagueAllowance": round(league_value, 1),
                "allowanceDeltaPct": round(delta_pct, 1),
            }
            if abs(delta_pct) >= 3.0:
                baseline_bias = round(clamp(delta_pct / 25.0, -0.6, 0.6), 2)

    baseline_inputs["baselineBias"] = baseline_bias

    if not enabled or not allowance_key:
        return baseline_bias, baseline_inputs

    if not ENABLE_PLAYTYPE_OPPONENT_BIAS or allowance_key not in ("points", "assists", "threePM"):
        return baseline_bias, baseline_inputs

    if not player_id:
        baseline_inputs["fallbackUsed"] = True
        baseline_inputs["fallbackReason"] = "missing_player_data"
        return baseline_bias, baseline_inputs

    if not opponent_team:
        baseline_inputs["fallbackUsed"] = True
        baseline_inputs["fallbackReason"] = "missing_team_data"
        return baseline_bias, baseline_inputs

    playtype_bias, playtype_inputs = compute_playtype_bias(
        stat_family=allowance_key,
        player_id=player_id,
        opponent_team=opponent_team,
        team_name_map=team_name_map or {},
        ttl_hours=PLAYTYPE_CACHE_TTL_HOURS,
        min_share=OPPONENT_PLAYTYPE_MIN_SHARE,
        min_poss=OPPONENT_PLAYTYPE_MIN_POSS,
        max_types=OPPONENT_PLAYTYPE_MAX_TYPES,
    )

    if playtype_inputs.get("fallbackUsed"):
        baseline_inputs.update(playtype_inputs)
        return baseline_bias, baseline_inputs

    weight_total = OPPONENT_BASELINE_WEIGHT + OPPONENT_PLAYTYPE_WEIGHT
    baseline_weight = OPPONENT_BASELINE_WEIGHT / weight_total if weight_total > 0 else 1.0
    playtype_weight = OPPONENT_PLAYTYPE_WEIGHT / weight_total if weight_total > 0 else 0.0
    blended = clamp(
        (baseline_weight * baseline_bias) + (playtype_weight * playtype_bias),
        -0.6,
        0.6,
    )
    inputs = {
        **baseline_inputs,
        **playtype_inputs,
        "playtypeBias": playtype_bias,
    }
    return round(blended, 2), inputs


def compute_rest_bias(stat_type, team_context, opponent_context, scheduled_dt, enabled=True):
    allowance_key = get_allowance_key(stat_type)
    sensitivity = REST_SENSITIVITY.get(allowance_key, 0.0)
    if not enabled or sensitivity == 0.0:
        return 0.0, {}

    team_schedule = summarize_schedule(team_context, scheduled_dt)
    opponent_schedule = summarize_schedule(opponent_context, scheduled_dt)

    adjustment = 0.0
    if team_schedule["backToBack"]:
        adjustment -= 0.2 * sensitivity
    elif team_schedule["threeInFour"]:
        adjustment -= 0.1 * sensitivity
    if team_schedule["fourInSix"]:
        adjustment -= 0.1 * sensitivity

    if opponent_schedule["backToBack"]:
        adjustment += 0.15 * sensitivity
    elif opponent_schedule["threeInFour"]:
        adjustment += 0.08 * sensitivity
    if opponent_schedule["fourInSix"]:
        adjustment += 0.08 * sensitivity

    return round(clamp(adjustment, -0.3, 0.3), 2), {
        "teamSchedule": team_schedule,
        "opponentSchedule": opponent_schedule,
    }


def compute_role_bias(stat_type, clean_metrics, enabled=True):
    if not enabled or len(clean_metrics) < 8:
        return 0.0, {}

    sample = clean_metrics[:10]
    recent = sample[:5]
    minute_values = [game["minutes"] for game in sample]
    if not minute_values:
        return 0.0, {}

    median_minutes = float(pd.Series(minute_values).median())
    normal_threshold = median_minutes * 0.7
    normal_recent = [game for game in recent if game["minutes"] >= normal_threshold]
    if len(normal_recent) < 4:
        return 0.0, {
            "normalRecentGames": len(normal_recent),
            "normalMinuteThreshold": round(normal_threshold, 1),
        }

    recent_minutes = _average([game["minutes"] for game in normal_recent])
    baseline_minutes = _average([game["minutes"] for game in sample])
    recent_usage = _average([game["usageProxy"] for game in normal_recent])
    baseline_usage = _average([game["usageProxy"] for game in sample])

    allowance_key = get_allowance_key(stat_type)
    secondary_key = ROLE_SECONDARY_MAP.get(allowance_key, "usagePerMin")
    recent_secondary = _average([game[secondary_key] for game in normal_recent])
    baseline_secondary = _average([game[secondary_key] for game in sample])

    minute_delta = recent_minutes - baseline_minutes
    minute_delta_pct = _percent_delta(recent_minutes, baseline_minutes)
    usage_delta_pct = _percent_delta(recent_usage, baseline_usage)
    secondary_delta_pct = _percent_delta(recent_secondary, baseline_secondary)

    minutes_material = abs(minute_delta_pct) >= 10.0 or abs(minute_delta) >= 2.5
    usage_material = abs(usage_delta_pct) >= 8.0
    secondary_material = abs(secondary_delta_pct) >= 8.0
    if not (minutes_material or usage_material or secondary_material):
        return 0.0, {
            "normalRecentGames": len(normal_recent),
            "normalMinuteThreshold": round(normal_threshold, 1),
            "minuteDeltaPct": round(minute_delta_pct, 1),
            "usageDeltaPct": round(usage_delta_pct, 1),
            "secondaryDeltaPct": round(secondary_delta_pct, 1),
            "secondaryKey": secondary_key,
        }

    minute_component = clamp(minute_delta_pct / 30.0, -0.35, 0.35)
    usage_component = clamp(usage_delta_pct / 40.0, -0.25, 0.25)
    secondary_component = clamp(secondary_delta_pct / 80.0, -0.1, 0.1)
    adjustment = round(clamp(minute_component + usage_component + secondary_component, -0.7, 0.7), 2)

    return adjustment, {
        "normalRecentGames": len(normal_recent),
        "normalMinuteThreshold": round(normal_threshold, 1),
        "minuteDeltaPct": round(minute_delta_pct, 1),
        "usageDeltaPct": round(usage_delta_pct, 1),
        "secondaryDeltaPct": round(secondary_delta_pct, 1),
        "secondaryKey": secondary_key,
    }


def build_context_summary(signed_adjustments, opponent_detail=None):
    non_zero = [
        (label, value)
        for label, value in (
            ("pace", signed_adjustments.get("paceAdjustment", 0.0)),
            ("opponent", signed_adjustments.get("opponentAdjustment", 0.0)),
            ("rest", signed_adjustments.get("restAdjustment", 0.0)),
            ("role", signed_adjustments.get("roleAdjustment", 0.0)),
        )
        if abs(value) >= 0.05
    ]
    if not non_zero:
        return "Context neutral."
    parts = []
    for label, value in non_zero:
        if label == "opponent" and opponent_detail:
            parts.append(f"{label} {value:+.2f} ({opponent_detail})")
        else:
            parts.append(f"{label} {value:+.2f}")
    return ", ".join(parts)


def compute_context_for_prop(
    player_df,
    clean_metrics,
    stat_type,
    game_hint,
    start_time,
    cache,
    *,
    enable_pace=True,
    enable_opponent=True,
    enable_rest=True,
    enable_role=True,
):
    if cache is None:
        return {
            "paceBias": 0.0,
            "opponentBias": 0.0,
            "restBias": 0.0,
            "roleBias": 0.0,
            "contextBias": 0.0,
            "inputs": {},
        }

    teams = cache.get("teams", {})
    league_context = cache.get("league", {})
    player_team = str(player_df.iloc[0].get("TEAM_ABBREVIATION") or "").upper()
    if not player_team:
        player_team = parse_team_from_matchup(player_df.iloc[0].get("MATCHUP"))
    opponent_team = parse_game_hint(game_hint)
    scheduled_dt = parse_iso_datetime(start_time)
    player_id = player_df.iloc[0].get("PLAYER_ID") if "PLAYER_ID" in player_df.columns else None
    if player_id is not None:
        try:
            player_id = int(player_id)
        except (TypeError, ValueError):
            player_id = None

    player_team_context = teams.get(player_team)
    opponent_context = teams.get(opponent_team) if opponent_team else None
    if player_team_context is None:
        player_team = None
    if opponent_team and opponent_context is None:
        opponent_team = None
    if player_team and opponent_team and player_team == opponent_team:
        opponent_team = None
        opponent_context = None

    team_name_map = {
        str(value.get("teamName")).upper(): abbr
        for abbr, value in teams.items()
        if value.get("teamName")
    }

    pace_bias, pace_inputs = compute_pace_bias(
        stat_type,
        player_team_context,
        opponent_context,
        enabled=enable_pace,
    )
    opponent_bias, opponent_inputs = compute_opponent_bias(
        stat_type,
        opponent_context,
        league_context,
        player_id=player_id,
        opponent_team=opponent_team,
        team_name_map=team_name_map,
        enabled=enable_opponent,
    )
    rest_bias, rest_inputs = compute_rest_bias(
        stat_type,
        player_team_context,
        opponent_context,
        scheduled_dt,
        enabled=enable_rest,
    )
    role_bias, role_inputs = compute_role_bias(
        stat_type,
        clean_metrics,
        enabled=enable_role,
    )

    return {
        "paceBias": pace_bias,
        "opponentBias": opponent_bias,
        "restBias": rest_bias,
        "roleBias": role_bias,
        "contextBias": round(pace_bias + opponent_bias + rest_bias + role_bias, 2),
        "inputs": {
            "playerTeam": player_team or None,
            "opponentTeam": opponent_team,
            "pace": pace_inputs,
            "opponent": opponent_inputs,
            "rest": rest_inputs,
            "role": role_inputs,
        },
    }
