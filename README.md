# PP BOT

Discord bot for tracking PrizePicks NBA props, scoring both sides with Python analytics, posting the strongest plays to Discord, grading them later from official NBA logs, and analyzing a parallel projection layer.

## What the project does

- Polls PrizePicks NBA props and detects new lines or moved lines.
- Scores both `over` and `under` using recent performance, edge vs line, volatility, role risk, and matchup context.
- Uses local team context for pace, opponent allowance, rest, and role adjustments.
- Adds a projection layer (`minutes x rate`) for logging and research.
- Posts qualifying alerts to Discord and stores them in `data/active/postedProps.jsonl`.
- Grades settled props into `data/active/gradedProps.jsonl` using official NBA player game logs.
- Supports regular season, play-in, and playoff grading.
- Writes recap summaries, archives old data, and provides projection analysis reports.
- Offers an optional SQLite analytics import for querying posted props and grading history without changing the bot's JSONL workflow.

## Requirements

- Node.js 18+
- Python 3.10+
- `npm install`
- Python packages used by the analytics scripts:
  - `nba_api`
  - `pandas`
  - `numpy`

## Setup

Install dependencies:

```bash
npm install
pip install nba_api pandas numpy
```

Create a local `.env` from `.env.example`.

Required values:

- `DISCORD_TOKEN`
- `CHANNEL_ID`

Optional PrizePicks fetch helpers:

- `PRIZEPICKS_DEVICE_ID`
- `PRIZEPICKS_COOKIE`

These are not strictly required, but they can help with fetch reliability if PrizePicks blocks or rate-limits anonymous traffic.

## Project layout

Source code now lives under `src/js/` and `src/py/`, while generated state and reports live in separate folders:

- `src/js/`
  - Node entrypoints and Discord-facing workflow scripts
- `src/py/`
  - Python analytics, grading, projection, and context helpers

- `data/active/`
  - live JSON and JSONL state that the bot reads and writes during normal operation
- `reports/`
  - generated grading, projection, and role-risk reports
- `archive/`
  - archived settled JSONL history
- `backups/`
  - backup and recovery files

Main active files:

- `data/active/postedProps.jsonl`
- `data/active/gradedProps.jsonl`
- `data/active/seenProps.json`
- `data/active/teamContextCache.json`
- `data/active/playTypeCache.json`
- `data/active/lastRecapPosted.json`
- `reports/gradingSummary.json`
- `reports/projectionCalibration.json`
- `reports/roleRiskDeltaReport.json`

## Commands

### Main workflow

- `npm start`
  - Starts the bot and begins polling PrizePicks.

- `npm run grade`
  - Grades pending props only.
  - Updates `data/active/gradedProps.jsonl` and `reports/gradingSummary.json`.
  - Uses a supplied NBA game ID, or the exact scheduled league date plus expected opponent. Timezone defaults to `America/New_York`; there is no nearby-game fallback. Ambiguous/missing evidence stays unresolved.
  - Records `gradingVersion: 3`, matching provenance, and `platformSettlementVerified: false`. `result` compares NBA box scores with the posted line. Positive participation is evaluated even below five minutes; zero/missing participation stays unresolved in that comparison.
  - Adds NBA participation evidence and a separate inferred settlement assessment, described below. NBA rotation data can establish absence after halftime; missing play-by-play events cannot.

- `npm run recap`
  - Posts the latest recap from `reports/gradingSummary.json`.
  - Does not rerun grading.

- `npm run grade:full`
  - Runs grading, then posts the recap.

### Context and maintenance

- `npm run context:refresh`
  - Refreshes the cached team context manually.

- `npm run archive`
  - Moves older settled records out of active JSONL files into `archive/`.

### Projection analysis

- `npm run projection:report`
  - Runs the projection calibration report.
  - Writes or updates `reports/projectionCalibration.json`.

- `npm run projection:confrontation`
  - Compares posted-side results vs projection-preferred-side results.

Examples:

```bash
npm run projection:report -- --days 30 --post-deploy-only
npm run projection:confrontation -- --days 30 --post-deploy-only
npm run projection:report -- --all-history --include-predeploy --output-artifact reports/projectionFull.json
npm run projection:confrontation -- --all-history --include-predeploy --output-artifact reports/confrontationFull.json
```

Both reports read `archive/graded/*.jsonl` plus active grades by default, select the latest outcome per alert before applying time filters, and accept `--active-only`, `--grades PATH`, and `--archive-root PATH`. Custom grade paths use only that file unless an archive root is supplied. `--all-history` disables the rolling age cutoff; `--include-predeploy` also retains early records without projection fields.

