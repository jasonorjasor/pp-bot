"""
Play-type context cache and matchup bias utilities.
Uses official NBA Synergy play-type data via nba_api.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from nba_api.stats.endpoints import SynergyPlayTypes

from prop_utils import CURRENT_SEASON

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_ACTIVE_DIR = BASE_DIR / "data" / "active"
PLAYTYPE_CACHE_FILE = str(DATA_ACTIVE_DIR / "playTypeCache.json")
SOURCE_LABEL = "official_nba_synergy_playtypes"

PLAYTYPE_API_MAP = {
    "spot_up": "Spotup",
    "transition": "Transition",
    "isolation": "Isolation",
    "pick_and_roll_ball_handler": "PRBallHandler",
    "pick_and_roll_roll_man": "PRRollMan",
    "handoff": "Handoff",
    "cut": "Cut",
    "off_screen": "OffScreen",
}

PLAYTYPE_NORMALIZATION = {
    "spotup": "spot_up",
    "transition": "transition",
    "isolation": "isolation",
    "prballhandler": "pick_and_roll_ball_handler",
    "pickandrollballhandler": "pick_and_roll_ball_handler",
    "prrollman": "pick_and_roll_roll_man",
    "pickandrollrollman": "pick_and_roll_roll_man",
    "handoff": "handoff",
    "cut": "cut",
    "offscreen": "off_screen",
}

PLAYTYPE_BY_FAMILY = {
    "points": [
        "spot_up",
        "transition",
        "isolation",
        "pick_and_roll_ball_handler",
        "pick_and_roll_roll_man",
        "handoff",
        "cut",
        "off_screen",
    ],
    "assists": [
        "pick_and_roll_ball_handler",
        "handoff",
        "transition",
        "spot_up",
        "off_screen",
    ],
    "threePM": [
        "spot_up",
        "handoff",
        "off_screen",
        "transition",
    ],
}


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


def is_stale(timestamp: str | None, ttl_hours: float) -> bool:
    parsed = parse_iso_datetime(timestamp)
    if parsed is None:
        return True
    return datetime.now(UTC) - parsed > timedelta(hours=ttl_hours)


def normalize_playtype_label(label: str | None) -> str | None:
    if not label:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", str(label).lower())
    return PLAYTYPE_NORMALIZATION.get(normalized)


def load_cache(cache_file: str) -> dict | None:
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def save_cache(cache_file: str, cache: dict) -> None:
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2)


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
    lower_map = {col.lower(): col for col in df.columns}
    for candidate in candidates:
        key = candidate.lower()
        if key in lower_map:
            return lower_map[key]
    return None


def _map_team_abbr(row: pd.Series, team_name_map: dict[str, str]) -> str | None:
    abbr = row.get("TEAM_ABBREVIATION") or row.get("TEAM_ABB") or row.get("TEAM")
    if abbr:
        return str(abbr).upper()
    team_name = row.get("TEAM_NAME")
    if team_name and team_name_map:
        return team_name_map.get(str(team_name).upper())
    return None


def _compute_league_ppp(teams_defense: dict) -> dict:
    total_poss = sum(value.get("poss", 0.0) for value in teams_defense.values())
    if total_poss > 0:
        weighted_ppp = sum(
            value.get("ppp", 0.0) * value.get("poss", 0.0)
            for value in teams_defense.values()
        )
        return {"ppp": round(weighted_ppp / total_poss, 3), "poss": round(total_poss, 1)}
    values = [value.get("ppp", 0.0) for value in teams_defense.values() if value.get("ppp")]
    avg_ppp = sum(values) / len(values) if values else 0.0
    return {"ppp": round(avg_ppp, 3), "poss": 0.0}


def _fetch_synergy_playtype(playtype_api: str, player_or_team: str, type_grouping: str, season: str, retries: int = 3) -> pd.DataFrame:
    last_error = None
    for attempt in range(retries):
        try:
            time.sleep(1)
            endpoint = SynergyPlayTypes(
                season=season,
                player_or_team_abbreviation=player_or_team,
                play_type_nullable=playtype_api,
                type_grouping_nullable=type_grouping,
                timeout=30,
            )
            df = endpoint.get_data_frames()[0].copy()
            if df.empty:
                raise ValueError("SynergyPlayTypes returned empty data.")
            return df
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise ValueError(f"SynergyPlayTypes failed for {playtype_api} {player_or_team}/{type_grouping}: {last_error}")


def _build_playtype_dataset(playtype_key: str, team_name_map: dict[str, str], season: str) -> dict:
    playtype_api = PLAYTYPE_API_MAP.get(playtype_key)
    if not playtype_api:
        raise ValueError(f"Unknown play type: {playtype_key}")

    players_df = _fetch_synergy_playtype(playtype_api, "P", "Offensive", season)
    teams_df = _fetch_synergy_playtype(playtype_api, "T", "Defensive", season)

    player_id_col = _find_column(players_df, ["PLAYER_ID"])
    player_name_col = _find_column(players_df, ["PLAYER_NAME"])
    poss_col = _find_column(players_df, ["POSS", "POSS_PCT", "POSSESSIONS"])
    ppp_col = _find_column(players_df, ["PPP"])

    if not player_id_col or not player_name_col or not poss_col or not ppp_col:
        raise ValueError("SynergyPlayTypes player columns missing required fields.")

    players = {}
    for _, row in players_df.iterrows():
        player_id = row.get(player_id_col)
        if player_id is None:
            continue
        poss = float(row.get(poss_col) or 0.0)
        if poss <= 0:
            continue
        ppp = float(row.get(ppp_col) or 0.0)
        team_abbr = _map_team_abbr(row, team_name_map) if team_name_map else None
        players[str(int(player_id))] = {
            "playerId": int(player_id),
            "playerName": str(row.get(player_name_col) or "").strip(),
            "team": team_abbr,
            "poss": round(poss, 1),
            "ppp": round(ppp, 3),
        }

    team_abbr_col = _find_column(teams_df, ["TEAM_ABBREVIATION", "TEAM_ABB", "TEAM"])
    team_name_col = _find_column(teams_df, ["TEAM_NAME"])
    team_poss_col = _find_column(teams_df, ["POSS", "POSS_PCT", "POSSESSIONS"])
    team_ppp_col = _find_column(teams_df, ["PPP"])

    if not team_poss_col or not team_ppp_col:
        raise ValueError("SynergyPlayTypes team columns missing required fields.")

    teams_defense = {}
    for _, row in teams_df.iterrows():
        poss = float(row.get(team_poss_col) or 0.0)
        if poss <= 0:
            continue
        ppp = float(row.get(team_ppp_col) or 0.0)
        abbr = None
        if team_abbr_col and row.get(team_abbr_col):
            abbr = str(row.get(team_abbr_col)).upper()
        elif team_name_col and row.get(team_name_col) and team_name_map:
            abbr = team_name_map.get(str(row.get(team_name_col)).upper())
        if not abbr:
            continue
        teams_defense[abbr] = {
            "team": abbr,
            "poss": round(poss, 1),
            "ppp": round(ppp, 3),
        }

    league_defense = _compute_league_ppp(teams_defense)

    return {
        "generatedAt": datetime.now(UTC).isoformat(),
        "status": "ok",
        "players": players,
        "teamsDefense": teams_defense,
        "leagueDefense": league_defense,
    }


def ensure_playtype_cache(playtype_key: str, ttl_hours: float, team_name_map: dict[str, str], season: str = CURRENT_SEASON) -> dict:
    cache = load_cache(PLAYTYPE_CACHE_FILE) or {}
    if cache.get("season") != season:
        cache = {}

    playtypes = cache.get("playtypes", {})
    entry = playtypes.get(playtype_key)
    if entry and not is_stale(entry.get("generatedAt"), ttl_hours):
        return cache

    try:
        entry = _build_playtype_dataset(playtype_key, team_name_map, season)
    except Exception as exc:
        entry = {
            "generatedAt": datetime.now(UTC).isoformat(),
            "status": "error",
            "error": str(exc),
            "players": {},
            "teamsDefense": {},
            "leagueDefense": {},
        }

    cache = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "season": season,
        "source": SOURCE_LABEL,
        "playtypes": {**playtypes, playtype_key: entry},
    }
    save_cache(PLAYTYPE_CACHE_FILE, cache)
    return cache


def compute_playtype_bias(
    *,
    stat_family: str,
    player_id: int | None,
    opponent_team: str | None,
    team_name_map: dict[str, str],
    ttl_hours: float,
    min_share: float,
    min_poss: float,
    max_types: int,
) -> tuple[float, dict]:
    if not player_id:
        return 0.0, {"fallbackUsed": True, "fallbackReason": "missing_player_data"}
    if not opponent_team:
        return 0.0, {"fallbackUsed": True, "fallbackReason": "missing_team_data"}

    playtypes = PLAYTYPE_BY_FAMILY.get(stat_family)
    if not playtypes:
        return 0.0, {"fallbackUsed": True, "fallbackReason": "no_qualifying_playtypes"}

    player_entries = {}
    team_entries = {}
    league_entries = {}

    for playtype_key in playtypes:
        cache = ensure_playtype_cache(playtype_key, ttl_hours, team_name_map)
        entry = cache.get("playtypes", {}).get(playtype_key, {})
        if entry.get("status") != "ok":
            return 0.0, {"fallbackUsed": True, "fallbackReason": "endpoint_failure"}

        players = entry.get("players", {})
        teams_defense = entry.get("teamsDefense", {})
        league_defense = entry.get("leagueDefense", {})

        player_entries[playtype_key] = players.get(str(player_id))
        team_entries[playtype_key] = teams_defense.get(opponent_team)
        league_entries[playtype_key] = league_defense

    if all(value is None for value in player_entries.values()):
        return 0.0, {"fallbackUsed": True, "fallbackReason": "missing_player_data"}

    total_poss = sum((value or {}).get("poss", 0.0) for value in player_entries.values())
    if total_poss <= 0:
        return 0.0, {"fallbackUsed": True, "fallbackReason": "missing_player_data"}

    candidate = []
    for playtype_key, player_entry in player_entries.items():
        if not player_entry:
            continue
        poss = float(player_entry.get("poss", 0.0))
        share = poss / total_poss if total_poss > 0 else 0.0
        if poss < min_poss or share < min_share:
            continue
        candidate.append((playtype_key, share, poss, player_entry))

    if not candidate:
        return 0.0, {"fallbackUsed": True, "fallbackReason": "no_qualifying_playtypes"}

    candidate.sort(key=lambda item: item[1], reverse=True)
    candidate = candidate[:max_types]

    playtypes_used = []
    delta_sum = 0.0
    for playtype_key, share, poss, player_entry in candidate:
        team_entry = team_entries.get(playtype_key)
        league_entry = league_entries.get(playtype_key)
        if not team_entry or not league_entry or not league_entry.get("ppp"):
            return 0.0, {"fallbackUsed": True, "fallbackReason": "missing_team_data"}

        opp_ppp = float(team_entry.get("ppp", 0.0))
        league_ppp = float(league_entry.get("ppp", 0.0))
        if league_ppp <= 0:
            continue
        delta_pct = ((opp_ppp - league_ppp) / league_ppp) * 100.0
        delta_sum += share * delta_pct
        playtypes_used.append(
            {
                "playtype": playtype_key,
                "share": round(share, 3),
                "poss": round(poss, 1),
                "oppPPP": round(opp_ppp, 3),
                "leaguePPP": round(league_ppp, 3),
                "deltaPct": round(delta_pct, 2),
            }
        )

    playtype_bias = max(-0.35, min(0.35, round(delta_sum / 25.0, 3)))
    return playtype_bias, {
        "fallbackUsed": False,
        "fallbackReason": None,
        "playtypeBias": playtype_bias,
        "playtypeDeltaPct": round(delta_sum, 2),
        "playtypesUsed": playtypes_used,
    }
