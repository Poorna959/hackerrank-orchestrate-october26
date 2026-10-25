# Final completion report — Ghost Data Still Exists

Date: 2026-10-25. Everything below was run on this checkout; nothing is estimated
or copied from an earlier session. Where a metric could not be measured it is
marked **not measured**.

## 1. Summary

Completed in this pass:

1. An authorised deletion whose subject cannot be resolved is no longer reported
   `COMPLETE`; it either audits the records linked to the request address or
   returns `INCOMPLETE` with an explanatory note and no invented steps.
2. A would-be `COMPLETE` with no citable verification step is downgraded to
   `INCOMPLETE`, so the workflow's claim is never accepted as proof.
3. Policy-derived record state is no longer overridden by a record's own
   `result: PRESENT` word for tombstone / support / analytics records, removing
   24 spurious exposures findings.
4. Workflow account attribution rejects accounts whose own evidence names a
   different subject, and ignores rows dated after `audit_at`.
5. Undated evidence no longer risks a `None`-timestamp comparison.
6. The validator gained semantic checks (uncited `COMPLETE`, citations after
   `audit_at` for files, activities and audit rows, duplicate steps) and now
   separates structural from semantic failures.
7. 53 automated tests were added (none existed), including 22 negative validator
   tests and 8 new end-to-end cases on temporary datasets.
8. `--out` is now a documented alias of `--output`; an optional, measured
   local-OCR path (`--ocr`) was added and kept off by default.
9. Reports were rewritten against the latest measured run; `output.csv` and
   `code.zip` were regenerated and verified.

Remaining: nothing is blocked in the code. The open items are judgement calls
about the hidden grader (§7) and the submission-side items in §8.

## 2. Repository state

| Item | Value |
| --- | --- |
| Branch | `freebuff/the-complete-instruction-what-you-should-do-is-in--774853a0-e530-49f1-8587-c557a2a8fdfa` (worktree of the repo) |
| Base commit | `59ff76e` |
| Commits created | none — no commit or push was performed (not authorised) |
| Diff size | 12 files changed, +786 / −371, plus 6 new paths |

Modified: `README.md`, `code/main.py`, `code/audit/engine.py`,
`code/audit/planner.py`, `code/audit/validator.py`,
`code/audit/evidence/json_source.py`, `code/audit/evidence/loader.py`,
`code/evaluation/main.py`, `code/evaluation/evaluation_report.md`,
`code/evaluation/usage_report.md`, `output.csv`, `log.txt`.

Added: `code/audit/evidence/ocr_source.py`, `code/evaluation/cases.py`,
`code/evaluation/fixtures.py`, `code/evaluation/baseline_report.md`,
`code/tests/{test_cli,test_engine,test_parsers,test_validator}.py`, `FINAL_REPORT.md`.

## 3. Commands and exit codes (all run for this report)

| # | Command | Exit | Observed output |
| --- | --- | --- | --- |
| 1 | `python3 code/main.py` | 0 | `wrote 518 rows`, `COMPLETE=149, INCOMPLETE=359, NOT_APPLICABLE=10`, `validation PASS … structural issues=0, semantic issues=0, warning(s)=9` |
| 2 | `python3 code/evaluation/main.py` | 0 | status 32/32, aligned 144/211, fields 1003/1158, sequence 13/32, `custom cases: 11/11 passed (8 end-to-end)` |
| 3 | `python3 -m unittest discover -s code/tests` | 0 | `Ran 53 tests … OK` (1.33 s) |
| 4 | `python3 /tmp/independent_check.py .` (separate script, no `audit/` imports) | 0 | `rows 518 … steps 3149 citations 1137`, `errors 0` |
| 5 | `python3 code/main.py --requests dataset/sample_deletion_requests.csv --out /tmp/sample_final.csv` | 0 | `--out` alias accepted |
| 6 | `python3 code/main.py --ocr --output /tmp/with_ocr.csv` | 0 | 5 rows change vs default; 34.3 s |
| 7 | `unzip -q code.zip -d /tmp/zipcheck2 && cd /tmp/zipcheck2 && python3 code/main.py --quiet` | 0 | `validation PASS` and `cmp` reports the archive's `output.csv` is byte-identical to the repository's |
| 8 | `cd /tmp/zipcheck2 && python3 -m unittest discover -s code/tests` | 0 | `Ran 53 tests … OK` |
| 9 | `cd /tmp/zipcheck2 && python3 code/evaluation/main.py` | 0 | `11/11 passed` |
| 10 | `python3 -m py_compile` over all shipped modules | 0 | no syntax errors |

