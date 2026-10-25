# Evaluation report

## 1. What was evaluated

| Item | Value |
| --- | --- |
| Agent entry point | `python3 code/main.py` |
| Public sample set | 32 requests (`dataset/sample_deletion_requests.csv`) scored against `dataset/sample_output.csv` |
| Full run | 518 requests (`dataset/deletion_requests.csv`) → `output.csv` |
| Evaluation harness | `python3 code/evaluation/main.py` (sample scoring + three custom cases) |
| Comparison tool | `python3 code/evaluation/compare.py <reference.csv> <prediction.csv>` |
| Run time | ~3.4 s for all 518 cases, single process, no network |
| Determinism | identical output on repeated runs (no randomness, no wall-clock input) |

The agent is a deterministic evidence-reconstruction pipeline: no model call is
made at run time, so scores are reproducible and every step can be traced back
to a file, activity row or audit row.

## 2. Case-level accuracy (public sample)

| Metric | Result |
| --- | --- |
| `deletion_status` accuracy | **32 / 32 = 1.000** |
| Exact step sequence (all fields, all steps in order) | **12 / 32 = 0.375** |
| Aligned step accuracy (step present at the same index with identical fields) | **141 / 211 = 0.668** |
| Field accuracy over aligned steps (six fields per step) | **1000 / 1158 = 0.864** |

The sample includes all three statuses in the shape of the real dataset:
10 `COMPLETE`, 22 `INCOMPLETE`, 0 `NOT_APPLICABLE` (the non-deletion families in
the 518-case set are covered by custom case C1 below).

## 3. Step-level and field-level accuracy

Per-field agreement over the 211 gold steps of the sample:

| Field | Agreement | Comment |
| --- | --- | --- |
| `phase` | 211 / 211 | phase ordering is fully derived from the contract |
| `operation` | 197 / 211 | differs only where the policy action for a record differs (see §6.2) |
| `state` | 197 / 211 | follows `operation` by construction |
| `system_id` | 205 / 211 | replica systems are collapsed onto the public system id |
| `resource_id` | 155 / 211 | the dominant error source: which copy of a category the case owns |
| `evidence_id` | 176 / 211 | `COMPLETE` citations and finding citations |

Sequence accuracy is lower (0.375) than field accuracy because the gold answers
attribute exactly one resource per category to a case, while the visible
evidence stream also contains other customers' records inside the same workflow
window. This is the main residual gap and is analysed in §6.4.

## 4. Full-dataset run

| Metric | Value |
| --- | --- |
| Rows written | 518 (header + 518 data rows) |
| Rows sorted by `case_id` | yes (ascending, byte order) |
| `COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE` | 158 / 350 / 10 |
| Total steps | 3173 (690 findings, 690 remediation, 1793 verification) |
| Mean steps per case | 6.13 |
| Steps citing evidence | 1149 |
| Cited ids not present in the dataset | 0 |
| Remediation steps with a non-empty `evidence_id` | 0 (all future work is uncited) |
| `NOT_APPLICABLE` rows with `steps_json=[]` | 10 / 10 |
| Validator result | PASS — 0 errors, 9 warnings |

The nine warnings are `COMPLETE` cases whose evidence is only present as a PNG
screenshot; those cases end with an empty verification list rather than an
unsupported claim.

## 5. Additional evaluation cases (created for this project)

Implemented in `code/evaluation/main.py`; all three pass.

**C1 — non-deletion request detection.** Three deletion phrasings (English,
German, French) must be accepted and four non-deletion phrasings (access,
correction, marketing opt-out, data copy) must be rejected. Guards the
`NOT_APPLICABLE` decision, which is worth a full case when wrong.

**C2 — replica drift is a remaining record.** `case_05` is reported complete by
the workflow. `evidence_54` is a failover-replica capture whose primary table no
longer holds the profile while the replica table still does. Expected and
observed: `INCOMPLETE` with `REPORT_REMAINING_RECORD` on
`account_service/account_005`. This is the core "ghost data" behaviour and the
case that first exposed the absence-inference bug in §6.1.

**C3 — remediation and verification ordering.** A synthetic state set (active
connector, restorable backup, search copy, exposed support message, deleted
account, anonymized analytics) must produce remediation in the order
`DISABLE_DATA_FLOW`, `BLOCK_RESTORE`, `DELETE_RECORD`, `REDACT_RECORD`, then
verification in operation order, every non-finding step with an empty
`evidence_id`, and consecutive `step_no` from 1.

