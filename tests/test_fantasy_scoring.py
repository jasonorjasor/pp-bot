import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

SRC_PY_DIR = Path(__file__).resolve().parents[1] / "src" / "py"
sys.path.insert(0, str(SRC_PY_DIR))

from grade_props import grade_alert
from nba_stats import compute_analytics
from prop_utils import STAT_MAP, compute_game_total
from team_context import compute_allowance_metrics, compute_context_for_prop, compute_opponent_bias


class FantasyScoringTests(unittest.TestCase):
    def setUp(self):
        self.box = {"PTS": 20, "REB": 5, "AST": 5, "STL": 1, "BLK": 1, "TOV": 4}

    def test_both_fantasy_aliases_deduct_turnovers(self):
        for label in ("Fantasy Score", "Fantasy Points"):
            with self.subTest(label=label):
                self.assertAlmostEqual(compute_game_total(self.box, STAT_MAP[label]), 35.5)

    def test_zero_turnovers_preserve_other_contributions(self):
        self.box["TOV"] = 0
        self.assertAlmostEqual(compute_game_total(self.box, STAT_MAP["Fantasy Score"]), 39.5)

    def test_missing_turnovers_are_not_silently_ignored(self):
        del self.box["TOV"]
        with self.assertRaises(KeyError):
            compute_game_total(self.box, STAT_MAP["Fantasy Score"])

    def test_non_fantasy_stats_keep_their_units(self):
        for label, expected in (("Points", 20), ("Pts+Rebs+Asts", 30), ("Turnovers", 4)):
            with self.subTest(label=label):
                self.assertEqual(compute_game_total(self.box, STAT_MAP[label]), expected)

    def test_grading_uses_corrected_fantasy_value_for_both_sides(self):
        row = {**self.box, "MIN_FLOAT": 30, "GAME_DATE_DT": datetime(2026, 3, 11), "MATCHUP": "LAL vs. DAL"}
        fetch = {"ok": True, "df": pd.DataFrame([row])}
        for side, expected in (("over", "loss"), ("under", "win")):
            with self.subTest(side=side):
                alert = {"alertId": side, "statType": "Fantasy Score", "line": 37.5, "recommendedSide": side, "startTime": "2026-03-11T19:00:00-07:00", "game": "DAL"}
                grade = grade_alert(alert, fetch)
                self.assertEqual(grade["finalValue"], 35.5)
                self.assertEqual(grade["result"], expected)
                self.assertEqual(grade.get("fantasyScoringVersion"), 2)

    def test_opponent_allowance_uses_same_formula_per_100_possessions(self):
        row = {f"OPP_{key}": value for key, value in self.box.items()}
        row.update({"OPP_POSS": 100, "OPP_FGM": 8, "OPP_FGA": 17, "OPP_FTM": 4, "OPP_FTA": 5, "OPP_FG3M": 0, "OPP_FG3A": 3})
        allowance = compute_allowance_metrics(pd.DataFrame([row]))
        self.assertEqual(allowance["fantasy"], 35.5)
        self.assertEqual(allowance["points"], 20)
        self.assertEqual(allowance["turnovers"], 4)

    def test_legacy_fantasy_cache_does_not_adjust_new_formula(self):
        opponent = {"opponentAllowance": {"blended": {"fantasy": 120}}}
        league = {"opponentAllowance": {"blended": {"fantasy": 100}}}
        bias, details = compute_opponent_bias("Fantasy Score", opponent, league)
        self.assertEqual(bias, 0)
        self.assertEqual(details.get("fallbackReason"), "legacy_fantasy_scoring")

    def test_versioned_fantasy_cache_can_adjust_new_formula(self):
        opponent = {"opponentAllowance": {"blended": {"fantasy": 120}}}
        league = {"opponentAllowance": {"blended": {"fantasy": 100}}}
        bias, _ = compute_opponent_bias("Fantasy Score", opponent, league, fantasy_scoring_version=2)
        self.assertGreater(bias, 0)

    def test_analytics_uses_corrected_totals_and_records_version(self):
        rows = []
        for index in range(10):
            date = datetime(2026, 3, 10) - timedelta(days=index * 2)
            rows.append({**self.box, "MIN_FLOAT": 30, "FGA": 17, "FTA": 5, "GAME_DATE_DT": date, "GAME_DATE": date.strftime("%b %d, %Y"), "MATCHUP": "LAL vs. DAL", "WL": "W"})
        with patch("nba_stats.load_team_context_cache", return_value=None):
            analytics = compute_analytics(pd.DataFrame(rows), "Fantasy Score", 37.5)
        self.assertEqual(analytics["mean"], 35.5)
        self.assertEqual(analytics["games"][0]["value"], 35.5)
        self.assertEqual(analytics["projectionMean"], 35.5)
        self.assertEqual(analytics["fantasyScoringVersion"], 2)

    def test_context_passes_cache_formula_version_to_opponent_adjustment(self):
        frame = pd.DataFrame([{**self.box, "MATCHUP": "LAL vs. DAL"}])
        cache = {
            "teams": {"LAL": {}, "DAL": {"opponentAllowance": {"blended": {"fantasy": 120}}}},
            "league": {"opponentAllowance": {"blended": {"fantasy": 100}}},
        }
        legacy = compute_context_for_prop(frame, [], "Fantasy Score", "DAL", None, cache, enable_pace=False, enable_rest=False, enable_role=False)
        self.assertEqual(legacy["opponentBias"], 0)
        cache["fantasyScoringVersion"] = 2
        current = compute_context_for_prop(frame, [], "Fantasy Score", "DAL", None, cache, enable_pace=False, enable_rest=False, enable_role=False)
        self.assertGreater(current["opponentBias"], 0)


if __name__ == "__main__":
    unittest.main()