Latest-alert, unique-line, and player-game/stat populations are reported separately. Unique lines include player, stat, event, side, and line. Player-game/stat observations choose the earliest posted alert before testing projection eligibility, so a later line/side is not selected because its result or coverage is preferable. Missing event identity is isolated by alert ID. These observations can still be correlated.

Projection win rates use eligible wins divided by eligible wins plus losses; raw results and coverage are separate. Confrontation rates compare identical complete pairs and exclude probability ties. Both predictions and labels require fantasy formula version 2 for default calibration; `--include-legacy-fantasy` is an explicit research override. Historical game matching has not been comprehensively reconciled, and probability treatment of voids/pushes awaits step 5. All projection families remain `watch_only`.

### Review historical grading safely

```bash
npm run grade -- --all-history --regrade --dry-run --output-dir reports/gradingReview
```

This can fetch NBA game logs for the entire posted history. It writes separate `proposedGrades.jsonl`, `acceptedGrades.jsonl`, `gradingComparison.json`, and `gradingSummary.json`, without appending to source history or replacing the live summary. `--regrade` requires dry-run or separate output; custom source files also require separate output. Proposed settled-to-unresolved changes are marked `needs_review` and excluded from accepted output. Accepted output remains a proposal for later historical reconciliation, not an automatic source rewrite. Ordinary grading includes archived outcomes in its overall summary but processes only active pending posts.

For deterministic offline review, add `--game-logs PATH`. The JSON shape is `{"players":{"Player Name|2025-26":{"playerId":123,"games":[{"GAME_ID":"0022500001","GAME_DATE":"2026-03-11","MATCHUP":"LAL vs. DAL","MIN":30,"PTS":25}]}}}`. Supply all stat columns needed for each prop (including `TOV` for fantasy). Missing cached player/season entries stay unresolved and never trigger live fetches. Use `--posted PATH --grades PATH` for isolated fixtures.

### NBA participation and inferred reboots

Ordinary grading checks finalized NBA GameRotation stints against the full box-score roster/minutes and five-player coverage throughout the game. It calculates first-half, second-half, and overtime minutes. If rotations are unavailable or incomplete, recognized on-court play-by-play events can prove that a player returned; absence of events stays unknown. Results are shared across players in the same game and cached under `data/active/participation/`. Valid rotations refresh after 24 hours; partial finalized data refreshes after 15 minutes. Failed/nonfinal responses are cached only within the current run. NBA requests have bounded retries, and evidence retains source timestamps and hashes.

