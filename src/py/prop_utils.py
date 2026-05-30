"""
Shared stat and player helpers for analytics and grading.
"""

import time
from datetime import datetime

import pandas as pd
from nba_api.stats.static import players
from nba_api.stats.endpoints import playergamelog

CURRENT_SEASON = "2025-26"
DEFAULT_GAME_LOG_SEASON_TYPES = (
    "Regular Season",
    "PlayIn",
    "Playoffs",
)

STAT_MAP = {
    "Points": {"type": "simple", "keys": ["PTS"]},
    "Rebounds": {"type": "simple", "keys": ["REB"]},
    "Offensive Rebounds": {"type": "simple", "keys": ["OREB"]},
    "Defensive Rebounds": {"type": "simple", "keys": ["DREB"]},
    "Assists": {"type": "simple", "keys": ["AST"]},
    "Steals": {"type": "simple", "keys": ["STL"]},
    "Blocks": {"type": "simple", "keys": ["BLK"]},
    "Blocked Attempts": {"type": "simple", "keys": ["BLKA"]},
    "Turnovers": {"type": "simple", "keys": ["TOV"]},
    "Personal Fouls": {"type": "simple", "keys": ["PF"]},
    "Fouls": {"type": "simple", "keys": ["PF"]},
    "Fouls Drawn": {"type": "simple", "keys": ["PFD"]},
    "Minutes": {"type": "simple", "keys": ["MIN_FLOAT"]},
    "3-PT Made": {"type": "simple", "keys": ["FG3M"]},
    "3-Pointers Made": {"type": "simple", "keys": ["FG3M"]},
    "3 Pointers Made": {"type": "simple", "keys": ["FG3M"]},
    "3-PT Attempted": {"type": "simple", "keys": ["FG3A"]},
    "3-Pointers Attempted": {"type": "simple", "keys": ["FG3A"]},
    "3 Pointers Attempted": {"type": "simple", "keys": ["FG3A"]},
    "FT Made": {"type": "simple", "keys": ["FTM"]},
    "Free Throws Made": {"type": "simple", "keys": ["FTM"]},
    "Free Throws": {"type": "simple", "keys": ["FTM"]},
    "FT Attempted": {"type": "simple", "keys": ["FTA"]},
    "Free Throws Attempted": {"type": "simple", "keys": ["FTA"]},
    "Field Goals Made": {"type": "simple", "keys": ["FGM"]},
    "FG Made": {"type": "simple", "keys": ["FGM"]},
    "Field Goals Attempted": {"type": "simple", "keys": ["FGA"]},
    "FG Attempted": {"type": "simple", "keys": ["FGA"]},
    "Two Pointers Made": {"type": "derived", "formula": "2PM"},
    "2-Pointers Made": {"type": "derived", "formula": "2PM"},
    "2 Pointers Made": {"type": "derived", "formula": "2PM"},
    "Two Pointers Attempted": {"type": "derived", "formula": "2PA"},
    "2-Pointers Attempted": {"type": "derived", "formula": "2PA"},
    "2 Pointers Attempted": {"type": "derived", "formula": "2PA"},
    "Fantasy Score": {"type": "fantasy", "keys": ["PTS", "REB", "AST", "STL", "BLK"]},
    "Fantasy Points": {"type": "fantasy", "keys": ["PTS", "REB", "AST", "STL", "BLK"]},
    "Pts+Rebs": {"type": "simple", "keys": ["PTS", "REB"]},
    "Pts+Asts": {"type": "simple", "keys": ["PTS", "AST"]},
    "Rebs+Asts": {"type": "simple", "keys": ["REB", "AST"]},
    "Pts+Rebs+Asts": {"type": "simple", "keys": ["PTS", "REB", "AST"]},
    "Blks+Stls": {"type": "simple", "keys": ["BLK", "STL"]},
    "Pts+Rebs+Asts+Stls": {"type": "simple", "keys": ["PTS", "REB", "AST", "STL"]},
    "Pts+Rebs+Asts+Blks": {"type": "simple", "keys": ["PTS", "REB", "AST", "BLK"]},
    "Pts+Rebs+Blks+Stls": {"type": "simple", "keys": ["PTS", "REB", "BLK", "STL"]},
    "Pts+Asts+Blks+Stls": {"type": "simple", "keys": ["PTS", "AST", "BLK", "STL"]},
    "Rebs+Asts+Blks+Stls": {"type": "simple", "keys": ["REB", "AST", "BLK", "STL"]},
}

