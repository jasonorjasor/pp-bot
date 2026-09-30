import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/py"))
from props_sql import database_health, import_sources, open_database, rebuild_database
from prop_history import event_hash, latest_grades
from test_history_grading import posted, grade, jsonl


class SqlHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.posts, self.grades = self.root / "posted.jsonl", self.root / "grades.jsonl"
        self.db = self.root / "analytics.sqlite3"
        jsonl(self.posts, [posted()])
        jsonl(self.grades, [grade()])
        self.connection = open_database(self.db)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def load(self):
        return import_sources(self.connection, [self.posts], [self.grades])

    def test_backfill_offsets_and_ties_match_python_in_any_order(self):
        newer = grade(gradedAt="2026-03-12T08:00:00Z")
        older = grade(gradedAt="2026-03-12T10:00:00+03:00", result="loss")
        tied = grade(gradedAt=newer["gradedAt"], result="push")
        jsonl(self.grades, [newer, tied, older])
        self.load()
        expected = latest_grades([newer, tied, older])["a"]
        self.assertEqual(self.connection.execute("SELECT event_hash FROM latest_grades").fetchone()[0], event_hash(expected))
        jsonl(self.grades, [older])
        self.load()
        self.assertEqual(self.connection.execute("SELECT event_hash FROM latest_grades").fetchone()[0], event_hash(expected))
        self.assertEqual(database_health(self.connection)["equalTimestampResultConflicts"], 1)

    def test_repeat_import_counts_and_metadata(self):
        first = self.load()
        second = self.load()
        self.assertEqual(first["counts"]["insertedGradeEvents"], 1)
        self.assertEqual(second["counts"]["insertedGradeEvents"], 0)
        self.assertEqual(second["counts"]["duplicateGradeEvents"], 1)
        self.assertEqual(second["counts"]["unchangedPosts"], 1)
        self.assertEqual(len(second["sources"][0]["sha256"]), 64)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0], 2)
        self.assertEqual(database_health(self.connection)["integrity"], "ok")

    def test_missing_requested_file_rolls_back_all_files(self):
        with self.assertRaises(FileNotFoundError):
            import_sources(self.connection, [self.posts], [self.grades, self.root / "missing.jsonl"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM posted_props").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0], 0)

    def test_malformed_archive_rolls_back_all_files(self):
        bad = self.root / "archive.jsonl"
        bad.write_text('{bad}\n')
        with self.assertRaisesRegex(ValueError, "archive.jsonl:1"):
            import_sources(self.connection, [self.posts], [self.grades, bad])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM grade_events").fetchone()[0], 0)

    def test_nested_identity_mismatch_rejected_even_when_post_exists(self):
        jsonl(self.grades, [grade(alert=posted("different"))])
        with self.assertRaisesRegex(ValueError, "nested alertId"):
            self.load()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM posted_props").fetchone()[0], 0)

    def test_nonfinite_and_invalid_required_values_are_rejected(self):
        bad_posts = [posted(line=float("nan")), posted(score=float("inf")), posted(line=True),
                     posted(recommendedSide="more"), posted(playerName=""), posted(alertId=""),
                     posted(analytics=[]), posted(postedAt="2026-03-11T20:00:00"),
                     posted(analytics={"pOverAdjusted": 1.5})]
        for record in bad_posts:
            with self.subTest(record=record):
                jsonl(self.posts, [record])
                with self.assertRaises(ValueError):
                    self.load()
        jsonl(self.posts, [posted()])
        for record in (grade(result="unknown"), grade(gradedAt=None), grade(finalValue="NaN"), grade(finalMinutes=-1)):
            with self.subTest(record=record):
                jsonl(self.grades, [record])
                with self.assertRaises(ValueError):
                    self.load()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM posted_props").fetchone()[0], 0)

    def test_legacy_ids_and_sql_observation_views(self):
        legacy = posted()
        del legacy["alertId"]
        jsonl(self.posts, [legacy, posted("b", line=21.5, postedAt="2026-03-11T21:00:00Z")])
        legacy_id = "prop|2026-03-11T20:00:00Z|20.5|over"
        jsonl(self.grades, [grade(legacy_id, alert=legacy), grade("b", alert=posted("b", line=21.5, postedAt="2026-03-11T21:00:00Z"))])
        self.load()
        health = database_health(self.connection)
        self.assertEqual((health["latest_grades"], health["unique_line_results"], health["player_game_stat_results"]), (2, 2, 1))
        self.assertEqual(self.connection.execute("SELECT alert_id FROM player_game_stat_results").fetchone()[0], legacy_id)

    def test_rebuild_failure_preserves_existing_database(self):
        self.load()
        self.connection.close()
        before = self.db.read_bytes()
        self.grades.write_text('{bad}\n')
        with self.assertRaises(ValueError):
            rebuild_database(self.db, [self.posts], [self.grades])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(list(self.root.glob("props-rebuild-*")), [])
        self.connection = open_database(self.db)

    def test_rebuild_removes_rows_not_in_selected_sources(self):
        self.load()
        self.connection.close()
        jsonl(self.posts, [posted("b")])
        jsonl(self.grades, [grade("b")])
        _, health = rebuild_database(self.db, [self.posts], [self.grades])
        self.assertEqual(health["latest_grades"], 1)
        self.connection = open_database(self.db)
        self.assertEqual(self.connection.execute("SELECT alert_id FROM latest_grades").fetchone()[0], "b")

    def test_future_schema_is_refused(self):
        future = self.root / "future.sqlite3"
        connection = sqlite3.connect(future)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        with self.assertRaisesRegex(ValueError, "future schema"):
            open_database(future)

    def legacy_database(self, name, timestamp="2026-03-12T05:00:00Z"):
        path = self.root / name
        connection = sqlite3.connect(path)
        connection.executescript("""CREATE TABLE posted_props (
            alert_id TEXT PRIMARY KEY, prop_id TEXT, posted_at TEXT, player_name TEXT, stat_type TEXT,
            line REAL, recommended_side TEXT, tier TEXT, score REAL, game TEXT, start_time TEXT, analytics_json TEXT NOT NULL);
            CREATE TABLE grade_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_hash TEXT NOT NULL UNIQUE,
            alert_id TEXT NOT NULL REFERENCES posted_props(alert_id), graded_at TEXT, result TEXT, source TEXT,
            game_date TEXT, final_value REAL, final_minutes REAL, notes TEXT, error_code TEXT);
            CREATE VIEW latest_grades AS SELECT alert_id,result FROM grade_events;""")
        connection.execute("INSERT INTO posted_props VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a", "prop", "2026-03-11T20:00:00Z", "Test Player", "Points", 20.5, "over", "watchlist", 7, "DAL", "2026-03-12T02:00:00Z", "{}"))
        connection.execute("INSERT INTO grade_events(event_hash,alert_id,graded_at,result,game_date) VALUES (?,?,?,?,?)",
            ("old-hash", "a", timestamp, "win", "2026-03-11"))
        connection.commit()
        connection.close()
        return path

    def test_legacy_schema_migration_preserves_records_and_marks_missing_raw(self):
        connection = open_database(self.legacy_database("legacy.sqlite3"))
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            health = database_health(connection)
            self.assertEqual((health["grade_events"], health["latest_grades"], health["eventsWithoutRawSnapshot"]), (1, 1, 1))
            self.assertEqual(connection.execute("SELECT result FROM latest_grades").fetchone()[0], "win")
        finally:
            connection.close()

    def test_invalid_migration_rolls_back_schema_changes(self):
        path = self.legacy_database("invalid.sqlite3", timestamp="bad")
        with self.assertRaises(ValueError):
            open_database(path)
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertNotIn("graded_at_us", {row[1] for row in connection.execute("PRAGMA table_info(grade_events)")})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM grade_events").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='import_runs'").fetchone())
        finally:
            connection.close()
