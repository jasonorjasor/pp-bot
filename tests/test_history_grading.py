import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/py"))
from grade_props import grade_alert, match_game_row
from prop_history import alert_id, dedupe_observations, latest_grades, load_grade_history, source_paths
from projection_report import summarize_records, choose_probability, get_projection_side, stable_representatives, select_records, write_calibration_artifact
from types import SimpleNamespace
from projection_confrontation_report import summarize_records as confront


def posted(identity="a", **changes):
    record = {"alertId": identity, "propId": "prop", "playerName": "Test Player", "statType": "Points",
        "line": 20.5, "recommendedSide": "over", "postedAt": "2026-03-11T20:00:00Z",
        "startTime": "2026-03-12T02:00:00Z", "game": "DAL", "tier": "watchlist", "score": 7.0,
        "analytics": {"projectionMethod": "test", "projectionConfidenceBand": "stable",
            "pOverAdjusted": 0.6, "pUnderAdjusted": 0.4}}
    record.update(changes)
    return record


def grade(identity="a", **changes):
    record = {"alertId": identity, "gradedAt": "2026-03-12T05:00:00Z", "result": "win",
        "gameDate": "2026-03-11", "finalValue": 25, "alert": posted(identity)}
    record.update(changes)
    return record


def box(day="2026-03-11", matchup="LAL vs. DAL", game_id="0022500001", minutes=30, pts=25):
    return {"GAME_DATE_DT": pd.Timestamp(day), "GAME_ID": game_id, "MATCHUP": matchup, "MIN_FLOAT": minutes, "PTS": pts}


def jsonl(path, records):
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8-sig")


class GradingIdentityTests(unittest.TestCase):
    def evaluate(self, rows, alert=None):
        return grade_alert(alert or posted(), {"ok": True, "playerId": 123, "df": pd.DataFrame(rows)})

    def test_midnight_utc_uses_scheduled_league_date(self):
        result = self.evaluate([box(), box("2026-03-12", "LAL @ SAS", "0022500002", pts=10)])
        self.assertEqual((result["gameDate"], result["finalValue"], result["result"]), ("2026-03-11", 25, "win"))
        self.assertEqual(result["nbaGameId"], "0022500001")
        self.assertEqual(result["nbaPlayerId"], 123)
        self.assertEqual(result["gradingVersion"], 3)
        self.assertFalse(result["platformSettlementVerified"])

    def test_no_previous_day_wrong_opponent_fallback(self):
        result = self.evaluate([box("2026-03-10", "LAL @ SAS")])
        self.assertEqual((result["result"], result["errorCode"]), ("unresolved", "no_game_found"))

    def test_wrong_opponent_on_exact_date_is_unresolved(self):
        self.assertEqual(self.evaluate([box(matchup="LAL vs. SAS")])["result"], "unresolved")

    def test_repeated_opponent_on_adjacent_day_does_not_override_exact_date(self):
        result = self.evaluate([box("2026-03-10", pts=10), box()])
        self.assertEqual(result["finalValue"], 25)

    def test_repeated_rows_are_ambiguous(self):
        self.assertEqual(self.evaluate([box(), box()])["errorCode"], "ambiguous_match")

    def test_postponement_without_game_id_is_not_guessed(self):
        self.assertEqual(self.evaluate([box("2026-03-13")])["result"], "unresolved")

    def test_explicit_game_id_can_verify_rescheduled_game(self):
        result = self.evaluate([box("2026-03-13")], posted(nbaGameId="22500001"))
        self.assertEqual(result["gameDate"], "2026-03-13")
        self.assertEqual(result["matchMethod"], "nba_game_id")

    def test_id_does_not_bypass_opponent_validation(self):
        self.assertEqual(self.evaluate([box(matchup="LAL @ SAS")], posted(nbaGameId="0022500001"))["result"], "unresolved")

    def test_invalid_event_time_and_missing_opponent_are_unresolved(self):
        for changes, code in (({"startTime": "2026-03-11T19:00:00"}, "invalid_event_time"),
                              ({"game": None}, "missing_opponent"), ({"game": "garbage"}, "invalid_opponent"),
                              ({"nbaGameId": True}, "invalid_game_id")):
            with self.subTest(changes=changes):
                self.assertEqual(self.evaluate([box()], posted(**changes))["errorCode"], code)

    def test_missing_game_date_with_verified_id_is_unresolved(self):
        row = box()
        del row["GAME_DATE_DT"]
        self.assertEqual(self.evaluate([row], posted(nbaGameId="0022500001"))["errorCode"], "missing_game_date")

    def test_short_participation_is_not_automatic_void(self):
        result = self.evaluate([box(minutes=2, pts=1)])
        self.assertEqual(result["result"], "loss")
        self.assertEqual(result["settlementBasis"], "nba_box_score_comparison")

    def test_zero_or_missing_participation_is_unresolved(self):
        for minutes in (0, None, float("nan")):
            with self.subTest(minutes=minutes):
                self.assertEqual(self.evaluate([box(minutes=minutes)])["result"], "unresolved")

    def test_invalid_line_or_stats_do_not_settle(self):
        self.assertEqual(self.evaluate([box()], posted(line=float("nan")))["errorCode"], "invalid_alert")
        self.assertEqual(self.evaluate([box(pts=float("nan"))])["errorCode"], "missing_stat_data")


