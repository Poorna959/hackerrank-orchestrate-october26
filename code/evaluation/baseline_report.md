# Baseline report — before the hardening pass

Recorded before any source change, on the checkout that was already pushed to
`main`. Every number below was produced by the command shown next to it on this
machine; nothing is estimated.

| Item | Value |
| --- | --- |
| Branch | `freebuff/the-complete-instruction-what-you-should-do-is-in--774853a0-e530-49f1-8587-c557a2a8fdfa` |
| Base commit | `59ff76e` — "Add the deletion-audit agent, evaluation reports and submission artifacts." |
| Working tree at start | `log.txt` modified (previous turn's log entry), `__pycache__` untracked; no source changes |
| Runtime | `Python 3.14.4` |
| Dataset | `dataset/` present: 518 requests, 94 evidence files (24 SQL, 28 JSON, 26 log, 4 PDF, 11 PNG, 1 TXT) |
| Dependencies | none — the pipeline imports only the standard library |
| Pre-existing tests | none (`detected_test_files: 0`; no `test_*`, no runner, no dependency manifest) |

## 1. Commands run (baseline)

```bash
python3 --version
# Python 3.14.4

python3 code/main.py --output /tmp/baseline_full.csv --quiet
# validation PASS: 518 rows, 518 cases, 3173 steps, 0 error(s), 9 warning(s)
# warnings: case_33, case_35, case_45, case_50, case_52, case_53, case_55,
#           case_61, case_63 — "COMPLETE with no verification steps"
# exit 0, real 7.9 s

python3 code/evaluation/main.py
# case status accuracy:      32/32 = 1.000
# step (aligned) accuracy:   141/211 = 0.668
# field accuracy:            1000/1158 = 0.864
# exact-sequence accuracy:   12/32 = 0.375
# custom cases: 3/3 passed
# exit 0
```

An independent structural check (not the project validator) re-read
`output.csv` with `csv`/`json` only:

| Check | Result |
| --- | --- |
| Header | `['case_id', 'deletion_status', 'steps_json']` — exact |
| Data rows | 518, equal to the request count |
| Case ids | all 518 present, unique, sorted ascending |
| Statuses | `INCOMPLETE` 350, `COMPLETE` 158, `NOT_APPLICABLE` 10 — all allowed |
| `NOT_APPLICABLE` with non-empty steps | 0 |
| `step_no` not consecutive from 1 | 0 |

## 2. Known defects at baseline

1. **Unresolved identity reported as `COMPLETE`.** `AuditEngine.audit_case`
   returned `COMPLETE` with `steps=[]` whenever the request email matched no
   identity. No case in `dataset/deletion_requests.csv` triggers the branch
   (measured: `unresolved identity = 0` of 518), so the defect is latent in the
   scored output but is exactly the "the dashboard says done" failure the
   challenge is about. It is a real correctness bug for any case whose subject
   is missing from `user_identities.csv` / `user_audit_log.csv`.
2. **`COMPLETE` claimed with no evidence.** `case_33`-family cases produce no
   state at all and still returned `COMPLETE` with an empty step list; the
   validator downgraded that to a warning.
3. **Result words overrode policy-derived state for redaction categories.** A
   flat JSON record with `result: "PRESENT"` was mapped to `PRESENT` even when
   the record carried no direct identifier (a tombstone with only `user_id`, an
   unattributable analytics row), producing spurious `REPORT_EXPOSED_PII`
   findings.
4. **Workflow rows named accounts without an ownership check.** `case`-level
   account attribution took the deletion-worker row nearest `reported_at` with no
   test that the account belonged to this subject, so a batched batch could
   attribute another customer's account.
5. **Screenshots never read.** 11 PNG files were skipped, leaving the nine
   `COMPLETE`-with-no-verification warnings.
6. **`README.md` documented `--out`,** while `code/main.py` only defined
   `--output`.
7. **No automated tests** existed for any of the above.

## 3. Environmental blockers

None. The dataset, the runtime and the evaluation harness all worked; there was
no network dependency to satisfy and no missing file.

## 4. Notes for later phases

* Full-run wall clock at baseline: 7.9 s. Recorded so later changes can be
  measured against it rather than asserted.
* Sample metrics at baseline: status 1.000, aligned steps 0.668, fields 0.864,
  exact sequence 0.375. These are the reference values for every later claim in
  `evaluation_report.md`.
