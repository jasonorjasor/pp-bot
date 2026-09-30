import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/py"))
from nba_participation import (
    ParticipationProvider, assess_settlement, clock_seconds, participation_from_game,
    settlement_summary, validate_rotations,
)
from grade_props import attach_settlement, filter_pending_alerts, should_append_record, run_grading
from prop_history import canonical_json
from props_sql import import_sources, open_database
from test_history_grading import grade, posted, jsonl

GAME = "0022500001"


def rotation_game(stints=None, period=4):
    """Two complete teams; player 123 and replacement 124 split one court slot."""
    stints = [(0, 10000)] if stints is None else stints
    end = 28800 + (period - 4) * 3000
    complement, cursor = [], 0
    for start, stop in stints:
        if cursor < start:
            complement.append((cursor, start))
        cursor = stop
    if cursor < end:
        complement.append((cursor, end))
    intervals = {123: stints, 124: complement, 125: []}
    intervals.update({identity: [(0, end)] for identity in (101, 102, 103, 104, 201, 202, 203, 204, 205)})
    players, rows = [], []
    for identity, shifts in intervals.items():
        team = 1 if identity < 200 else 2
        players.append({"personId": identity, "teamId": team,
                        "minutesSeconds": sum(stop - start for start, stop in shifts) / 10})
        rows.extend({"GAME_ID": GAME, "PERSON_ID": identity, "TEAM_ID": team,
                     "IN_TIME_REAL": start, "OUT_TIME_REAL": stop} for start, stop in shifts)
    return {"gameId": GAME, "gameStatus": 3, "period": period, "players": players,
            "rotationRows": rows, "actions": [], "fetchedAt": datetime.now(UTC).isoformat(), "sources": []}


def evaluated(game=None, **changes):
    game = rotation_game() if game is None else game
    evidence = participation_from_game(game, 123)
    record = grade(result="loss", finalValue=10, finalMinutes=evidence.get("totalMinutes", 17),
        nbaGameId=GAME, nbaPlayerId=123,
        alert=posted(league="NBA", marketScope="full_game", marketScopeSource="test"),
        participation=evidence)
    record.update(changes)
    return record


