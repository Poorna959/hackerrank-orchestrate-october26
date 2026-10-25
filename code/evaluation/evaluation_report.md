# Evaluation report

## 1. What was evaluated

| Item | Value |
| --- | --- |
| Agent entry point | `python3 code/main.py` |
| Evaluated state | working tree based on commit `59ff76e` plus the hardening pass described here (not committed) |
| Date of measurements | 2026-10-25 |
| Public sample set | 32 requests (`dataset/sample_deletion_requests.csv`) scored against `dataset/sample_output.csv` |
| Full run | 518 requests (`dataset/deletion_requests.csv`) → `output.csv` |
| Sample scorer | `python3 code/evaluation/main.py` (also `code/evaluation/compare.py`) |
| Regression suite | `python3 -m unittest discover -s code/tests -v` — 53 tests |
| Runtime | Python 3.14.4, single process, no network, no model calls |
| Full-run wall clock | 7.9 s / 10.7 s / 11.2 s / 15.0 s across four timed full runs on this machine (baseline: 7.9 s in its single timed run); the spread is machine load, not output-dependent |
| Determinism | two consecutive runs produce identical `output.csv` (no randomness, no wall-clock input) |

The agent is a deterministic evidence-reconstruction pipeline. No model is called
at prediction time, so every score below is reproducible and every step traces
back to a `file_id`, `activity_id` or `audit_id`.

**Hidden-set accuracy was not measured.** The 518-case set has no published
answers; the numbers below are the public sample, the full-output structural and
semantic checks, and the project's own cases. No claim is made about the score
of the hidden grader.

## 2. Case-level and step-level accuracy (public sample, 32 cases)

| Metric | Baseline (`59ff76e`) | Now | Change |
| --- | --- | --- | --- |
| `deletion_status` accuracy | 32/32 = 1.000 | **32/32 = 1.000** | unchanged |
| Aligned step accuracy (all six fields equal at the same index) | 141/211 = 0.668 | **144/211 = 0.682** | +3 steps |
| Field accuracy over aligned steps | 1000/1158 = 0.864 | **1003/1158 = 0.866** | +3 fields |
| Exact step-sequence accuracy | 12/32 = 0.375 | **13/32 = 0.406** | +1 case |

The sample contains 10 `COMPLETE` and 22 `INCOMPLETE` cases and no
`NOT_APPLICABLE` case, so the intent classifier is covered by in-process checks
(C1) rather than by the sample.

Per-field agreement over the 193 gold steps (aligned by index):

| Field | Agreement | Comment |
| --- | --- | --- |
| `phase` | 181/193 = 0.938 | one case orders a verification step differently |
| `system_id` | 162/193 = 0.839 | replica systems collapse onto the public system id |
| `resource_id` | 148/193 = 0.767 | the dominant error source (§6.4) |
| `operation` | 161/193 = 0.834 | follows the policy action taken for a record |
| `state` | 164/193 = 0.850 | follows `operation` by construction |
| `evidence_id` | 187/193 = 0.969 | citations are the strongest part of the output |

Sum: 1003/1158 = 0.866. Aligned accuracy is lower than field accuracy because the
gold answers attribute exactly one resource per category to a case while the
visible evidence stream also contains other customers' records inside the same
workflow window (§6.4).

## 3. Full-dataset run

Command: `python3 code/main.py` (from the repository root).

| Metric | Value |
| --- | --- |
| Rows written | 518 data rows + header |
| Rows sorted by `case_id` | yes (ascending, byte order) |
| `COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE` | 149 / 359 / 10 |
| Total steps | 3149 — 678 `FINDING`, 678 `REMEDIATION`, 1793 `VERIFICATION` |
| Mean steps per case | 6.08 |
| Steps with citations | 1137 |
| Remediation steps with a non-empty `evidence_id` | 0 |
| `NOT_APPLICABLE` rows with `steps_json=[]` | 10 / 10 |
| Project validator | PASS — 0 structural issues, 0 semantic issues, 9 warnings |
| Independent re-check (separate script, no `audit/` imports) | 0 errors over 518 rows / 3149 steps |