The archive was also re-run against the same eight-case matrix on the pre-change
sources (§4.4 of `code/evaluation/evaluation_report.md`) to prove the new tests
are regressions, not tautologies.

## 4. Test results

`python3 -m unittest discover -s code/tests -v` → **53 passed, 0 failed, 0 skipped**.

| Module | Tests | Result |
| --- | --- | --- |
| `code/tests/test_parsers.py` | 15 | all pass — `test_insert_without_column_list_uses_declared_order`, `test_quoted_values_and_nulls_round_trip`, `test_primary_table_defines_absence_scope_but_replica_does_not`, `test_empty_capture_is_not_absence_scope`, `test_snapshot_scope_rows_declare_every_system`, `test_collection_topologies_stay_separate`, `test_record_set_maps_results_and_categories`, `test_service_export_profile_lookup_404_is_absence`, `test_snapshot_bundle_reads_capture_time`, `test_only_terminal_events_become_records`, `test_trace_key_is_inherited_from_the_started_line`, `test_document_records_cite_resource_ids`, `test_supplied_documents_are_readable`, `test_undated_evidence_does_not_crash_and_stays_oldest`, `test_offsets_are_normalised_to_utc` |
| `code/tests/test_engine.py` | 11 | all pass — includes `test_authorized_deletion_with_unresolved_identity_is_never_complete`, `test_non_deletion_request_with_unresolved_identity_stays_not_applicable`, `test_unresolved_identity_row_serializes_and_validates`, `test_complete_without_citable_evidence_is_downgraded`, `test_all_synthetic_cases_pass` |
| `code/tests/test_cli.py` | 2 | all pass — `test_out_alias_and_output_are_the_same_destination`, `test_documented_readme_commands_parse` |
| `code/tests/test_validator.py` | 25 | all pass — 3 positive (`test_valid_file_passes`, `test_not_applicable_empty_array_passes`, `test_incomplete_without_findings_is_a_warning_not_a_pass_certificate`) and 22 negative (`test_complete_without_verification_fails`, `test_evidence_after_audit_at_fails`, `test_activity_citation_after_audit_at_fails`, `test_duplicate_step_fails`, `test_wrong_header_fails`, …) |

Evaluation cases (`python3 code/evaluation/main.py`, exit 0): C1, C2, C3 plus
E1–E8 all `PASS` → 11/11. Pre-change run of E1/E2/E7 on `59ff76e` sources: `FAIL`
(observed output recorded in `code/evaluation/evaluation_report.md` §4.1).

## 5. Measured quality

| Metric | Baseline `59ff76e` | Final |
| --- | --- | --- |
| Sample status accuracy | 32/32 = 1.000 | **32/32 = 1.000** |
| Sample aligned step accuracy | 141/211 = 0.668 | **144/211 = 0.682** |
| Sample field accuracy | 1000/1158 = 0.864 | **1003/1158 = 0.866** |
| Sample exact sequence | 12/32 = 0.375 | **13/32 = 0.406** |
| Full-run rows / steps | 518 / 3173 | **518 / 3149** |
| Full-run statuses C/I/NA | 158/350/10 | **149/359/10** |
| Project validator | PASS, 9 warnings | **PASS, 0 structural, 0 semantic, 9 warnings** |
| Independent re-check | not run | **0 errors** |
| Automated tests | none | **53 passing** |

Hidden-set accuracy: **not measured** (no answers are published for the 518-case
set). No score is claimed for the grader.

## 6. Artifacts