class ParticipationEvidenceTests(unittest.TestCase):
    def test_complete_rotations_establish_first_half_only(self):
        evidence = participation_from_game(rotation_game(), 123)
        self.assertTrue(evidence["complete"])
        self.assertTrue(evidence["playedFirstHalf"])
        self.assertFalse(evidence["playedSecondHalf"])
        self.assertFalse(evidence["returnedAfterHalftime"])
        self.assertEqual(evidence["lastExitSeconds"], 1000)

    def test_one_second_return_after_halftime_prevents_reboot(self):
        evidence = participation_from_game(rotation_game([(0, 10000), (14400, 14410)]), 123)
        self.assertTrue(evidence["playedSecondHalf"])
        self.assertTrue(evidence["returnedAfterHalftime"])
        self.assertGreater(evidence["secondHalfMinutes"], 0)

    def test_halftime_boundary_is_not_second_half_participation(self):
        evidence = participation_from_game(rotation_game([(0, 14400)]), 123)
        self.assertEqual(evidence["firstHalfMinutes"], 24)
        self.assertFalse(evidence["playedSecondHalf"])

    def test_overtime_return_is_detected(self):
        evidence = participation_from_game(rotation_game([(0, 10000), (28800, 28810)], period=5), 123)
        self.assertFalse(evidence["playedSecondHalf"])
        self.assertTrue(evidence["playedOvertime"])
        self.assertTrue(evidence["returnedAfterHalftime"])

    def test_validated_zero_participation_can_establish_dnp(self):
        self.assertTrue(participation_from_game(rotation_game(), 125)["didNotPlay"])

    def test_one_second_of_box_score_minutes_without_stints_is_not_dnp(self):
        game = rotation_game()
        next(row for row in game["players"] if row["personId"] == 125)["minutesSeconds"] = 1
        self.assertIsNone(participation_from_game(game, 125)["didNotPlay"])

    def test_missing_player_is_unknown_not_dnp(self):
        evidence = participation_from_game(rotation_game(), 999)
        self.assertFalse(evidence["complete"])
        self.assertIsNone(evidence["didNotPlay"])

    def test_missing_minutes_for_unused_bench_row_does_not_invalidate_other_players(self):
        game = rotation_game()
        next(row for row in game["players"] if row["personId"] == 125)["minutesSeconds"] = None
        self.assertTrue(participation_from_game(game, 123)["complete"])
        self.assertIsNone(participation_from_game(game, 125)["didNotPlay"])

    def test_nonfinal_game_is_unknown(self):
        game = rotation_game()
        game["gameStatus"] = 2
        self.assertEqual(participation_from_game(game, 123)["reason"], "game_not_final")

    def test_truncated_or_duplicate_rotations_cannot_establish_absence(self):
        for modification in ("truncated", "duplicate", "wrong_game", "wrong_team", "seconds_units", "missing_roster"):
            with self.subTest(modification=modification):
                game = rotation_game()
                if modification == "truncated":
                    game["rotationRows"].pop()
                elif modification == "duplicate":
                    game["rotationRows"].append(copy.deepcopy(game["rotationRows"][0]))
                elif modification == "wrong_game":
                    game["rotationRows"][0]["GAME_ID"] = "0022500002"
                elif modification == "wrong_team":
                    game["rotationRows"][0]["TEAM_ID"] = 2
                elif modification == "seconds_units":
                    for row in game["rotationRows"]:
                        row["IN_TIME_REAL"] /= 10
                        row["OUT_TIME_REAL"] /= 10
                else:
                    game["players"].pop()
                evidence = participation_from_game(game, 123)
                self.assertFalse(evidence["complete"])
                self.assertIsNone(evidence["returnedAfterHalftime"])

    def test_rotation_minutes_must_reconcile_with_box_score_and_log(self):
        game = rotation_game()
        game["players"][0]["minutesSeconds"] += 10
        self.assertFalse(participation_from_game(game, 123)["complete"])
        self.assertFalse(participation_from_game(rotation_game(), 123, expected_minutes=30)["complete"])
        self.assertIsNone(participation_from_game(rotation_game(), 125, expected_minutes=0.5)["didNotPlay"])

    def test_sparse_play_by_play_proves_presence_but_not_absence(self):
        game = rotation_game()
        game["rotationRows"] = []
        game["actions"] = [{"period": 1, "personId": 123, "actionType": "Made Shot"}]
        evidence = participation_from_game(game, 123)
        self.assertTrue(evidence["playedFirstHalf"])
        self.assertIsNone(evidence["playedSecondHalf"])
        game["actions"].append({"period": 3, "personId": 123, "actionType": "Rebound"})
        self.assertTrue(participation_from_game(game, 123)["returnedAfterHalftime"])

    def test_bench_technical_ejection_and_substitution_are_not_court_proof(self):
        game = rotation_game()
        game["rotationRows"] = []
        game["actions"] = [{"period": 3, "personId": 123, "actionType": kind}
                           for kind in ("Foul", "Technical Foul", "Ejection", "Timeout", "Substitution")]
        self.assertIsNone(participation_from_game(game, 123)["returnedAfterHalftime"])

    def test_conflicting_rotation_and_play_by_play_is_not_complete(self):
        game = rotation_game()
        game["actions"] = [{"period": 3, "personId": 123, "actionType": "Made Shot"}]
        evidence = participation_from_game(game, 123)
        self.assertFalse(evidence["complete"])
        self.assertTrue(evidence["returnedAfterHalftime"])

    def test_minutes_parser_accepts_seconds_and_iso_clocks(self):
        self.assertEqual(clock_seconds("25:01"), 1501)
        self.assertEqual(clock_seconds("PT00M01.00S"), 1)
        self.assertIsNone(clock_seconds(None))
        with self.assertRaises(ValueError):
            clock_seconds("12:90")


