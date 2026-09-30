# How SQL works in PP BOT

SQL is the language we use to ask structured questions about the bot's history. SQLite is the database engine that runs those queries. Here it stores everything in one local file: `data/active/props_analytics.sqlite3`. It needs no separate database server or paid service.

The bot still saves posted alerts and grading events in JSONL files (one JSON record per line). Those active files and their archives are the authoritative history. SQL is a reproducible analytics copy of that history. Starting the bot or grading a prop does not automatically refresh this copy.

## Data flow

1. The bot records an alert in `data/active/postedProps.jsonl`.
2. The grader records an evaluation in `data/active/gradedProps.jsonl`.
3. Archival history remains in `archive/posted/` and `archive/graded/`.
4. `npm run analytics:sql` imports all those sources into SQLite and prints counts/integrity checks.
5. Queries use tables and views to examine the latest outcomes or selected observation populations.

For example, an alert says a player's Points line was 20.5 and the bot recommended MORE. Its grade says the player scored 10: the box-score result is a loss. A later grading event can include complete first-half-only participation and an inferred reboot. SQL retains both events and the raw loss. A settlement query can show the inferred void separately, with its reason and evidence.

## Tables and views

A table stores records. A view is a saved query: it selects records consistently without storing another independent copy.

| Name | What it contains | Why it matters |
| --- | --- | --- |
| `posted_props` | One posted alert per alert ID, including player, line, side, analytics and raw JSON | Links predictions to the correct recorded alert |
| `grade_events` | Distinct grading events, their timestamps, raw results and complete `grade_json` | Preserves corrections and participation/settlement evidence |
| `import_runs` | Source paths/hashes and import counts | Shows what was imported and helps audit completeness |
| `latest_grades` | Latest grading event per alert | Earlier attempts and older backfills do not replace newer evaluations |
| `latest_alert_results` | Latest grades joined to posted alert details | Lets us group results by player, stat, side or tier |
| `unique_line_results` | Earliest posted representative for each player/stat/event/side/line | Reduces repeat alerts for the exact same line |
| `player_game_stat_results` | Earliest posted representative per player/game/stat | Reduces repeated lines for the same underlying player performance |

Latest events are selected by normalized UTC timestamps, then a deterministic content hash for exact-time ties. Ties with conflicting results are flagged in database health output. Choosing one deterministically does not prove it is the authoritative correction. Even deduplicated observations can remain correlated.

## Refresh and protection

From the project directory, run:

```powershell
npm run analytics:sql
```

Identical repeated imports do not duplicate alerts or grading events. A new correction becomes another event. Required identities, timestamps, finite values and enums are validated before the import commits; a failed import rolls back the requested source set. The importer records SHA-256 source hashes and checks SQLite integrity and foreign keys.

Incremental imports retain previously imported rows even if a source file later removes them. To rebuild an exact copy of the currently selected source files:

```powershell
npm run analytics:sql -- --rebuild
```

Rebuilding checks a temporary database before atomically replacing the destination. Original JSONL files are not modified. The prior derived database survives a failed rebuild. `--active-only` explicitly excludes archives; default imports include them. Schema version 2 preserves raw snapshots; an old database missing those snapshots needs source reimport/rebuild to restore them.

## Example queries

Run these in a SQLite query tool against `data/active/props_analytics.sqlite3`.

Count the stored populations:

```sql
SELECT 'posted alerts' AS population, COUNT(*) AS records FROM posted_props
UNION ALL SELECT 'grading events', COUNT(*) FROM grade_events
UNION ALL SELECT 'latest grades', COUNT(*) FROM latest_grades
UNION ALL SELECT 'unique lines', COUNT(*) FROM unique_line_results
UNION ALL SELECT 'player/game/stat', COUNT(*) FROM player_game_stat_results;
```

Compare box-score win/loss rates by stat on the unique-line population:

```sql
SELECT stat_type,
       SUM(result = 'win') AS wins,
       SUM(result = 'loss') AS losses,
       ROUND(100.0 * SUM(result = 'win') /
             NULLIF(SUM(result IN ('win', 'loss')), 0), 1) AS box_score_win_pct
FROM unique_line_results
GROUP BY stat_type;
```

This denominator excludes voids, pushes and unresolved results. It describes recorded box-score labels, including legacy grading rules; it does not measure verified PrizePicks settlement or profitability.

Inspect inferred reboots alongside the preserved raw result:

```sql
SELECT player_name, stat_type, line, recommended_side,
       result AS box_score_result,
       json_extract(grade_json, '$.settlement.result') AS inferred_result,
       json_extract(grade_json, '$.settlement.reason') AS reason,
       json_extract(grade_json, '$.participation.firstHalfMinutes') AS first_half_minutes,
       json_extract(grade_json, '$.participation.secondHalfMinutes') AS second_half_minutes
FROM latest_alert_results
WHERE json_extract(grade_json, '$.settlement.reason') = 'nba_reboot';
```

The repository's `sql/settlement_summary.sql` groups all latest alerts by assessment status, outcome and reason. Old events without a settlement assessment appear as `legacy_unassessed`, with inferred outcome `unresolved`. `sql/performance_summary.sql` provides raw box-score descriptive summaries.

On September 30, 2026, the verified full-history copy contained 41,573 posted alerts, 42,468 grading events and 41,316 latest outcomes. These are a recorded snapshot, not live counters. Newly implemented participation assessments will appear after grading and SQL refresh; the implementation did not automatically relabel the old dataset.
