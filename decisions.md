# Repair decisions

Recorded September 30, 2026. Scope: repair-plan steps 2 and 3. Entries document the selected behavior, alternatives, costs, and verification. The existing fantasy repair and uncommitted SQL work are preserved. Projections remain research-only.

## D01 — Match event identity before computing a grade

**Decision:** use a verified NBA game ID when supplied; otherwise require the scheduled league date and expected opponent. Convert aware start times to an explicit league timezone, default `America/New_York`. Do not substitute a nearby game's result or infer a reschedule from proximity. Missing/ambiguous evidence stays unresolved.

**Reason:** an unresolved record can be retried; a plausible but incorrect label contaminates every downstream model comparison. The old ±1/±2-day search could choose a previous day's different opponent. Explicit IDs can identify a rescheduled game without relying on date guesses.

**Tradeoff:** strict matching may leave more records unresolved until event metadata is available. Name/date matching is necessary for legacy alerts because they lack NBA IDs; a PrizePicks prop ID is not an NBA game ID. Naive timestamps are rejected for event matching rather than silently assigned a timezone.

**Verification:** back-to-backs, midnight UTC, explicit offsets, repeated opponents, wrong opponents, missing/duplicate IDs, naive times, postponed dates, and exact-date fixtures.

## D02 — Box-score evaluation is distinct from platform settlement

**Decision:** persist grading version, matched NBA game ID/player ID, matching basis, and `settlementBasis: nba_box_score_comparison`; `platformSettlementVerified` remains false. Remove the five-minute automatic-void heuristic. Zero participation is unresolved without official participation/settlement evidence. Positive minutes use the recorded stat total for internal evaluation.

**Reason:** minutes alone cannot establish PrizePicks injury/reboot eligibility. The platform's current [DNP/reboot guidance](https://www.prizepicks.com/help-center/dnps-reboots-and-ties) distinguishes non-participation and more-specific reboot cases. A player with positive minutes is not automatically void solely for playing fewer than five minutes.

**Tradeoff:** this does not automate PrizePicks reboots or certify actual payouts. No existing platform outcomes are replaced. Future platform confirmation needs its own trusted source; this phase prevents the bot from overstating what a box score proves.

## D03 — Preserve history; make corrections explicitly reviewable

**Decision:** normal grading still processes pending active alerts. Add dry-run, all-history/regrade, cached-game-log input, and separate output options, plus a change comparison. Reports and SQL read archives without moving them. Existing settled grades are reconsidered only with an explicit regrade option.

**Reason:** automatically rewriting 40K+ alerts would obscure the original model record and mix corrected labels with legacy predictions. Cached NBA rows allow deterministic offline verification without dependence on live endpoints.

**Tradeoff:** a dry-run comparison of all real historical grades is complete only where authoritative box-score data has been supplied or successfully fetched. Missing data is reported, not fabricated. A later historical-correction stage remains responsible for a comprehensive reconciliation.

## D04 — One chronology policy across Python and SQLite

**Decision:** latest grade = greatest timezone-normalized `gradedAt` at microsecond precision, then greatest canonical event hash for exact timestamp ties. Reject missing/naive grade timestamps. Retain all distinct grade events and full raw JSON in SQL.

**Reason:** insertion order makes backfills regress outcomes; lexical ISO timestamps disagree across offsets. Shared helpers prevent the reports and SQL from selecting different latest events. Exact-time conflicts are deterministic and surfaced as quality flags; a hash is a tie-breaker, not evidence that one conflicting result is more authoritative.

**Tradeoff:** timestamps are still only as reliable as the producer. Conflicting equal-time grades warrant review. Legacy schema migration cannot recreate raw fields never stored; a JSONL reimport can restore them.

## D05 — Separate record populations and use a fixed observation policy

**Decision:** show latest-alert, unique-line, and player-game/stat observations separately. Unique-line identity includes player, stat, side, event, and line. Player-game/stat sampling selects the earliest posted alert, rather than whichever was graded last. Collapse grade events before window/coverage filtering and select representatives before projection/confidence eligibility filters.

**Reason:** grading retries are not extra observations; line movement is not an independent game. Selecting representatives after seeing coverage/outcomes introduces an avoidable selection effect. Missing event identity falls back to the individual alert ID instead of merging unrelated missing-date rows.

**Tradeoff:** player-game/stat rows are not independent across different stats or players in the same game. Their labels describe a defined sampling policy, not a claim of statistical independence. Historical results will differ from the old prop-ID/latest-graded policy.

## D06 — Report eligibility and legacy formula limitations honestly

