# PP BOT repair plan

Updated September 30, 2026. Work in the existing PP BOT checkout and preserve the uncommitted SQL addition.

The audit findings are in `reports/offseason_review_2026-09-30.md`. Relevant project history confirms that projections deliberately remain research-only and all families remain `watch_only`. Repairing a shared stat formula can change a score because its input was wrong; promoting projections or redesigning score weights is a separate, later decision.

## Working rules

- Complete and test one bounded stage before moving to the next.
- Write regression tests for demonstrated failures, including integration boundaries where relevant.
- Keep active and archived history intact. Produce separate correction artifacts and an impact comparison before replacing any history.
- Use offline fixtures for Discord, API, archive, and failure tests. No live posts are needed to validate repairs.
- Preserve a clear distinction between code correctness, historical label correctness, and measured predictive performance.
- Retain the original audit as a baseline; track repairs here instead of rewriting its original findings.

## 1. Shared fantasy scoring — complete

Fix the turnover deduction for Fantasy Score/Fantasy Points and opponent fantasy allowance. Use a shared formula to prevent drift. Mark new fantasy calculations with a formula version and protect fantasy adjustments from legacy context caches.

Acceptance: known box-score totals match the published formula; both aliases, zero/missing turnovers, ordinary stats, grading sides, cached context, and posted-field persistence are covered. Full existing checks pass. Original history remains unchanged.

## 2. Exact game matching and grading provenance — complete

Prefer stable game IDs where available, require the expected opponent, and prioritize the correct event date with explicit timezone handling. Leave ambiguous cases unresolved. Review the five-minute void heuristic against current platform settlement rules and distinguish internal analytics grading from confirmed platform settlement. Handle postponed games, DNPs, corrections, and retryable missing results explicitly.

Acceptance: fixtures cover back-to-backs, UTC midnight, repeated opponents, wrong-opponent candidates, reschedules, missing games, and ambiguous matches. A grading dry run writes to a separate location and shows which outcomes would change.

Implemented strict NBA-ID or league-date/opponent matching, grading version/provenance, unresolved zero/missing participation, and removal of the five-minute void heuristic. Added separate dry-run/regrade outputs and an offline cached-log input. Comparison flags unsupported settled-to-unresolved changes for review; accepted corrections are separate. Existing real grades remain intact for step 7.

## 3. Reporting and complete SQL history — complete

Fix projection win-rate populations; reduce grade history to deterministic latest outcomes before analysis. Define alert, unique-line, and player-game observations separately. Include active plus archived data in all-history reports and SQL imports. Make latest grades chronology-safe across repeated/backfilled imports. Add strict finite-number and record validation, BOM support, explicit missing-file errors, schema versions, import metadata, and a documented rebuild path.

Acceptance: no rate can exceed 100%; numerator and denominator use the same population; latest outcomes survive older backfills; malformed inputs roll back; full-history counts reconcile; repeated imports remain idempotent; integrity and foreign-key checks pass.

Implemented shared UTC chronology/tie-breaking, archive discovery, first-posted observation sampling, consistent eligible denominators, complete-pair confrontation metrics, and legacy-fantasy calibration exclusion. SQLite schema 2 adds raw snapshots, population views, import hashes/counts, transactional migration, strict validation/BOM support, and atomic rebuild. Decisions/tradeoffs are in `decisions.md`.

## 4. Alert delivery and safe archiving — pending

Separate observation, analysis, pending delivery, and confirmed delivery. Retry transient failures and persist delivery intent before sending; record Discord message IDs. Fix the archive entrypoint. Make archival recoverable and coordinate writers so interruption/concurrency cannot lose or duplicate history.

Acceptance: mocked send/write failures, restarts, unchanged-line retries, line movement, duplicate prevention, and archive crash recovery pass. Test archiving only against copied fixtures. Document the remaining external-service limits of delivery guarantees.

## 5. Season, context, and projection inputs — pending

Normalize endpoint IDs and validate available stat fields. Make season/season type shared and configurable, with an explicit prior-season fallback and reduced confidence for changed roles. Add roster/availability and minutes inputs in a separate bounded change. Add cache TTL/version invalidation and verify disabled context features actually produce zero adjustments. Align projection mean/rate/probability estimators and distinguish graded-outcome probabilities from void/push probabilities. Evaluate side-specific edge scoring as a model experiment rather than silently changing thresholds.

Acceptance: realistic endpoint frames exercise play-type adjustments; missing fields fail clearly; stale data and empty new-season samples behave explicitly; projection summaries are internally consistent; research-only policy remains enforced.

## 6. Reproducible environment and CI — pending

Document supported Node/Python versions, pin Python dependencies, remove unused Node packages, replace `latest` dependency ranges, and update dependencies with audit findings. Add CI and meaningful offline workflow checks. Review environment/configuration parsing and documentation against actual commands.

Acceptance: a clean environment installs and runs the full checks; the archive command is exercised beyond syntax; dependency audit results are documented and unresolved relevant issues identified; no secrets/generated history are committed.

## 7. Historical correction and model evaluation — pending

After formulas, grading, and imports are stable, generate a versioned corrected dataset while retaining originals. Reconcile unresolved records and compare corrections by stat, date, side, and tier. Record model/configuration/data versions. Collect passed/skipped candidates and line snapshots prospectively. Compare the current score, simple baselines, and projections on later time blocks, grouping correlated observations by player/game.

Acceptance: correction counts reconcile with originals, evaluation uses only information available at prediction time, metrics include coverage/calibration/uncertainty, and claims do not confuse alert volume with independent outcomes or win rate with profitability. Promote model influence only after evidence supports it.

## Stage log

- Initial baseline: syntax checks and 15 unit tests passed; the audit reproduced bugs missing from those tests.
- Stage 1 complete: added turnover deductions to both fantasy aliases; opponent allowance uses the shared formula; new analytics and calculated grades persist `fantasyScoringVersion: 2`; legacy fantasy context is excluded until refreshed.
- Verification: 10 new scoring tests passed, including analytics, grading, and cache integration. The full `npm test` suite passed all syntax checks and 25 tests (12 projection, 3 SQL, 10 scoring). `git diff --check` passed.
- Stage 1 verification did not rewrite history or run a live workflow. Changes were left local and uncommitted at that stage.
- Stages 2/3 complete: all syntax/compile checks and 65 tests passed (12 projection, 15 SQL, 10 scoring, 28 grading/history/report integration).
- Full-history SQL: 41,573 posted alerts, 42,468 grade events, 41,316 latest outcomes. SQL and Python agree on all latest hashes and representatives (32,723 unique lines; 23,675 player-game/stat observations). A repeat import inserted no new records. Integrity and foreign-key checks passed.
- A copied legacy database migrated without losing rows; raw snapshots missing from the old schema remain explicitly marked until reimport. Failure tests cover complete-import rollback, migration rollback, and rebuild preservation.
- All eight source JSONL hashes remain unchanged. Offline grading fixtures showed a supported loss-to-win correction and an unsupported downgrade requiring review. Real historical reconciliation is pending step 7. No live Discord workflow was started.
- The default analytics database was refreshed from all sources after preserving its previous active-only copy. Separate reports, databases, test logs, and verification evidence are under `reports/step23/`. Next stage: alert delivery and safe archiving.
- Revision handoff: this code revision captures completed stages 1–3, their regression tests, README, and decision log. Runtime history, analytics databases, generated reports, and credentials stay outside version control. Stages 4–7 remain separate work; completing them is not required to publish these tested fixes.