The 9 warnings are `INCOMPLETE` cases with no citable findings: nothing in the
admissible evidence identifies the subject's remaining data, so the agent reports
the request as open with an empty plan rather than inventing a resource (§6.2).

The independent re-check re-derived the operation/phase/state table, the ordering
rules, the citation admissibility rule and the status content rules from the
problem statement without importing the pipeline. It also confirmed:
every cited id exists; no citation is observed after its case's `audit_at`; no
`COMPLETE` row is empty or uncited; no `NOT_APPLICABLE` row has steps.

## 4. Additional evaluation cases created for this project

`python3 code/evaluation/main.py` exits non-zero if any case fails. Current
result: **11/11 passed, 8 of them end-to-end.**

### 4.1 End-to-end fixture cases (`code/evaluation/cases.py`)

Each case writes a complete temporary dataset with the production schema — CSVs,
evidence manifest, SQL/JSON/log evidence — then runs the production engine, the
production CSV writer (`main._write_output`) and the production validator.
Nothing touches `dataset/` or the repository's `output.csv`.

| Case | Scenario | Assertion | Result | Failed on old code? |
| --- | --- | --- | --- | --- |
| E1 | authorised deletion, subject in no identity table, no evidence | `INCOMPLETE`, `steps=[]`, note mentions the unresolved identity | PASS | **yes** — old code returned `COMPLETE, []` |
| E2 | same, but a support record carries the request email | `INCOMPLETE` with `REPORT_EXPOSED_PII` on `support_501` citing `evidence_02` | PASS | **yes** — old code returned no steps (nothing auditable) |
| E3 | failover replica still holds the profile, primary is clean | `REPORT_REMAINING_RECORD` on `account_701`, `DELETE_RECORD` remediation, uncited | PASS | no (regression guard) |
| E4 | a later capture shows the record redacted, one before the cut-off shows it exposed | finding cites `evidence_40` only, never the post-cut-off `evidence_41` | PASS | no (regression guard) |
| E5 | record only carries the pre-migration email from `user_audit_log.csv` | finding on `search_901` cited to `evidence_50` | PASS | no (regression guard) |
| E6 | compliant tombstone + retained audit record | `COMPLETE` with `VERIFY_RECORD_REDACTED` and `VERIFY_RECORD_PRESENT`, both cited | PASS | no (regression guard) |
| E7 | deletion-worker row names another subject's account in the same window | `account_951` never appears; only the subject's own `search_951` | PASS | **yes** — old code attributed `account_951` |
| E8 | data-catalog `LINK_RESOURCE_IDENTITY` row for `object_951` | `object_951` never appears in any step | PASS | no (regression guard) |

The "failed on old code" column was measured, not assumed: the same eight
functions were run against `git show 59ff76e:code/audit/{engine,planner}.py` and
`json_source.py` in a temporary copy of `code/`. Output observed:

```text
FAIL | E1 … | status=COMPLETE, steps=[]
FAIL | E2 … | steps=[], validator_ok=False
PASS | E3 …   PASS | E4 …   PASS | E5 …   PASS | E6 …
FAIL | E7 … | resources=['account_951', 'search_951']
PASS | E8 …
old tombstone state = PRESENT          # should be REDACTED
```

### 4.2 In-process checks

| Check | What it proves | Result |
| --- | --- | --- |
| C1 | deletion phrasings (EN/DE/FR) accepted; access, correction, opt-out and export phrasings rejected | PASS |
| C2 | on the real sample, `case_05` reports the failover-replica copy as remaining | PASS |
| C3 | planner ordering: `DISABLE_DATA_FLOW`, `BLOCK_RESTORE`, `DELETE_RECORD`, `REDACT_RECORD`, then verification in operation order, all future work uncited, `step_no` consecutive | PASS |

C2 and C3 exercise the real dataset and the planner directly; they are labelled
in-process because they do not run the full input-to-output pipeline.

## 5. Automated test suite

`python3 -m unittest discover -s code/tests -v` → **Ran 53 tests, OK** (1.33 s).