`participation` contains these checks. `settlement` contains `status`, `result`, `reason`, `policyVersion`, and `platformVerified: false`. For a supported single-player NBA full-game MORE market, a losing box-score comparison plus validated first-half participation and no later return yields an inferred `void` with reason `nba_reboot`. Returning in quarters 3/4 or overtime prevents that inference. Winning MORE and LESS results remain their box-score outcomes; ties remain `push`. Complete evidence of zero participation can produce an inferred DNP. This follows the [published NBA reboot guidance](https://www.prizepicks.com/reboots), recorded as a September 30, 2026 policy snapshot; it does not verify a PrizePicks lineup, discretionary board correction, injury reason, or payout.

New alerts save NBA full-game feed provenance. Partial-game durations and unsupported stat markets stay for review. Old alerts without market scope are unassessed unless a separate historical review explicitly supplies `--legacy-market-scope full_game`. That override is an assumption about legacy market eligibility, not evidence that the platform settled it that way. Missing participation data leaves the assessment `needs_review` and can be retried within the normal grading window. A supported DNP stops ordinary retries. Explicit regrading that loses a supported assessment is flagged for review and excluded from accepted output.

A missing PlayerGameLog row alone never means DNP. If the alert supplies an NBA game ID, the fallback additionally verifies the scheduled league date, opponent, roster membership, and complete zero participation. Without that identity/evidence, the case remains unresolved. Existing history is not automatically rewritten; historical assessment remains a separate review.

For fully offline grading, supply normalized participation data with `--participation-data PATH` alongside `--game-logs PATH`. Its shape is `{"games":{"0022500001":{"gameId":"0022500001","gameStatus":3,"period":4,"players":[{"personId":123,"teamId":1,"minutesSeconds":600}],"rotationRows":[{"GAME_ID":"0022500001","PERSON_ID":123,"TEAM_ID":1,"IN_TIME_REAL":0,"OUT_TIME_REAL":6000}],"actions":[]}}}`. Times in rotation rows are elapsed tenths of a second; a usable fixture must contain both full rosters and complete court coverage, not just the example player. DNP fallback also needs `gameDate` and player `teamTricode`. See `tests/test_participation.py` for complete fixtures. Missing supplied games never trigger network requests; supplying game logs without participation data also disables participation network requests. `--participation-cache PATH` changes the live cache location.

The grading summary, recap, projection reports, and SQL query expose inferred settlements separately from raw box-score metrics. Projection fitting still uses its documented box-score labels; inferred reboots do not silently change calibration inputs.

### Optional SQL analytics

The bot continues to treat JSONL files as the source of truth. To create or refresh a local SQLite analytics copy and print import metadata and integrity counts, run:

```bash
npm run analytics:sql
```

This creates `data/active/props_analytics.sqlite3` (ignored local data), importing active posts/grades plus `archive/posted/*.jsonl` and `archive/graded/*.jsonl`. `--active-only` opts out of archives; `--archive-root PATH` sets an explicit root. Custom post/grade paths default to isolated sources. Required files and explicitly supplied archive roots must exist. Empty files are valid; missing files are errors.

Imports validate required identities, aware timestamps, enums, finite numbers/probabilities, and nested alert IDs. UTF-8 BOMs are accepted. Legacy alerts without IDs use the original `propId|postedAt|line|recommendedSide` formula. A failure anywhere in the requested source set rolls back the import. `import_runs` logs scanned/inserted/updated/duplicate counts and source SHA-256 hashes. Identical reimports do not duplicate posts/events.

Schema version 2 (`PRAGMA user_version`) retains full raw JSON and normalizes `gradedAt` to UTC microseconds. `latest_grades` selects by timestamp, then canonical event hash for exact-time ties; insertion order cannot regress results. `latest_alert_results`, `unique_line_results`, and `player_game_stat_results` expose the corresponding observation populations. Equal-time conflicting results are flagged in health output; their deterministic tie-breaker does not establish authority. `sql/performance_summary.sql` provides descriptive counts.

Older databases migrate transactionally on open. Fields omitted by the original schema cannot be reconstructed faithfully; migrated events report `eventsWithoutRawSnapshot` until source reimport restores them. Unknown future versions are refused. Incremental imports retain rows even if later removed from JSONL. Use an explicit rebuild for an exact copy of selected sources:

```bash
npm run analytics:sql -- --rebuild
npm run analytics:sql -- --posted fixtures/posted.jsonl --grades fixtures/graded.jsonl --database reports/fixture.sqlite3 --rebuild
```

Rebuild imports and checks a temporary database beside the destination, then replaces the destination atomically. Failure preserves the previous database. Close external database readers before rebuilding on Windows. SQL remains an optional analytics copy; its historical labels are not certified settlement or profitability estimates.

SQL refresh is manual: rerun `npm run analytics:sql` after grading or collecting more alerts. The existing schema stores the complete grading JSON, including participation and settlement evidence. `sql/settlement_summary.sql` counts inferred outcomes and marks old grades as `legacy_unassessed`; it does not replace their box-score results. See [docs/SQL_GUIDE.md](docs/SQL_GUIDE.md) for a plain-language explanation, table/view descriptions, and example queries.

### Checks and tests

- `npm run smoke`
  - Syntax-checks `src/js/index.js`.

- `npm run smoke:grade`
  - Syntax-checks `src/js/grade_props.js`.

- `npm run smoke:recap`
  - Syntax-checks `src/js/post_recap.js`.

- `npm run smoke:archive`
  - Syntax-checks `src/js/archive_props.js`.

- `npm run smoke:projection`
  - Python compile check for projection-related scripts.

- `npm run smoke:context`
  - Python compile check for context and scoring scripts.

- `npm run smoke:sql`
  - Python compile check for the optional SQL analytics importer.

- `npm run test:projection`
  - Runs the projection-focused Python unit tests.

- `npm run test:sql`
  - Tests imports, chronology, archive rollback, validation, schema migration, and safe rebuilding.

- `npm run test:grading`
  - Tests event identity, participation, report populations, and the Node-to-Python offline grading CLI.

- `npm run test:participation`
  - Tests validated court coverage, halftime/overtime, DNP identity, incomplete feeds, retries/cache, separate assessments, recap rendering, and offline grading-to-SQL import.

- `npm test`
  - Runs the full local check suite.

## Environment variables

### Bot and polling

- `POLL_INTERVAL_MS`
  - Default: `300000`
- `PYTHON_BIN`
  - Default: `python`
- `LOG_LEVEL`
  - Default: `info`
- `PRIZEPICKS_BACKOFF_MS`
  - Default: `300000`

### Scoring thresholds

- `MIN_RECOMMENDATION_SCORE`
  - Default: `6.5`
- `MIN_SAMPLE_SIZE`
  - Default: `6`
- `BEST_BET_SCORE`
  - Default: `8.0`
- `WATCHLIST_SCORE`
  - Default: `6.5`

### Grading

- `GRADE_GAME_DATE_TIMEZONE`
  - Default: `America/New_York`; converts aware start times to the NBA schedule date.
  - Requires timezone data (`tzdata` on Windows if the Python environment lacks it).
- `GRADE_VOID_MINUTES_THRESHOLD` is retired and ignored; minutes alone do not establish platform settlement.
- `GRADE_SETTLEMENT_DELAY_HOURS`
  - Default: `4`
- `GRADE_LOOKBACK_DAYS`
  - Default: `2`
- `GRADING_CHANNEL_ID`
  - Optional override for recap posting
- `FORCE_RECAP`
  - Default: `false`

### Context

- `CONTEXT_CACHE_TTL_HOURS`
  - Default: `24`
- `CONTEXT_ENABLE_PACE`
  - Default: `true`
- `CONTEXT_ENABLE_OPPONENT`
  - Default: `true`
- `CONTEXT_ENABLE_REST`
  - Default: `true`
- `CONTEXT_ENABLE_ROLE`
  - Default: `true`

### Opponent play-type bias

- `ENABLE_PLAYTYPE_OPPONENT_BIAS`
  - Default: `true`
- `PLAYTYPE_CACHE_TTL_HOURS`
  - Default: `24`
- `OPPONENT_BASELINE_WEIGHT`
  - Default: `0.7`
- `OPPONENT_PLAYTYPE_WEIGHT`
  - Default: `0.3`
- `OPPONENT_PLAYTYPE_MIN_SHARE`
  - Default: `0.08`
- `OPPONENT_PLAYTYPE_MIN_POSS`
  - Default: `25`
- `OPPONENT_PLAYTYPE_MAX_TYPES`
  - Default: `3`

### Archiving

- `ARCHIVE_RETENTION_DAYS`
  - Default: `14`
- `ARCHIVE_ROOT`
  - Default: `archive`

### Misc

- `BALLDONTLIE_KEY`
  - Present in `.env.example`, but not currently used by the main bot flow.

## Important files

- `data/active/postedProps.jsonl`
  - Raw posted alerts stored by the live bot.
- `data/active/gradedProps.jsonl`
  - Settled prop outcomes.
- `data/active/seenProps.json`
  - Tracks props already seen by the bot.
- `data/active/lastRecapPosted.json`
  - Stores the last recap payload sent to Discord.
- `data/active/prizepicksDeviceId.json`
  - Local PrizePicks device ID cache used for fetch stability.
- `reports/gradingSummary.json`
  - Latest grading summary used by the recap script.
- `data/active/teamContextCache.json`
  - Cached pace/opponent/rest context.
- `data/active/playTypeCache.json`
  - Cached play-type data for opponent bias.
- `reports/projectionCalibration.json`
  - Latest projection calibration artifact.

## Current model shape

The live posting logic is still driven by the score-based system in `src/py/nba_stats.py`:

- weighted hit rate
- edge vs line
- volatility / sample quality
- role-risk penalties
- context adjustments

The projection layer is currently parallel and research-focused:

- projected minutes
- projected per-minute rate
- projected mean / std
- over / under probabilities
- confidence band
- confrontation and calibration reporting

Projection fields are logged and reported, but they do not currently control live posting decisions.

## Behavior notes

- `npm run grade` no longer posts the recap by itself.
- Use `npm run grade:full` if you want grading and recap together.
- Grading now supports regular season, play-in, and playoff game logs.
- Recaps report both raw alert-level results and deduped unique-line results.
- Archive scripts keep old settled data out of the active JSONL files.

### Fantasy scoring repair

Fantasy Score and Fantasy Points use `PTS + 1.2*REB + 1.5*AST + 3*STL + 3*BLK - TOV`, following the [PrizePicks NBA scoring chart](https://www.prizepicks.com/playbook-article/how-to-play-prizepicks-nba-fantasy-scoring-system). New fantasy analytics and calculated grades carry `fantasyScoringVersion: 2`.

Older saved fantasy records omitted turnovers and have not been rewritten. Existing team caches without version 2 are excluded from fantasy opponent-allowance adjustments until `npm run context:refresh` rebuilds them; other context inputs remain available. Review historical fantasy results before using them for calibration.

`npm run test:scoring` runs the fantasy scoring regression tests and is included in `npm test`. See [FIX_PLAN.md](FIX_PLAN.md) for the staged repair plan.