**Decision:** raw result totals stay separate from projection-eligible wins/losses; both numerator and denominator use the same eligible population. Reject non-finite/out-of-range probabilities. Mark legacy fantasy grades/predictions as uncorrected and exclude them from projection calibration unless explicitly requested. Preserve existing probability estimator semantics for now and label this limitation.

**Reason:** mixed field coverage previously allowed win rates above 100%. Adding archives increases coverage gaps and legacy formula exposure, so these must be explicit. Corrected labels alone cannot repair predictions produced with the old formula.

**Tradeoff:** fewer usable calibration rows, especially fantasy. The void/push-conditioned probability redesign belongs to step 5, so this phase fixes record accounting without quietly changing model mathematics.

## D07 — Atomic imports, versioned migration, explicit rebuild

**Decision:** validate all explicitly requested sources, accept UTF-8 BOMs, reject non-finite values/invalid required identities/enums/nested ID mismatches, and import the complete source set in one transaction. Recover legacy IDs using the original `propId|postedAt|line|recommendedSide` formula when all four validated fields exist; never assign arbitrary replacement IDs. Log import counts and source hashes. Migrate the existing schema transactionally with `PRAGMA user_version`; reject unknown future versions. Rebuild via a separate temporary database and atomic replacement after successful import/checks, never by deleting the existing database first.

**Reason:** partial archive imports give misleading research populations. Repeated imports must remain idempotent and older backfills must not regress latest outcomes. Python's [SQLite transaction documentation](https://docs.python.org/3/library/sqlite3.html) notes that `executescript` can commit pending work, so migration transaction boundaries will be explicit.

**Tradeoff:** strict validation can expose historical inconsistencies that require repair rather than silently dropping them. Incremental imports retain historical rows removed from source files; an explicit rebuild is the exact selected-source copy. Rebuild requires temporary disk space and closed external database readers.

## Execution order and verification

1. Shared history/identity/chronology helpers and their regression tests.
2. Grading match rules, provenance, offline dry-run and comparison.
3. Projection/confrontation/grading report population repairs.
4. SQLite schema migration, multi-file import, rebuild and validation.
5. Fixture CLI tests, full local checks, full-history imports/reports to separate artifacts, repeat imports and database integrity checks.
6. Update README/FIX_PLAN and this decision log with actual outcomes and remaining limits.

No original JSONL file is scheduled for mutation during verification. Live posting and model score-weight changes are outside this request.

## Participation follow-up decisions

### D08 — Validate court intervals; never infer absence from missing events

Use NBA GameRotation stints, validated against full box-score minutes and five-player team coverage for the entire finalized game. Check quarters 3/4 and overtime. NBA play-by-play is a fallback for positive on-court evidence only; missing events, bench technicals, and absent player rows do not establish non-participation. Quarter-filtered traditional box scores are excluded: a live request for one quarter returned a player with 25:01 minutes. Rotation availability is intermittent, so unsuccessful requests are retried and incomplete evidence remains unknown.

Cache finalized per-game data once for all alerts, with a 24-hour refresh for valid rotations and a 15-minute refresh for partial evidence. Retain source timestamps/hashes and allow supplied offline fixtures without live fallback. Validation costs an extra full-game box score but prevents a silently truncated rotation feed from creating false reboots. A missing game-log row alone cannot prove a DNP. An alert with a supplied NBA game ID can use the full-roster fallback only after verifying date, opponent, membership, and zero participation; otherwise it remains unresolved.

### D09 — Store inferred settlement separately from box-score results

