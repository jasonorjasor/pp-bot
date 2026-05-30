import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PY_DIR = REPO_ROOT / "src" / "py"
if str(SRC_PY_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_PY_DIR))

from projection_report import (
    build_calibration_artifact,
    build_family_tables,
    build_side_tables,
    choose_probability,
    dedupe_player_game,
    finalize_bucket,
    get_probability_for_side,
    make_summary,
    update_bucket,
)


class ProjectionReportTests(unittest.TestCase):
    def test_zero_probability_is_not_missing(self):
        analytics = {"pOverAdjusted": 0.0, "pOverFull": 0.9}
        alert = {"recommendedSide": "over", "analytics": analytics}
        self.assertEqual(get_probability_for_side(analytics, "over"), 0.0)
        self.assertEqual(choose_probability(alert), 0.0)

    def test_dedupe_prefers_prop_id(self):
        records = [
            {
                "gradedAt": "2026-04-01T10:00:00Z",
                "result": "win",
                "alert": {
                    "propId": "123",
                    "playerName": "Test Player",
                    "statType": "Points",
                    "recommendedSide": "over",
                    "startTime": "2026-04-01T12:00:00Z",
                },
            },
            {
                "gradedAt": "2026-04-01T11:00:00Z",
                "result": "loss",
                "alert": {
                    "propId": "123",
                    "playerName": "Test Player",
                    "statType": "Points",
                    "recommendedSide": "under",
                    "startTime": "2026-04-01T12:00:00Z",
                },
            },
        ]
        self.assertEqual(len(dedupe_player_game(records)), 1)

    def test_calibration_gates_skip_thin_tables(self):
        summary = make_summary()
        update_bucket(summary["bySide"]["over"], 0.60, 1)
        finalize_bucket(summary["bySide"]["over"])
        update_bucket(summary["byStatType"]["Points"], 0.62, 1)
        finalize_bucket(summary["byStatType"]["Points"])

        side_tables = build_side_tables(summary, 2)
        family_tables, eligible_families, skipped_families = build_family_tables(summary, 2)

        self.assertFalse(side_tables["over"]["eligible"])
        self.assertEqual(side_tables["over"]["skipReason"], "below_min_stable_count")
        self.assertFalse(family_tables["Points"]["eligible"])
        self.assertEqual(family_tables["Points"]["skipReason"], "below_min_stable_count")
        self.assertNotIn("Points", eligible_families)
        self.assertIn("Points", skipped_families)

    def test_calibration_artifact_marks_skipped_family(self):
        summary = make_summary()
        update_bucket(summary["bySide"]["over"], 0.60, 1)
        finalize_bucket(summary["bySide"]["over"])
        update_bucket(summary["byStatType"]["Points"], 0.62, 1)
        finalize_bucket(summary["byStatType"]["Points"])

        args = SimpleNamespace(
            days=7,
            start_date=None,
            game_date=None,
            include_predeploy=False,
            calibration_min_side_count=2,
            calibration_min_family_count=2,
        )
        artifact = build_calibration_artifact(summary, summary, Counter(), None, args)
        self.assertEqual(artifact["metadata"]["fitSource"], "dedupedStableFit")
        self.assertEqual(artifact["dedupedStableFit"]["skippedFamilies"]["Points"]["reason"], "below_min_stable_count")


if __name__ == "__main__":
    unittest.main()
