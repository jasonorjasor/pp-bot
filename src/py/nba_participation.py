"""NBA participation evidence and inferred (never platform-confirmed) settlement."""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections import Counter
from zoneinfo import ZoneInfo

from prop_history import LEAGUE_TIMEZONE, aware_datetime, canonical_json, event_hash, finite_number
from prop_utils import STAT_MAP

CACHE_VERSION = 1
POLICY_VERSION = "nba_reboot_guidance_2026-09-30"
POLICY_URL = "https://www.prizepicks.com/reboots"
COURT_ACTIONS = {"made shot", "missed shot", "2pt", "3pt", "rebound", "turnover", "steal",
                 "block", "free throw", "freethrow", "jump ball", "jumpball"}


def game_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,10}", str(value)):
        raise ValueError("invalid NBA game ID")
    return str(value).zfill(10)


def person_id(value):
    number = finite_number(value, "player/team ID", required=True)
    if number <= 0 or number != int(number):
        raise ValueError("player/team ID must be a positive integer")
    return int(number)


def clock_seconds(value):
    if value is None or value == "":
        return None
    raw = str(value)
    match = re.fullmatch(r"PT(?:(\d+)M)?(\d+(?:\.\d+)?)S", raw)
    if match:
        return int(match[1] or 0) * 60 + float(match[2])
    if re.fullmatch(r"\d+:\d+(?:\.\d+)?", raw):
        minutes, seconds = raw.split(":")
        if float(seconds) >= 60:
            raise ValueError("invalid minutes clock")
        return int(minutes) * 60 + float(seconds)
    raise ValueError("minutes must use an NBA clock format")


def validate_rotations(game):
    if game.get("gameStatus") != 3:
        raise ValueError("game is not finalized")
    period = person_id(game.get("period"))
    if period < 4 or period > 20:
        raise ValueError("invalid final NBA period")
    end = (2880 + (period - 4) * 300) * 10
    roster = {}
    for row in game.get("players", []):
        identity = person_id(row.get("personId"))
        if identity in roster:
            raise ValueError("duplicate roster player")
        seconds = finite_number(row.get("minutesSeconds"), "full-game minutes")
        if seconds is not None and (seconds < 0 or seconds * 10 > end + 11):
            raise ValueError("invalid full-game minutes")
        roster[identity] = {"teamId": person_id(row.get("teamId")), "seconds": seconds}
    teams = {row["teamId"] for row in roster.values()}
    if len(teams) != 2:
        raise ValueError("complete two-team roster required")
    intervals = {identity: [] for identity in roster}
    team_intervals = {identity: [] for identity in teams}
    for row in game.get("rotationRows", []):
        if game_id(row.get("GAME_ID")) != game_id(game["gameId"]):
            raise ValueError("rotation game ID mismatch")
        identity, team = person_id(row.get("PERSON_ID")), person_id(row.get("TEAM_ID"))
        if identity not in roster or roster[identity]["teamId"] != team:
            raise ValueError("rotation player/team not in roster")
        start = finite_number(row.get("IN_TIME_REAL"), "entry time", required=True)
        stop = finite_number(row.get("OUT_TIME_REAL"), "exit time", required=True)
        if not 0 <= start <= stop <= end:
            raise ValueError("rotation times outside finalized game")
        if start == stop:
            continue
        intervals[identity].append((start, stop))
        team_intervals[team].append((start, stop))
    for identity, rows in intervals.items():
        rows.sort()
        if any(rows[index][0] < rows[index - 1][1] for index in range(1, len(rows))):
            raise ValueError("overlapping player stints")
        seconds = sum(stop - start for start, stop in rows) / 10
        full_seconds = roster[identity]["seconds"]
        if full_seconds is None and rows:
            raise ValueError("positive rotation stints lack full box-score minutes")
        if full_seconds is not None and full_seconds > 0 and not rows:
            raise ValueError("positive full box-score minutes lack rotation stints")
        if full_seconds is not None and abs(seconds - full_seconds) > 1.1:
            raise ValueError("rotation minutes disagree with full box score")
    for rows in team_intervals.values():
        changes = {0: 0, end: 0}
        for start, stop in rows:
            changes[start] = changes.get(start, 0) + 1
            changes[stop] = changes.get(stop, 0) - 1
        count, previous = 0, 0
        for instant, delta in sorted(changes.items()):
            if instant > previous and count != 5:
                raise ValueError("incomplete five-player court coverage")
            count += delta
            previous = instant
        if count != 0:
            raise ValueError("unclosed rotation coverage")
    return roster, intervals, end