| Module | Tests | Coverage |
| --- | --- | --- |
| `code/tests/test_parsers.py` | 15 | SQL inserts without column lists, quoted values and `NULL`, absence scope (primary vs replica vs empty capture), `snapshot_scope`, collection topologies, JSON record sets, service exports, snapshot bundles, log terminal events, trace-key inheritance, PDF extraction, undated evidence, UTC normalisation of offset timestamps |
| `code/tests/test_engine.py` | 11 | resolvable/unresolved/non-deletion identity paths, serialisation, planner downgrade, ordering, intent classification, all eight end-to-end cases, validator acceptance |
| `code/tests/test_cli.py` | 2 | `--out` and `--output` reach the same destination; every `python3 code/main.py …` command printed in `README.md` parses |
| `code/tests/test_validator.py` | 25 | 3 positive cases and 22 negative cases: header, row count, duplicates, sorting, status, JSON shape, `NOT_APPLICABLE` payload, step numbering, operation/phase mismatch, state mismatch, extra fields, phase order, `COMPLETE` without verification, uncited verification, uncited findings, unknown evidence, evidence after `audit_at` (file and activity), cited remediation, cited future verification, duplicate steps |

## 6. Failures found and improvements made

### 6.1 An authorised deletion with an unresolved subject was reported `COMPLETE`

`AuditEngine.audit_case` returned `COMPLETE` with `steps=[]` whenever the request
email matched no identity — the exact failure mode the challenge describes (a
workflow claiming success for someone the system cannot even identify).

Fix: intent is still decided first, so a non-deletion request stays
`NOT_APPLICABLE`. For an authorised deletion with no resolvable subject the engine
now (a) tries an evidence-only resolution through the request address itself, so a
record that carries that address is still auditable, and (b) otherwise reports
`INCOMPLETE` with no steps and an explanatory note. No resource, evidence id or
step is invented. Measured: E1 flips from `COMPLETE` to `INCOMPLETE`; no case in
`dataset/deletion_requests.csv` hits this branch (0 of 518), so the scored file is
unchanged by this fix alone.

### 6.2 `COMPLETE` was claimed with no supporting evidence

Cases whose evidence yields no state at all (`case_33`, `case_35`, `case_45`,
`case_50`, `case_52`, `case_53`, `case_55`, `case_61`, `case_63`) returned
`COMPLETE` with an empty checklist. The planner now downgrades a would-be
`COMPLETE` to `INCOMPLETE` when not one verification step can be cited. Measured:
status distribution moved 158/350/10 → 149/359/10; sample status accuracy
unchanged at 32/32; those nine cases now carry the "no admissible evidence"
warning. This is a deliberate trade: an unsupported success claim is replaced by
an honest "cannot prove completion".

### 6.3 A record-level result word overrode the policy-derived state

For `ACCOUNT_TOMBSTONE`, `SUPPORT_MESSAGE` and `ANALYTICS_PROFILE` records, a flat
JSON `result: "PRESENT"` was mapped straight to `PRESENT`, even when the record
carried no direct identifier. A tombstone holding only `user_id` therefore looked
like exposed PII. Fix: for those categories the result word now only says the row
was found; the compliance state is derived from the identifiers actually present
(a tombstone with no direct identifier is `REDACTED`, an unattributable analytics
row is `ANONYMIZED`). Measured: 8 cases changed, 24 spurious `REPORT_EXPOSED_PII`
findings removed (3173 → 3149 steps), no status change, sample field accuracy
0.864 → 0.866.

### 6.4 Workflow rows named accounts without an ownership check

Account attribution took the deletion-worker row nearest `reported_at` with no
test that the account belonged to this subject, so a batched `reported_at` could
attribute another customer's account. Fix: a workflow row is now rejected when the
account's own evidence names a different subject, and rows dated after `audit_at`
are ignored. Measured: E7 fails on the old code and passes now; sample aligned
step accuracy 0.668 → 0.682, exact sequence 0.375 → 0.406.

### 6.5 Undated evidence could crash the cut-off comparison

An evidence record with no timestamp in the record and none in the manifest
produced `None` timestamps, which the cut-off comparisons and ordering would have
compared against aware datetimes. Fix: undated records are pinned to
"oldest-possible" so the comparison stays total and any authoritative capture at
or before `audit_at` still overrules them. Covered by
`LoaderTests.test_undated_evidence_does_not_crash_and_stays_oldest`.