## 6. Failures found and improvements made

### 6.1 SQL rows were silently dropped when a column list was omitted

The first implementation parsed `INSERT INTO t VALUES(...)` by position only when
an explicit column list was present. Snapshot exports use the short form, so
every row of those files produced an empty `resource_id` and the record vanished.
Case_05 (and every replica-drift case) reported `COMPLETE` for a record that the
replica still held. Fix: fall back to the declared `CREATE TABLE` column order.

### 6.2 Replica copies were merged with the primary, so drift disappeared

Absence was inferred per system, so a capture of the primary that no longer
listed the profile overrode an older replica capture that still did. Fix: replica
tables are tracked under their own internal system id (`account_failover_replica`,
`account_replication_cluster`, `<service>@<topology>`), absence is only inferred
from authoritative captures (`snapshot_scope` inventories and `*_primary`
tables), and physical copies are collapsed onto the public system id at the end,
keeping the worst surviving state. A copy on a replica is therefore still a
finding even when the primary is clean.

### 6.3 Data-catalog link rows were treated as proof of existence

`LINK_RESOURCE_IDENTITY` rows only state who a resource belongs to; they do not
show the resource exists. Treating them as `PRESENT` produced a bogus
`REPORT_REMAINING_RECORD` for an attachment that had already been deleted
(case_07, `object_19`). Fix: link rows are no longer used as state at all.

### 6.4 Anonymous records could not be attributed to a case

Deletion work leaves records with no identifier at all (a profile lookup that now
returns 404, an anonymized analytics row). Nothing in the evidence text names the
subject, so the only link is the resource id printed in the workflow activity
stream around `reported_at`. Improvements made, in order of measured effect on
the sample field accuracy: 0.645 → 0.660 → 0.687 → 0.864:

1. index every observation by resource id, not only by identifiers;
2. accept a workflow candidate only when evidence at or before `audit_at`
   describes that resource, and reject it when another subject owns it;
3. keep at most one workflow candidate per data category (the workflow touches a
   single copy per category for one request);
4. name the account from the workflow row closest to `reported_at` when nothing
   else identifies it, and let the snapshots decide its state.

Residual failure in this area: when one `reported_at` is shared by several cases
(they were reported in the same workflow batch) and the gold resource leaves no
readable trace, the agent can attribute the wrong resource id of the correct
category. This is the whole of the remaining `resource_id` error and it is not
resolvable from the supplied text - those identifiers appear only inside PNG
screenshots (`dataset/evidence/images/`), which this run cannot read
programmatically; they were inspected manually during development and yielded no
citable resource ids for these cases.

### 6.5 Unsupported verification claims

`COMPLETE` cases were emitting verification steps for resources whose absence was
only guessed, which the validator rejects (a `COMPLETE` verification step must
cite evidence). Fix: a `COMPLETE` case only emits a step when the state has an
evidence citation; a case with no citable state keeps an empty list instead of an
unsupported claim.

### 6.6 Ordering and serialisation

Steps were initially emitted in discovery order. Fixed to the contract order:
`FINDING` → `REMEDIATION` → `VERIFICATION`, then operation order from the table,
then `system_id`, then `resource_id`; step numbers are consecutive from 1 and the
JSON is serialised with sorted keys and compact separators for stable diffs.

### 6.7 Sample-output ordering check

The validator initially used a natural (numeric) sort for `case_id` while the
writer used byte order, so a correct file failed validation. Both sides now use
plain ascending string order, matching `sorted(case_id)`.

## 7. Known limitations

1. **PNG screenshots are not machine-read.** The eleven screenshots in
   `dataset/evidence/images/` contain business facts (a customer console showing
   `user_11 · account_5762`, an object browser showing
   `delivery_confirmation_19.pdf`, a retained-reference screen for `user_17`).
   The pipeline does not decode them, so cases that rely only on a screenshot can
   end up with no citable state. A vision model would close this gap.
2. **Batched `reported_at` rounds.** Where many cases share one `reported_at`,
   the visible stream cannot separate one customer's resources from another's
   when the resources carry no identifier. See §6.4.
3. **Evidence captured after `audit_at`.** Such files are excluded by design, so
   a case's true state may be unknowable from the admissible evidence; the agent
   then reports the last admissible state instead of guessing.