def court_evidence(game, identity):
    periods = set()
    final_period = game.get("period", 0)
    for action in game.get("actions", []):
        try:
            player = person_id(action.get("personId"))
            period = person_id(action.get("period"))
        except ValueError:
            continue
        kind = str(action.get("actionType") or "").strip().casefold()
        if player == identity and kind in COURT_ACTIONS and 1 <= period <= final_period:
            # Bench technicals, ejections, and timeouts are not court participation.
            periods.add(period)
    return periods


def participation_from_game(game, identity, expected_minutes=None):
    identity = person_id(identity)
    evidence = {"version": CACHE_VERSION, "nbaGameId": game_id(game["gameId"]), "nbaPlayerId": identity,
        "source": "nba_game_rotation", "fetchedAt": game.get("fetchedAt"),
        "sourceHash": event_hash(game), "sources": game.get("sources", []),
        "complete": False, "playedFirstHalf": None, "playedSecondHalf": None,
        "playedOvertime": None, "returnedAfterHalftime": None, "didNotPlay": None}
    if game.get("gameStatus") != 3:
        return {**evidence, "reason": "game_not_final"}
    try:
        roster, intervals, end = validate_rotations(game)
        if identity not in roster:
            raise ValueError("player missing from full roster")
        if roster[identity]["seconds"] is None:
            raise ValueError("queried player's full box-score participation is missing")
        if expected_minutes is not None:
            expected_minutes = finite_number(expected_minutes, "matched game-log minutes", required=True)
            if (expected_minutes < 0 or (expected_minutes > 0 and roster[identity]["seconds"] == 0)
                    or abs(roster[identity]["seconds"] / 60 - expected_minutes) > 1.0):
                raise ValueError("box-score minutes disagree with matched player game log")
        rows = intervals[identity]
        before = sum(max(0, min(stop, 14400) - start) for start, stop in rows if start < 14400) / 10
        after = sum(max(0, min(stop, 28800) - max(start, 14400)) for start, stop in rows) / 10
        overtime = sum(max(0, stop - max(start, 28800)) for start, stop in rows) / 10
        periods = court_evidence(game, identity)
        if (any(p >= 3 for p in periods) and after + overtime == 0) or (any(p <= 2 for p in periods) and before == 0):
            raise ValueError("rotation and play-by-play evidence disagree")
        return {**evidence, "complete": True, "reason": "validated_full_game_rotations",
            "playedFirstHalf": before > 0, "playedSecondHalf": after > 0, "playedOvertime": overtime > 0,
            "returnedAfterHalftime": after + overtime > 0, "didNotPlay": before + after + overtime == 0,
            "firstHalfMinutes": round(before / 60, 4), "secondHalfMinutes": round(after / 60, 4),
            "overtimeMinutes": round(overtime / 60, 4), "totalMinutes": round((before + after + overtime) / 60, 4),
            "lastExitSeconds": max((stop / 10 for _, stop in rows), default=None)}
    except (ValueError, KeyError, TypeError) as exc:
        periods = court_evidence(game, identity)
        return {**evidence, "source": "nba_play_by_play_positive_evidence" if periods else "nba_participation_unavailable",
            "reason": "participation_incomplete", "details": str(exc),
            "playedFirstHalf": True if any(p <= 2 for p in periods) else None,
            "playedSecondHalf": True if any(p in (3, 4) for p in periods) else None,
            "playedOvertime": True if any(p > 4 for p in periods) else None,
            "returnedAfterHalftime": True if any(p >= 3 for p in periods) else None,
            "observedPeriods": sorted(periods)}


