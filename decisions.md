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

## Implementation outcomes

- Steps 2 and 3 implemented locally. Report sampling and SQL views share the same keys and ordering. The Node grading wrapper forwards review arguments and reads the separate summary. Recap text identifies box-score evaluation without claiming verified platform settlement.
- **Legacy identity choice:** 999 posted records predate saved IDs. Keeping the original ID formula imported them without losing their links to grade events. Full import: 41,573 posted alerts, 42,468 events, 41,316 latest outcomes. These counts include archived files.
- **Population verification:** SQL and Python agree on every latest event hash and every representative: 32,723 unique lines, 23,675 player-game/stat observations. No equal-time result conflicts were found in the current dataset.
- **Repeat import:** zero new/updated posts and zero new grade events; all 42,468 grade events recognized as duplicates. Integrity is `ok`; foreign-key violations are zero. Import metadata records hashes of all eight source files.
- **Migration verification:** a copy of the previous active-only database retained 6,970 posts, 6,782 events, and 6,713 latest alerts. All migrated events correctly reported missing raw snapshots because the old schema never saved them. Invalid migrations roll back schema changes; failed rebuilds preserve the previous database.
- **History protection:** all eight active/archive JSONL file hashes were unchanged after testing/import/reporting. The prior analytics database is preserved in `reports/step23/props_active_before.sqlite3`; the default derived database is refreshed through the checked rebuild workflow.
- **Grading review:** offline CLI fixtures demonstrate a loss-to-win correction and a settled-to-unresolved `needs_review` case. Only the supported correction enters `acceptedGrades.jsonl`. Cached-input mode makes no live NBA fetches. No real historical grades were rewritten and no Discord messages were sent.
- **Research interpretation:** the full-history player-game/stat confrontation sample has 15,550 complete non-tied pairs: posted sides win 54.8%, projection-preferred sides 52.8%. These use legacy box-score labels, exclude legacy fantasy predictions, and do not establish profitability or independent sample size. They support retaining research-only projections; model promotion/weight changes were not made.
- **Checks:** all syntax/compile checks and 65 regression tests pass. Separate artifacts and reconciliation evidence are under ignored `reports/step23/`. Comprehensive authoritative historical regrading remains step 7; probability estimator/season/context repairs remain step 5.