class SettlementAssessmentTests(unittest.TestCase):
    def test_attempt_based_or_unknown_market_cannot_be_inferred(self):
        for changes in ({"statType": "Points in First 3 Attempts"}, {"recommendedSide": "unknown"}):
            alert = posted(league="NBA", marketScope="full_game", **changes)
            self.assertEqual(assess_settlement(evaluated(alert=alert))["reason"], "unsupported_market")

    def test_dnp_requires_complete_evidence(self):
        record = evaluated(result="unresolved", participation={"complete": False, "didNotPlay": True})
        self.assertEqual(assess_settlement(record)["status"], "needs_review")

    def test_eligible_more_loss_is_inferred_reboot_and_raw_loss_is_retained(self):
        record = evaluated()
        settlement = assess_settlement(record)
        self.assertEqual((settlement["result"], settlement["reason"]), ("void", "nba_reboot"))
        self.assertFalse(settlement["platformVerified"])
        self.assertEqual(record["result"], "loss")

    def test_less_already_winning_more_and_ties_are_not_reboots(self):
        under = evaluated(alert=posted(league="NBA", marketScope="full_game", recommendedSide="under"), result="win")
        for record, expected in ((under, "less_not_rebooted"),
                                 (evaluated(result="win"), "more_already_won"),
                                 (evaluated(result="push"), "tied_projection")):
            with self.subTest(expected=expected):
                self.assertEqual(assess_settlement(record)["reason"], expected)
                self.assertNotEqual(assess_settlement(record)["result"], "void")

    def test_second_half_and_overtime_returns_keep_the_loss(self):
        for game in (rotation_game([(0, 10000), (14400, 14410)]),
                     rotation_game([(0, 10000), (28800, 28810)], period=5)):
            self.assertEqual(assess_settlement(evaluated(game))["result"], "loss")

    def test_partial_unknown_and_other_leagues_are_never_automatic_reboots(self):
        for alert in (posted(), posted(league="NBA", marketScope="first_half"),
                      posted(league="WNBA", marketScope="full_game")):
            with self.subTest(alert=alert):
                self.assertEqual(assess_settlement(evaluated(alert=alert))["status"], "needs_review")

    def test_legacy_override_is_explicit_and_never_overrides_partial_game(self):
        self.assertEqual(assess_settlement(evaluated(alert=posted()), "full_game")["result"], "void")
        half = evaluated(alert=posted(league="NBA", marketScope="first_half"))
        self.assertEqual(assess_settlement(half, "full_game")["result"], "unresolved")

    def test_missing_participation_for_more_loss_remains_unresolved(self):
        record = evaluated(participation={"complete": False, "reason": "participation_data_unavailable"})
        self.assertEqual(assess_settlement(record)["result"], "unresolved")

    def test_proven_dnp_has_separate_inferred_void(self):
        record = evaluated(result="unresolved", finalMinutes=0,
            participation=participation_from_game(rotation_game(), 125))
        self.assertEqual(assess_settlement(record)["reason"], "nba_dnp")

    def test_summary_excludes_inferred_voids_reviews_and_legacy_from_win_loss_rate(self):
        records = [evaluated(), evaluated(result="win"), grade("legacy"),
                   evaluated(participation={})]
        for record in (records[0], records[1], records[3]):
            record["settlement"] = assess_settlement(record)
        summary = settlement_summary(records)
        self.assertEqual((summary["countable"], summary["void"], summary["winRate"]), (1, 1, 100.0))
        self.assertEqual(summary["statuses"]["legacy_unassessed"], 1)