def assess_settlement(record, legacy_market_scope=None):
    alert = record.get("alert") or {}
    participation = record.get("participation") or {}
    proven_dnp = participation.get("complete") is True and participation.get("didNotPlay") is True
    scope = alert.get("marketScope") or legacy_market_scope
    league = alert.get("league") or ("NBA" if legacy_market_scope else None)
    base = {"policyVersion": POLICY_VERSION, "policyUrl": POLICY_URL,
        "platformVerified": False, "scopeSource": alert.get("marketScopeSource") or
        ("explicit_legacy_override" if legacy_market_scope else None)}

    def decision(result, status, reason):
        return {**base, "result": result, "status": status, "reason": reason}

    result = record.get("result")
    if result == "unresolved" and not proven_dnp:
        return decision("unresolved", "needs_review", record.get("errorCode") or "box_score_unresolved")
    if league != "NBA" or scope is None:
        return decision("unresolved", "needs_review", "market_scope_unknown")
    if scope != "full_game":
        return decision("unresolved", "needs_review", "unsupported_market_scope")
    if alert.get("statType") not in STAT_MAP or alert.get("recommendedSide") not in ("over", "under"):
        return decision("unresolved", "needs_review", "unsupported_market")
    if proven_dnp:
        return decision("void", "inferred", "nba_dnp")
    if record.get("finalMinutes") is None or record["finalMinutes"] <= 0:
        return decision("unresolved", "needs_review", "participation_incomplete")
    if alert.get("recommendedSide") == "under":
        return decision(result, "inferred", "less_not_rebooted")
    if result == "win":
        return decision(result, "inferred", "more_already_won")
    if result == "push":
        return decision(result, "inferred", "tied_projection")
    if participation.get("returnedAfterHalftime") is True:
        return decision(result, "inferred", "returned_after_halftime")
    if participation.get("complete") is True and participation.get("playedFirstHalf") is True and participation.get("returnedAfterHalftime") is False:
        return decision("void", "inferred", "nba_reboot")
    return decision("unresolved", "needs_review", participation.get("reason") or "participation_data_unavailable")


def settlement_summary(records):
    results, statuses, reasons = Counter(), Counter(), Counter()
    for record in records:
        assessment = record.get("settlement") or {}
        status = assessment.get("status") or "legacy_unassessed"
        statuses[status] += 1
        reasons[assessment.get("reason") or "legacy_unassessed"] += 1
        results[assessment.get("result") if status == "inferred" else "unresolved"] += 1
    countable = results["win"] + results["loss"]
    return {"basis": "inferred NBA participation and published platform rules", "platformVerified": False,
        "gradedCount": len(records), **{key: results[key] for key in ("win", "loss", "push", "void", "unresolved")},
        "countable": countable, "winRate": round(results["win"] / countable * 100, 1) if countable else None,
        "statuses": dict(statuses), "reasons": dict(reasons)}


