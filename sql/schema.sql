CREATE TABLE IF NOT EXISTS posted_props (
 alert_id TEXT PRIMARY KEY, prop_id TEXT, posted_at TEXT, player_name TEXT,
 stat_type TEXT, line REAL, recommended_side TEXT, tier TEXT, score REAL,
 game TEXT, start_time TEXT, analytics_json TEXT NOT NULL,
 raw_json TEXT, posted_at_us INTEGER
);
CREATE TABLE IF NOT EXISTS grade_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_hash TEXT NOT NULL UNIQUE,
 alert_id TEXT NOT NULL REFERENCES posted_props(alert_id), graded_at TEXT,
 result TEXT, source TEXT, game_date TEXT, final_value REAL, final_minutes REAL,
 notes TEXT, error_code TEXT, graded_at_us INTEGER, grade_json TEXT,
 unique_line_key TEXT, player_game_key TEXT, posted_at_us INTEGER,
 grading_version INTEGER, fantasy_scoring_version INTEGER, settlement_basis TEXT
);
CREATE TABLE IF NOT EXISTS import_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, completed_at TEXT NOT NULL,
 sources_json TEXT NOT NULL, counts_json TEXT NOT NULL, schema_version INTEGER NOT NULL
);
-- ANALYTICS VIEWS
CREATE INDEX IF NOT EXISTS idx_posted_props_stat_side ON posted_props(stat_type, recommended_side);
CREATE INDEX IF NOT EXISTS idx_grade_chronology ON grade_events(alert_id, graded_at_us DESC, event_hash DESC);
DROP VIEW IF EXISTS player_game_stat_results;
DROP VIEW IF EXISTS unique_line_results;
DROP VIEW IF EXISTS latest_alert_results;
DROP VIEW IF EXISTS latest_grades;
CREATE VIEW latest_grades AS
SELECT * FROM (
 SELECT grade_events.*, ROW_NUMBER() OVER (
 PARTITION BY alert_id ORDER BY graded_at_us DESC, event_hash DESC) AS row_num
 FROM grade_events
) WHERE row_num=1;
CREATE VIEW latest_alert_results AS
SELECT g.*, p.player_name, p.stat_type, p.line, p.recommended_side, p.tier, p.score
FROM latest_grades g JOIN posted_props p USING(alert_id);
CREATE VIEW unique_line_results AS
SELECT * FROM (
 SELECT a.*, ROW_NUMBER() OVER (
 PARTITION BY unique_line_key ORDER BY posted_at_us, alert_id) AS observation_num
 FROM latest_alert_results a
) WHERE observation_num=1;
CREATE VIEW player_game_stat_results AS
SELECT * FROM (
 SELECT a.*, ROW_NUMBER() OVER (
 PARTITION BY player_game_key ORDER BY posted_at_us, alert_id) AS observation_num
 FROM latest_alert_results a
) WHERE observation_num=1;