FANTASY_WEIGHTS = {
    "PTS": 1.0,
    "REB": 1.2,
    "AST": 1.5,
    "STL": 3.0,
    "BLK": 3.0,
}


def parse_minutes(min_str):
    try:
        value = str(min_str).strip()
        if not value or value in ("None", "nan"):
            return 0.0
        if ":" in value:
            mins, secs = value.split(":")
            return int(mins) + int(secs) / 60.0
        return float(value)
    except Exception:
        return 0.0


def find_player(name):
    results = players.find_players_by_full_name(name)
    if not results:
        raise ValueError(f"Player not found: {name}")
    active = [player for player in results if player["is_active"]]
    player = active[0] if active else results[0]
    return player["id"], player["full_name"]


def _prepare_game_log_frame(df, season_type):
    df = df.copy()
    df["MIN_FLOAT"] = df["MIN"].apply(parse_minutes)
    df["2PM"] = df["FGM"] - df["FG3M"]
    df["2PA"] = df["FGA"] - df["FG3A"]
    df["GAME_DATE_DT"] = df["GAME_DATE"].apply(
        lambda d: datetime.strptime(d, "%b %d, %Y")
    )
    df["SEASON_TYPE"] = season_type
    return df


def _dedupe_game_log(df):
    if "Game_ID" in df.columns:
        subset = ["Game_ID"]
    elif "GAME_ID" in df.columns:
        subset = ["GAME_ID"]
    else:
        subset = [col for col in ("GAME_DATE", "MATCHUP") if col in df.columns]

    if subset:
        df = df.drop_duplicates(subset=subset, keep="first")
    else:
        df = df.drop_duplicates(keep="first")

    return df


def get_game_log(
    player_id,
    season=CURRENT_SEASON,
    retries=3,
    limit=None,
    season_types=None,
):
    season_types = tuple(season_types or DEFAULT_GAME_LOG_SEASON_TYPES)

    for attempt in range(retries):
        try:
            frames = []
            for season_type in season_types:
                time.sleep(1)
                log = playergamelog.PlayerGameLog(
                    player_id=player_id,
                    season=season,
                    season_type_all_star=season_type,
                    timeout=30,
                )
                df = log.get_data_frames()[0]
                if df.empty:
                    continue
                frames.append(_prepare_game_log_frame(df, season_type))

            if frames:
                df = pd.concat(frames, ignore_index=True)
                df = df.sort_values("GAME_DATE_DT", ascending=False)
                df = _dedupe_game_log(df).reset_index(drop=True)
                if limit is not None:
                    return df.head(limit).reset_index(drop=True)
                return df
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise ValueError(f"Failed to fetch game log: {exc}")
    raise ValueError("No game data found")


def compute_game_total(row, stat_config):
    stat_type = stat_config["type"]
    if stat_type == "simple":
        return sum(float(row[key]) for key in stat_config["keys"])
    if stat_type == "derived":
        formula = stat_config["formula"]
        if formula == "2PM":
            return float(row["FGM"]) - float(row["FG3M"])
        if formula == "2PA":
            return float(row["FGA"]) - float(row["FG3A"])
    if stat_type == "fantasy":
        return sum(
            float(row[key]) * FANTASY_WEIGHTS.get(key, 1.0)
            for key in stat_config["keys"]
        )
    return 0.0