### 6.6 The validator could pass output it should reject

The old validator downgraded `COMPLETE` with no verification to a warning, did not
check citation timing at all, and could not distinguish structural from semantic
failures. Changes:

* `COMPLETE` with no verification steps is now an **error** (the contract requires
  evidence-backed proof);
* a citation of evidence observed after the case's `audit_at` is now an **error**,
  checked for `file_id` (manifest `captured_at`), `activity_id` and `audit_id`;
* duplicate steps are a semantic error;
* `INCOMPLETE` with no findings, and `INCOMPLETE` with an empty plan, are
  **warnings** — the contract asks for findings, but when the admissible evidence
  names nothing, inventing one would violate the evidence rules;
* the report separates `structural issues` from `semantic issues`.

All 22 negative tests fail as they should (`code/tests/test_validator.py`).

### 6.7 Documentation / CLI mismatch

`README.md` used `--out` while `code/main.py` only defined `--output`. Fix:
`--out` is now a documented alias of `--output` (same destination), and every
command in the README is exercised in §7.

### 6.8 Ordering and serialisation (carried forward from the first build)

Steps are emitted in contract order — phase, then operation order, then
`system_id`, then `resource_id` — numbered consecutively from 1 and serialised
with sorted keys and compact separators. The validator sorts with plain ascending
string order, matching the writer.

## 7. Reproducing every number in this report

```bash
python3 code/main.py                                   # 518 rows, validator report, distribution
python3 code/main.py --requests dataset/sample_deletion_requests.csv --out /tmp/sample.csv
python3 code/evaluation/compare.py dataset/sample_output.csv /tmp/sample.csv
python3 code/evaluation/main.py                        # sample metrics + 11 cases (exit 1 on failure)
python3 -m unittest discover -s code/tests -v          # 53 tests
```

## 8. Screenshot evidence: measured, not enabled

Eleven PNG screenshots are supplied. The pipeline does not read them by default,
because OCR would make the output depend on whether a local `tesseract` binary
exists. An optional reader (`code/audit/evidence/ocr_source.py`, enabled with
`--ocr` or `HR_AUDIT_OCR=1`) was implemented and **measured** on this machine
(tesseract 5.5.0):

| Metric | Default run | `--ocr` run |
| --- | --- | --- |
| Full-run wall clock (single timed run each) | 7.9 s | 34.3 s |
| Cases whose row changed | — | 5 (`case_34`, `case_35`, `case_39`, `case_50`, `case_53`) |
| No-citable-finding warnings | 9 | 6 |
| `COMPLETE` → `INCOMPLETE` flips | — | 1 (`case_39`) |

OCR recovered usable text (for example `user_17` and a retained-reference screen),
filled three of the empty verification lists, but also produced one status flip
and a much larger step list for `case_34` — noisy enough that enabling it by
default would trade reproducible output for unverified extra steps. The submitted
`output.csv` therefore comes from the deterministic default run, and the OCR path
is documented rather than relied on.

## 9. Residual limitations

1. **Resource-id attribution for identifier-free records.** When several cases
   share one `reported_at` and the workflow touches a record that carries no
   identifier, the visible stream cannot separate one customer's resource from
   another's. The agent now refuses to attribute a resource whose evidence names
   another subject, and keeps at most one workflow candidate per category, but the
   remaining `resource_id` disagreement (0.767 on the sample) comes from this.
   The gold resource ids for some of these cases appear only inside screenshots.
2. **Nine cases with no admissible evidence.** Reported `INCOMPLETE` with an empty
   plan rather than an unsupported `COMPLETE`. If the hidden gold labels them
   `COMPLETE`, these are the most likely status losses in the full run.
3. **Screenshots are not read by default.** See §8.
4. **Policy conditions are not evaluated symbolically.** `deletion_rules.csv`
   conditions (retention window, "record is still attributable") are applied
   through the category-level action plus the derived state; a rule whose action
   depended on a numeric threshold would not be honoured.
5. **Hidden-set accuracy is unknown.** Only the public sample can be scored, and
   it is small (32 cases, 10 `COMPLETE`, no `NOT_APPLICABLE`).
