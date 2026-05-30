import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PY_DIR = REPO_ROOT / "src" / "py"
if str(SRC_PY_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_PY_DIR))

from projection_confrontation_report import projection_result_for_side
from projection_report import get_projection_side


class ProjectionConfrontationTests(unittest.TestCase):
    def test_projection_preferred_side_prefers_higher_probability(self):
        self.assertEqual(
            get_projection_side({"pOverAdjusted": 0.61, "pUnderAdjusted": 0.39}),
            "over",
        )
        self.assertEqual(
            get_projection_side({"pOverAdjusted": 0.39, "pUnderAdjusted": 0.61}),
            "under",
        )
        self.assertEqual(
            get_projection_side({"pOverAdjusted": 0.50, "pUnderAdjusted": 0.50}),
            "tie",
        )

    def test_projection_result_for_side_uses_final_value(self):
        self.assertEqual(projection_result_for_side("over", 31.0, 29.5), "win")
        self.assertEqual(projection_result_for_side("under", 31.0, 29.5), "loss")
        self.assertEqual(projection_result_for_side("under", 28.0, 29.5), "win")
        self.assertEqual(projection_result_for_side("tie", 31.0, 29.5), "tie")


if __name__ == "__main__":
    unittest.main()