class ParticipationProvider:
    def __init__(self, cache_dir=None, offline_data=None, *, timeout=6, attempts=2, log=None):
        if offline_data is not None and (not isinstance(offline_data, dict) or
                not isinstance(offline_data.get("games", {}), dict)):
            raise ValueError("offline participation must be an object with a games object")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.offline_data = offline_data
        self.timeout, self.attempts, self.log = timeout, attempts, log or (lambda message: None)
        self.games = {}
        self.fetches = 0

    def request(self, cls, identity):
        for attempt in range(self.attempts):
            try:
                self.log(f"Participation: {cls.__name__} {identity} attempt {attempt + 1}/{self.attempts}")
                return cls(game_id=identity, timeout=self.timeout).get_dict()
            except Exception:
                if attempt + 1 == self.attempts:
                    raise
                time.sleep(0.25)

    def fetch_game(self, identity):
        from nba_api.stats.endpoints import boxscoresummaryv3, boxscoretraditionalv3, gamerotation, playbyplayv3
        from nba_api.stats.library.http import NBAStatsResponse
        sources, errors = [], []

        def endpoint(cls):
            raw = self.request(cls, identity)
            sources.append({"endpoint": cls.__name__, "sha256": event_hash(raw)})
            parsed = cls(game_id=identity, get_request=False)
            parsed.nba_response = NBAStatsResponse(response=canonical_json(raw), status_code=200,
                url="https://stats.nba.com/stats/" + cls.endpoint)
            parsed.load_response()
            return parsed

        try:
            summary = endpoint(boxscoresummaryv3.BoxScoreSummaryV3).game_summary.get_data_frame().iloc[0].to_dict()
            if game_id(summary["gameId"]) != identity:
                raise ValueError("game summary ID mismatch")
            game = {"gameId": identity, "gameStatus": int(summary["gameStatus"]), "period": int(summary["period"]),
                "fetchedAt": datetime.now(UTC).isoformat(), "players": [], "rotationRows": [],
                "actions": [], "sources": sources, "errors": errors}
            game["gameDate"] = aware_datetime(summary["gameTimeUTC"]).astimezone(ZoneInfo(LEAGUE_TIMEZONE)).date().isoformat()
            if game["gameStatus"] != 3:
                return game
            box = endpoint(boxscoretraditionalv3.BoxScoreTraditionalV3).player_stats.get_data_frame()
            for _, row in box.iterrows():
                if game_id(row["gameId"]) != identity:
                    raise ValueError("box-score game ID mismatch")
                seconds = clock_seconds(row.get("minutes"))
                comment = str(row.get("comment") or "")
                if seconds is None and re.search(r"DNP|DND|INACTIVE|DID NOT", comment, re.IGNORECASE):
                    seconds = 0.0
                game["players"].append({"personId": person_id(row["personId"]),
                    "teamId": person_id(row["teamId"]), "teamTricode": str(row["teamTricode"]),
                    "minutesSeconds": seconds})
            try:
                rotation = endpoint(gamerotation.GameRotation)
                game["rotationRows"] = [row for frame in rotation.get_data_frames()
                    for row in frame.astype(object).where(frame.notna(), None).to_dict("records")]
                validate_rotations(game)
            except Exception as exc:
                errors.append({"source": "GameRotation", "error": str(exc)[:200]})
                try:
                    frame = endpoint(playbyplayv3.PlayByPlayV3).play_by_play.get_data_frame()
                    if not frame.empty and any(game_id(value) != identity for value in frame["gameId"]):
                        raise ValueError("play-by-play game ID mismatch")
                    game["actions"] = frame.astype(object).where(frame.notna(), None).to_dict("records")
                except Exception as fallback:
                    errors.append({"source": "PlayByPlayV3", "error": str(fallback)[:200]})
            return game
        except Exception as exc:
            return {"gameId": identity, "gameStatus": None, "period": 0, "players": [],
                "rotationRows": [], "actions": [], "sources": sources,
                "errors": [{"source": "NBA", "error": str(exc)[:200]}],
                "fetchedAt": datetime.now(UTC).isoformat()}

    def get_game(self, identity):
        identity = game_id(identity)
        if identity in self.games:
            return self.games[identity]
        if self.offline_data is not None:
            game = (self.offline_data.get("games") or {}).get(identity)
            if game is None:
                game = {"gameId": identity, "gameStatus": None, "period": 0,
                    "reason": "offline_participation_missing"}
            if game_id(game.get("gameId")) != identity:
                raise ValueError("offline participation game ID mismatch")
        else:
            path = self.cache_dir / f"{identity}.json" if self.cache_dir else None
            game = None
            if path and path.exists():
                try:
                    cached = json.loads(path.read_text(encoding="utf-8-sig"))
                    ttl = 24 if cached.get("complete") else 0.25
                    age = datetime.now(UTC) - aware_datetime(cached["fetchedAt"])
                    if cached.get("version") == CACHE_VERSION and game_id(cached["game"]["gameId"]) == identity and timedelta(0) <= age < timedelta(hours=ttl):
                        game = cached["game"]
                except (ValueError, KeyError, TypeError):
                    pass
            if game is None:
                self.fetches += 1
                game = self.fetch_game(identity)
                if path and game.get("gameStatus") == 3:
                    try:
                        validate_rotations(game)
                        complete = True
                    except (ValueError, TypeError, KeyError):
                        complete = False
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(canonical_json({"version": CACHE_VERSION, "complete": complete,
                        "fetchedAt": datetime.now(UTC).isoformat(), "game": game}), encoding="utf-8")
                    temporary.replace(path)
        self.games[identity] = game
        return game

    def get(self, identity, player, expected_minutes=None):
        try:
            game = self.get_game(identity)
            evidence = participation_from_game(game, player, expected_minutes)
            if game.get("gameStatus") is None:
                evidence["reason"] = "participation_data_unavailable"
                evidence["errors"] = game.get("errors", [])
            return evidence
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return {"version": CACHE_VERSION, "complete": False,
                "reason": "participation_data_unavailable", "details": str(exc)}