class ParticipationProviderTests(unittest.TestCase):
    def test_invalid_offline_container_fails_clearly(self):
        for data in ([], {"games": []}):
            with self.assertRaisesRegex(ValueError, "games object"):
                ParticipationProvider(offline_data=data)

    def test_missing_game_log_can_establish_dnp_only_with_verified_supplied_identity(self):
        for failure in (None, "date", "opponent", "absent", "participation", "no_id"):
            with self.subTest(failure=failure):
                game = rotation_game()
                game["gameDate"] = "2026-03-11"
                for player in game["players"]:
                    player["teamTricode"] = "LAL" if player["teamId"] == 1 else "DAL"
                alert = posted(league="NBA", marketScope="full_game", nbaGameId=GAME)
                player = 125
                if failure == "date":
                    game["gameDate"] = "2026-03-10"
                elif failure == "opponent":
                    alert["game"] = "SAS"
                elif failure == "absent":
                    player = 999
                elif failure == "participation":
                    game["rotationRows"] = []
                elif failure == "no_id":
                    del alert["nbaGameId"]
                record = grade(result="unresolved", errorCode="no_game_rows", alert=alert)
                provider = ParticipationProvider(offline_data={"games": {GAME: game}})
                attach_settlement(record, provider, fetch_result={"ok": True, "playerId": player})
                self.assertEqual(record["result"], "unresolved")
                self.assertEqual(record["settlement"]["result"], "void" if failure is None else "unresolved")
                if failure is None:
                    self.assertEqual(record["matchMethod"], "verified_roster_dnp")

    def test_proven_dnp_is_terminal_until_explicit_regrade(self):
        record = evaluated(result="unresolved", participation=participation_from_game(rotation_game(), 125))
        record["settlement"] = assess_settlement(record)
        alert = posted(startTime=(datetime.now(UTC) - timedelta(hours=8)).isoformat())
        self.assertEqual(filter_pending_alerts([alert], {"a": record}), [])
        self.assertEqual(filter_pending_alerts([alert], {"a": record}, regrade=True), [alert])

    def test_dnp_fallback_io_failure_remains_unresolved(self):
        record = grade(result="unresolved", errorCode="no_game_rows",
            alert=posted(league="NBA", marketScope="full_game", nbaGameId=GAME))
        with patch.object(ParticipationProvider, "get_game", side_effect=OSError("cache unavailable")):
            attach_settlement(record, ParticipationProvider(), fetch_result={"ok": True, "playerId": 125})
        self.assertEqual(record["settlement"]["status"], "needs_review")

    def test_regrade_does_not_accept_loss_of_previously_supported_assessment(self):
        record = evaluated()
        record["settlement"] = assess_settlement(record)
        args = SimpleNamespace(all_history=True, lookback_days=2, settlement_delay_hours=2,
            regrade=True, dry_run=True, output_dir=None, legacy_market_scope=None)
        logs = {"players": {"Test Player|2025-26": {"playerId": 123,
            "games": [{"GAME_ID": GAME, "GAME_DATE": "2026-03-11", "MATCHUP": "LAL vs. DAL",
                       "MIN_FLOAT": 1000 / 60, "PTS": 10}]}}}
        accepted, proposals, summary, comparison = run_grading([record["alert"]], [record], args, logs,
            ParticipationProvider(offline_data={}))
        self.assertEqual(accepted, [])
        self.assertEqual(proposals[0]["settlement"]["status"], "needs_review")
        self.assertEqual(comparison["counts"], {"needs_review": 1})
        self.assertEqual(summary["overall"]["settlementLevel"]["alertLevel"]["void"], 1)

    def test_recap_renders_inferred_counts_without_a_live_discord_call(self):
        record = evaluated()
        record["settlement"] = assess_settlement(record)
        from grade_props import build_report_views
        summary = build_report_views([record])
        script = "const {buildRecapEmbed}=require('./src/js/post_recap.js');const s=JSON.parse(process.argv[1]);delete s.primaryGameDate;delete s.gameDates;process.stdout.write(JSON.stringify(buildRecapEmbed(s).toJSON()));"
        result = subprocess.run(["node", "-e", script, json.dumps(summary)], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", check=True)
        embed = json.loads(result.stdout.splitlines()[-1])
        field = next(row for row in embed["fields"] if row["name"] == "Inferred settlement assessment")
        self.assertIn("Reboots: **1**", field["value"])
        self.assertIn("Voids: **1**", field["value"])
        self.assertNotIn("undefined", field["value"])

    def test_multiple_players_share_one_game_fetch_and_persistent_cache(self):
        with tempfile.TemporaryDirectory() as name:
            with patch.object(ParticipationProvider, "fetch_game", return_value=rotation_game()) as fetch:
                provider = ParticipationProvider(name)
                self.assertTrue(provider.get(GAME, 123)["complete"])
                self.assertTrue(provider.get(GAME, 124)["complete"])
                self.assertEqual(fetch.call_count, 1)
                second = ParticipationProvider(name)
                self.assertTrue(second.get(GAME, 123)["complete"])
                self.assertEqual(fetch.call_count, 1)

    def test_stale_and_corrupt_cache_are_refetched(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / f"{GAME}.json"
            for content in ("{bad}", canonical_json({"version": 1, "complete": True,
                    "fetchedAt": (datetime.now(UTC) - timedelta(days=2)).isoformat(), "game": rotation_game()})):
                path.write_text(content, encoding="utf-8")
                with patch.object(ParticipationProvider, "fetch_game", return_value=rotation_game()) as fetch:
                    self.assertTrue(ParticipationProvider(name).get(GAME, 123)["complete"])
                    self.assertEqual(fetch.call_count, 1)

    def test_offline_missing_data_never_fetches(self):
        with patch.object(ParticipationProvider, "fetch_game") as fetch:
            evidence = ParticipationProvider(offline_data={"games": {}}).get(GAME, 123)
            self.assertEqual(evidence["reason"], "participation_data_unavailable")
            fetch.assert_not_called()

    def test_fetch_errors_are_unknown_and_shared_in_memory(self):
        broken = {"gameId": GAME, "gameStatus": None, "period": 0, "errors": [{"error": "timeout"}]}
        with patch.object(ParticipationProvider, "fetch_game", return_value=broken) as fetch:
            provider = ParticipationProvider()
            self.assertIsNone(provider.get(GAME, 123).get("returnedAfterHalftime"))
            provider.get(GAME, 124)
            self.assertEqual(fetch.call_count, 1)

    def test_request_retries_transient_failures(self):
        class Endpoint:
            calls = 0
            def __init__(self, **kwargs):
                Endpoint.calls += 1
                if Endpoint.calls == 1:
                    raise TimeoutError("temporary")
            def get_dict(self):
                return {"ok": True}
        with patch("nba_participation.time.sleep"):
            self.assertEqual(ParticipationProvider().request(Endpoint, GAME), {"ok": True})
        self.assertEqual(Endpoint.calls, 2)

    def test_zero_participation_and_missing_identity_attach_without_mutating_raw_result(self):
        record = evaluated(result="unresolved", finalMinutes=0, nbaPlayerId=125)
        provider = ParticipationProvider(offline_data={"games": {GAME: rotation_game()}})
        attach_settlement(record, provider)
        self.assertEqual(record["settlement"]["result"], "void")
        self.assertEqual(record["result"], "unresolved")
        missing = grade()
        attach_settlement(missing, provider)
        self.assertEqual(missing["settlement"]["status"], "needs_review")

    def test_pending_assessment_is_retried_and_identical_decisions_do_not_append(self):
        record = evaluated()
        record["settlement"] = {"status": "needs_review", "result": "unresolved",
                                "reason": "participation_data_unavailable"}
        alert = posted(startTime=(datetime.now(UTC) - timedelta(hours=8)).isoformat())
        self.assertEqual(filter_pending_alerts([alert], {"a": record}), [alert])
        self.assertFalse(should_append_record("a", {"a": record}, copy.deepcopy(record)))
        updated = copy.deepcopy(record)
        updated["settlement"] = assess_settlement(updated)
        self.assertTrue(should_append_record("a", {"a": record}, updated))

    def test_dnp_assessment_can_update_an_unresolved_box_score_record(self):
        old = evaluated(result="unresolved", finalMinutes=0, errorCode="participation_unverified", notes="same")
        new = copy.deepcopy(old)
        new["settlement"] = {"status": "inferred", "result": "void", "reason": "nba_dnp"}
        self.assertTrue(should_append_record("a", {"a": old}, new))

    def test_offline_cli_and_sql_preserve_box_score_and_query_inferred_void(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            post, grades, logs, participation = (root / value for value in ("posted.jsonl", "grades.jsonl", "logs.json", "participation.json"))
            alert = posted(league="NBA", marketScope="full_game", marketScopeSource="fixture")
            jsonl(post, [alert])
            jsonl(grades, [])
            logs.write_text(json.dumps({"players": {"Test Player|2025-26": {"playerId": 123,
                "games": [{"GAME_ID": GAME, "GAME_DATE": "2026-03-11", "MATCHUP": "LAL vs. DAL",
                           "MIN_FLOAT": 1000 / 60, "PTS": 10}]}}}), encoding="utf-8")
            participation.write_text(canonical_json({"games": {GAME: rotation_game()}}), encoding="utf-8")
            originals = [path.read_bytes() for path in (post, grades, logs, participation)]
            output = root / "output"
            result = subprocess.run(["node", str(ROOT / "src/js/grade_props.js"), "--posted", str(post),
                "--grades", str(grades), "--all-history", "--dry-run", "--output-dir", str(output),
                "--game-logs", str(logs), "--participation-data", str(participation)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((output / "acceptedGrades.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["result"], "loss")
            self.assertEqual(record["settlement"]["result"], "void")
            summary = json.loads((output / "gradingSummary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["batch"]["settlementLevel"]["uniqueLineLevel"]["void"], 1)
            self.assertEqual(originals, [path.read_bytes() for path in (post, grades, logs, participation)])
            connection = open_database(root / "analytics.sqlite3")
            try:
                import_sources(connection, [post], [output / "acceptedGrades.jsonl"])
                self.assertEqual(connection.execute("SELECT result FROM latest_grades").fetchone()[0], "loss")
                rows = connection.execute((ROOT / "sql/settlement_summary.sql").read_text(encoding="utf-8")).fetchall()
                self.assertEqual(rows, [("inferred", "void", "nba_reboot", 1)])
            finally:
                connection.close()
