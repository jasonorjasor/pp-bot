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

The repo keeps source code at the top level for now, while generated state and reports live in separate folders:

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
```

### Checks and tests

- `npm run smoke`
  - Syntax-checks `index.js`.

- `npm run smoke:grade`
  - Syntax-checks `grade_props.js`.

- `npm run smoke:recap`
  - Syntax-checks `post_recap.js`.

- `npm run smoke:archive`
  - Syntax-checks `archive_props.js`.

- `npm run smoke:projection`
  - Python compile check for projection-related scripts.

- `npm run smoke:context`
  - Python compile check for context and scoring scripts.

- `npm run test:projection`
  - Runs the projection-focused Python unit tests.

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

- `GRADE_VOID_MINUTES_THRESHOLD`
  - Default: `5`
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

The live posting logic is still driven by the score-based system in `nba_stats.py`:

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