class HistoryReportTests(unittest.TestCase):
    def test_latest_uses_utc_chronology_and_deterministic_ties(self):
        older = grade(gradedAt="2026-03-12T10:00:00+03:00", result="loss")
        newer = grade(gradedAt="2026-03-12T08:00:00Z")
        self.assertEqual(latest_grades([newer, older])["a"], newer)
        tied = grade(gradedAt=newer["gradedAt"], result="loss")
        self.assertEqual(latest_grades([newer, tied]), latest_grades([tied, newer]))

    def test_population_keys_keep_line_moves_separate_and_choose_first_post(self):
        first = grade()
        later = grade("b", alert=posted("b", line=21.5, postedAt="2026-03-11T21:00:00Z"), result="loss")
        self.assertEqual(len(dedupe_observations([later, first])), 2)
        self.assertEqual(dedupe_observations([later, first], player_game=True), [first])
        next_day = grade("c", gameDate="2026-03-12", alert=posted("c"))
        self.assertEqual(len(dedupe_observations([first, next_day], player_game=True)), 2)

    def test_unknown_events_do_not_collapse_unrelated_alerts(self):
        rows = [grade(identity, gameDate=None, alert=posted(identity, startTime=None)) for identity in ("a", "b")]
        self.assertEqual(len(dedupe_observations(rows, player_game=True)), 2)

    def test_representative_is_chosen_before_confidence_eligibility(self):
        first = grade(alert=posted(analytics={}))
        later = grade("b", alert=posted("b", postedAt="2026-03-11T21:00:00Z"))
        self.assertEqual(stable_representatives(dedupe_observations([first, later], player_game=True)), [])

    def test_win_rate_uses_eligible_wins_only(self):
        rows = [grade(), grade("b", alert=posted("b", analytics={})), grade("c", result="loss", finalValue=10)]
        result = summarize_records(rows)
        self.assertEqual((result["win"], result["eligibleWins"], result["countable"], result["winRate"]), (2, 1, 2, 50.0))
        self.assertEqual(result["rawWinRate"], 66.7)

    def test_invalid_adjusted_probability_never_falls_back(self):
        for prob in (float("nan"), float("inf"), -0.1, 1.1, True, "bad"):
            with self.subTest(prob=prob):
                self.assertIsNone(choose_probability(posted(analytics={"pOverAdjusted": prob, "pOverFull": 0.6})))
        self.assertIsNone(get_projection_side({"pOverAdjusted": 0.6}))

    def test_legacy_fantasy_prediction_and_grade_versions_both_required(self):
        row = grade(fantasyScoringVersion=2, alert=posted(statType="Fantasy Score"))
        self.assertEqual(summarize_records([row])["countable"], 0)
        self.assertEqual(summarize_records([row], True)["countable"], 1)
        row["alert"]["analytics"]["fantasyScoringVersion"] = 2
        self.assertEqual(summarize_records([row])["countable"], 1)

    def test_confrontation_rates_use_identical_complete_pairs(self):
        rows = [grade(), grade("b", alert=posted("b", analytics={})),
                grade("c", finalValue=None), grade("d", result="loss", finalValue=10)]
        result = confront(rows)
        self.assertEqual((result["pairedPostedWins"], result["pairedPostedLosses"]), (1, 1))
        self.assertEqual((result["postedWinRate"], result["projectionWinRate"]), (50.0, 50.0))
        self.assertEqual(result["missingFinalValue"], 1)

    def test_confrontation_flags_inconsistent_label_and_ties_are_unpaired(self):
        bad = grade(result="loss", finalValue=25)
        tied = grade("b", alert=posted("b", analytics={"projectionMethod": "test", "projectionConfidenceBand": "stable", "pOverAdjusted": 0.5, "pUnderAdjusted": 0.5}))
        result = confront([bad, tied])
        self.assertEqual(result["inconsistentOutcome"], 1)
        self.assertIsNone(result["postedWinRate"])
        self.assertEqual(result["projectionPreferredTies"], 1)

    def test_history_bom_archives_and_conflicts(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "graded").mkdir()
            active = root / "active.jsonl"
            archive = root / "graded/old.jsonl"
            jsonl(active, [grade()])
            jsonl(archive, [grade(result="loss")])
            records, metadata = load_grade_history(source_paths(active, root))
            self.assertEqual(len(records), 1)
            self.assertEqual(metadata["equalTimestampResultConflicts"], 1)
            with self.assertRaises(FileNotFoundError):
                source_paths(root / "missing.jsonl")

    def test_latest_is_selected_before_game_date_filter(self):
        old = grade(gameDate="2026-03-10", gradedAt="2026-03-12T05:00:00Z")
        corrected = grade(gameDate="2026-03-11", gradedAt="2026-03-12T06:00:00Z", result="loss")
        args = SimpleNamespace(include_predeploy=True, post_deploy_only=False, game_date="2026-03-10", start_date=None, days=None)
        rows, counts = select_records(list(latest_grades([old, corrected]).values()), args, None)
        self.assertEqual(rows, [])
        self.assertEqual(counts["game_date"], 1)

    def test_report_outputs_cannot_replace_source_history(self):
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "custom_history.json"
            jsonl(source, [grade()])
            before = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "source history"):
                write_calibration_artifact(source, {"metadata": {"sources": [{"path": str(source)}]}})
            with self.assertRaisesRegex(ValueError, "protected"):
                write_calibration_artifact(Path(name) / "posted.jsonl", {})
            self.assertEqual(source.read_bytes(), before)

    def test_both_report_clis_include_archives_and_collapse_grade_retries(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "archive/graded").mkdir(parents=True)
            active = root / "active.jsonl"
            jsonl(active, [grade(gradedAt="2026-03-12T06:00:00Z", result="unresolved", finalValue=None)])
            jsonl(root / "archive/graded/old.jsonl", [grade(), grade("b")])
            before = active.read_bytes()
            for script, name in (("projection_report.py", "calibration"), ("projection_confrontation_report.py", "comparison")):
                output = root / f"{name}.json"
                result = subprocess.run([sys.executable, str(ROOT / "src/py" / script), "--grades", str(active),
                    "--archive-root", str(root / "archive"), "--all-history", "--include-predeploy", "--output-artifact", str(output)],
                    capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
                self.assertEqual(result.returncode, 0, result.stderr)
                artifact = json.loads(output.read_text())
                self.assertEqual(artifact["metadata"]["gradeEventsRead"], 3)
                self.assertEqual(artifact["metadata"]["latestAlerts"], 2)
                population = (artifact.get("populationSummaries") or artifact)["latestAlert"]
                self.assertEqual((population["gradedCount"], population["win"], population["unresolved"]), (2, 1, 1))
                self.assertEqual(active.read_bytes(), before)

    def test_legacy_id_matches_original_formula(self):
        row = posted()
        del row["alertId"]
        self.assertEqual(alert_id(row), "prop|2026-03-11T20:00:00Z|20.5|over")

    def test_offline_regrade_cli_preserves_sources_and_compares_corrections(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            posted_path, grades_path = root / "posted.jsonl", root / "graded.jsonl"
            jsonl(posted_path, [posted(), posted("b", game="SAS")])
            jsonl(grades_path, [grade(result="loss", finalValue=10), grade("b", alert=posted("b", game="SAS"))])
            cache = root / "games.json"
            row = box()
            row["GAME_DATE_DT"] = "2026-03-11"
            cache.write_text(json.dumps({"players": {"Test Player|2025-26": {"playerId": 123, "games": [row]}}}))
            before = [p.read_bytes() for p in (posted_path, grades_path)]
            result = subprocess.run(["node", str(ROOT / "src/js/grade_props.js"), "--posted", str(posted_path),
                "--grades", str(grades_path), "--all-history", "--regrade", "--dry-run", "--game-logs", str(cache),
                "--output-dir", str(root / "output")], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(before, [p.read_bytes() for p in (posted_path, grades_path)])
            comparison = json.loads((root / "output/gradingComparison.json").read_text())
            self.assertEqual(comparison["counts"], {"changed": 1, "needs_review": 1})
            accepted = (root / "output/acceptedGrades.jsonl").read_text().splitlines()
            self.assertEqual([json.loads(row)["alertId"] for row in accepted], ["a"])
            self.assertFalse(json.loads((root / "output/gradingSummary.json").read_text())["platformSettlementVerified"])
