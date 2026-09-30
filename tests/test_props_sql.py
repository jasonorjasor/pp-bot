import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PY_DIR = REPO_ROOT / "src" / "py"
if str(SRC_PY_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_PY_DIR))

from props_sql import import_jsonl, open_database


class PropsSqlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.posted = self.root / "posted.jsonl"
        self.grades = self.root / "grades.jsonl"
        self.database = self.root / "analytics.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_jsonl(self, path, records):
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def test_import_is_idempotent_and_latest_grade_is_selected(self):
        posted = {
            "alertId": "a-1", "propId": "p-1", "postedAt": "2026-09-01T10:00:00Z",
            "playerName": "Example Player", "statType": "Points", "line": 20.5,
            "recommendedSide": "over", "tier": "A", "score": 8.1,
            "game": "AAA @ BBB", "startTime": "2026-09-01T19:00:00Z",
            "analytics": {"sampleSize": 12},
        }
        grades = [
            {"alertId": "a-1", "gradedAt": "2026-09-02T01:00:00Z", "result": "unresolved"},
            {"alertId": "a-1", "gradedAt": "2026-09-02T02:00:00Z", "result": "win", "finalValue": 24},
        ]
        self.write_jsonl(self.posted, [posted])
        self.write_jsonl(self.grades, grades)

        connection = open_database(self.database)
        try:
            self.assertEqual(import_jsonl(connection, self.posted, self.grades), (1, 2))
            self.assertEqual(import_jsonl(connection, self.posted, self.grades), (1, 2))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM posted_props").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM grade_events").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT result FROM latest_grades WHERE alert_id='a-1'").fetchone()[0],
                "win",
            )
        finally:
            connection.close()

    def test_grade_snapshot_supplies_missing_posted_prop(self):
        self.write_jsonl(self.posted, [])
        self.write_jsonl(self.grades, [{
            "alertId": "a-2", "gradedAt": "2026-09-02T02:00:00Z", "result": "loss",
            "alert": {"alertId": "a-2", "postedAt": "2026-09-01T10:00:00Z", "playerName": "Another Player", "statType": "Rebounds", "line": 7.5,
                      "recommendedSide": "under", "analytics": {}},
        }])
        connection = open_database(self.database)
        try:
            import_jsonl(connection, self.posted, self.grades)
            self.assertEqual(
                connection.execute("SELECT player_name FROM posted_props WHERE alert_id='a-2'").fetchone()[0],
                "Another Player",
            )
        finally:
            connection.close()

    def test_malformed_json_rolls_back_the_import(self):
        self.write_jsonl(self.posted, [{"alertId": "a-3", "analytics": {}, "playerName": "Test Player", "statType": "Points", "line": 20.5, "recommendedSide": "over", "postedAt": "2026-09-01T10:00:00Z"}])
        self.grades.write_text('{bad json}\n', encoding="utf-8")
        connection = open_database(self.database)
        try:
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                import_jsonl(connection, self.posted, self.grades)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM posted_props").fetchone()[0], 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