Retain `result` as the original NBA box-score comparison. Add `participation` evidence and a versioned `settlement` assessment. Infer a reboot only for an explicit single-player NBA full-game MORE market, first-half participation, validated absence after halftime and in overtime, and a losing MORE comparison. Winning MORE, LESS, partial-game markets, and later returns are evaluated separately. A tie remains a push for the existing analytics convention; it is not automatically converted to a reboot. Verified zero participation can produce an inferred DNP. These are rule-based inferences using the [published NBA reboot guidance](https://www.prizepicks.com/reboots), not platform-confirmed settlements.

New alerts persist full-game NBA feed provenance. Legacy alerts lacking market scope require an explicit research override; ambiguous eligibility stays for review. Separate summary/recap counts expose inferred settlements without relabeling box-score history or automatically fitting projections on inferred platform labels. Cases with unavailable participation data can be retried during the grading window even when a box-score result already exists.

Only stat types supported by the full-game grader and valid MORE/LESS sides are assessed, excluding unknown attempt-based markets. Complete DNP assessments stop routine retries; explicit regrading that loses previously supported evidence requires review. These choices reduce unnecessary API requests and prevent an outage from erasing an evidence-backed assessment. Rotations establish court participation, not the reason a player left, and this feature has no platform lineup settlement feed.

### D10 — Reuse SQL raw snapshots for new evidence

Schema 2 already retains the complete grading JSON, so participation/settlement fields import without new nullable columns or a migration. Add a descriptive settlement query using SQLite JSON functions and validate assessment enums on import. The existing latest-event chronology still selects each alert's newest evidence, and original JSONL stays authoritative. This keeps the optional database reproducible and avoids making the bot depend on SQL for live grading.

## Implementation outcomes

### Earlier stages 1–3

- Steps 2 and 3 implemented locally. Report sampling and SQL views share the same keys and ordering. The Node grading wrapper forwards review arguments and reads the separate summary. Recap text identifies box-score evaluation without claiming verified platform settlement.
- **Legacy identity choice:** 999 posted records predate saved IDs. Keeping the original ID formula imported them without losing their links to grade events. Full import: 41,573 posted alerts, 42,468 events, 41,316 latest outcomes. These counts include archived files.
- **Population verification:** SQL and Python agree on every latest event hash and every representative: 32,723 unique lines, 23,675 player-game/stat observations. No equal-time result conflicts were found in the current dataset.
- **Repeat import:** zero new/updated posts and zero new grade events; all 42,468 grade events recognized as duplicates. Integrity is `ok`; foreign-key violations are zero. Import metadata records hashes of all eight source files.
- **Migration verification:** a copy of the previous active-only database retained 6,970 posts, 6,782 events, and 6,713 latest alerts. All migrated events correctly reported missing raw snapshots because the old schema never saved them. Invalid migrations roll back schema changes; failed rebuilds preserve the previous database.
- **History protection:** all eight active/archive JSONL file hashes were unchanged after testing/import/reporting. The prior analytics database is preserved in `reports/step23/props_active_before.sqlite3`; the default derived database is refreshed through the checked rebuild workflow.
- **Grading review:** offline CLI fixtures demonstrate a loss-to-win correction and a settled-to-unresolved `needs_review` case. Only the supported correction enters `acceptedGrades.jsonl`. Cached-input mode makes no live NBA fetches. No real historical grades were rewritten and no Discord messages were sent.
- **Research interpretation:** the full-history player-game/stat confrontation sample has 15,550 complete non-tied pairs: posted sides win 54.8%, projection-preferred sides 52.8%. These use legacy box-score labels, exclude legacy fantasy predictions, and do not establish profitability or independent sample size. They support retaining research-only projections; model promotion/weight changes were not made.
- **Checks:** all syntax/compile checks and 65 regression tests pass. Separate artifacts and reconciliation evidence are under ignored `reports/step23/`. Comprehensive authoritative historical regrading remains step 7; probability estimator/season/context repairs remain step 5.

### Participation follow-up

- **Checks:** all syntax/compile checks and 107 tests pass, including 40 participation tests. Coverage includes one-second returns, halftime boundaries, overtime, truncated/conflicting feeds, missing identities, bench events, retries/cache, DNP fallback, prior-assessment protection, recap rendering, SQL validation rollback, and offline Node-to-Python grading/import.
- **Live verification:** NBA game `0022000180` returned 24 roster players and 56 rotation rows, with date `2021-01-15`. Jaylen Brown's first-half 15.55 and second-half 9.4667 minutes matched 25:01 overall; second-half participation is established. No PrizePicks settlement was queried or claimed.
- **Offline integration:** four isolated alerts retained raw loss/win/loss/unresolved results while producing inferred reboot/win/loss/DNP assessments. Grading, projection, confrontation, and SQL summaries agreed. The fixture SQL import was idempotent with integrity `ok` and zero foreign-key violations.
- **Preservation:** all eight active/archive JSONL hashes remain unchanged. The real SQLite database retains 41,573 posted alerts, 42,468 events, and 41,316 latest outcomes, all currently `legacy_unassessed` for the new settlement field. No synthetic fixture data was mixed into the real database. No live Discord messages were sent.
- **Limits:** historical market eligibility and policy dates need review before any correction dataset is adopted. NBA rotations can be unavailable or incomplete; those cases remain unknown. Full-roster DNP fallback requires a supplied NBA game ID; date/opponent-only alerts missing from PlayerGameLog remain unresolved. Season/context and projection probability repairs remain stage 5. Evidence and demonstrations are in ignored `reports/participation_followup/`.