| Path | Size | Verified |
| --- | --- | --- |
| `output.csv` | 629 370 bytes (519 lines: header + 518 rows, LF endings, ASCII) | header exact, ids complete/unique/sorted, statuses allowed, JSON valid, ordering correct, citations admissible — project validator and independent script both clean |
| `code.zip` | 88 416 bytes (33 entries, 28 files) | contains `README.md`, `code/` (agent, tests, `baseline_report.md`, `evaluation_report.md`, `usage_report.md`); no `dataset/`, no `__pycache__`/`.pyc`, no nested archives, no `.env`/credentials; extracted to `/tmp` and re-run end-to-end |
| `FINAL_REPORT.md` (this file) | outside `code.zip` by design | — |
| `log.txt` | appended, never rewritten | session-start and per-turn entries |

## 7. Known limitations

1. **Nine full-dataset cases now return `INCOMPLETE` with an empty plan** where the
   workflow (and possibly the hidden gold) says `COMPLETE`. The supplied evidence
   for them is unreadable by the default pipeline (screenshot-only), so completion
   cannot be proved. This is a deliberate correctness trade, and it is the most
   likely place to lose status accuracy in the full run.
2. **`resource_id` attribution** for identifier-free records inside a batched
   `reported_at` remains the dominant sample error (0.767). Another subject's
   resources are no longer attributed, but two cases in the same batch can still
   swap resources of the same category.
3. **Screenshots are read only with `--ocr`** (local `tesseract`), which is noisy:
   5 of 518 rows change, including one status flip. Kept off so the submitted file
   is reproducible without OCR tooling.
4. **`deletion_rules.csv` conditions are applied at category level.** A rule whose
   action depended on a numeric condition (e.g. a retention deadline) would not be
   evaluated symbolically.
5. **No dependency manifest** exists because the solution uses only the standard
   library; `python3` ≥ 3.9 is the only requirement.
6. **Reduced inputs are handled, not tested exhaustively.** Missing optional CSVs
   are tolerated (`system_activity.csv`, `deletion_rules.csv`) but a missing
   `user_identities.csv` or `evidence_files.csv` raises `DatasetError`, which
   `code/main.py` turns into exit code 2 with a message.

## 8. Submission checklist

| Item | Status |
| --- | --- |
| `output.csv` at repository root, 518 rows, contract-clean | **done, verified twice** |
| `code.zip` with runnable code, README, evaluation reports | **done, verified by extraction + re-run** |
| `evaluation/evaluation_report.md` (status/step/field/sequence accuracy, ≥3 custom cases, failures + fixes) | **done — reports live in `code/evaluation/`** |
| `evaluation/usage_report.md` (models, calls, tokens, cost) | **done — states 0 calls / 0 tokens / $0.00 for the run** |
| No dataset, caches, nested archives, credentials in `code.zip` | **verified** |
| `log.txt` chat transcript | **done** (uploaded from this file; appended per turn, never rewritten) |
| `agent-trace.zip` | **action needed by the user** — see below |
| Secret/PII review of submitted transcripts | **partially done** (archive payload scanned: no credential-like assignments; `log.txt` reviewed: no secrets) |

Notes on the two user-side items:

* The challenge's trace instructions ask for an `agent-trace.zip` built from the
  harness's own session storage, with no credentials included. That storage lives
  outside this repository, so it cannot be assembled here; it must be exported from
  the app and reviewed before upload.
* `dataset/` must stay out of `code.zip` (it is) but `output.csv` is uploaded
  separately alongside `log.txt`.

## 9. Remaining actions for the user

1. Export the agent trace from Freebuff into `agent-trace/freebuff-desktop/` and zip
   it as `agent-trace.zip` for the trace upload slot.
2. Decide whether the nine screenshot-only cases should be `COMPLETE` (gold may say
   so) or `INCOMPLETE` (what the pipeline can prove); flipping them is a one-line
   policy change in `code/audit/planner.py` and is **not** recommended without
   evidence.
3. Commit and push when authorised — nothing has been committed in this pass.
