# HackerRank Orchestrate - October 2026

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

Build your solution in `code/main.py`, or use another language and document its setup and run command clearly in your README.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.

---

## Solution overview

This repository contains a deterministic deletion-audit agent. It reads only the
participant-facing files in `dataset/`, reconstructs the state of every record it
can tie to the subject at the case's `audit_at`, applies `deletion_rules.csv`, and
plans the ordered steps. No network access, no API keys and no model calls are
required to produce the answers.

```text
code/
├── main.py                 entry point: reads dataset/, writes output.csv, validates it
├── audit/
│   ├── models.py           states, categories, policy defaults, step/result types
│   ├── identity.py         current + historical identity resolution (email, hash, phone, project reference)
│   ├── policy.py           deletion_rules.csv loader and request-intent classifier
│   ├── engine.py           per-case state reconstruction at audit_at
│   ├── planner.py          phase/operation ordering and steps_json serialisation
│   ├── validator.py        output contract checks (structural + semantic)
│   └── evidence/
│       ├── loader.py       manifest walk, identifier linking, per-resource index
│       ├── sql_source.py   SQLite-compatible snapshots (primary / replica / failover)
│       ├── json_source.py  service exports, snapshot bundles, flat record sets
│       ├── log_source.py   operational logs, syslog, cloud-runtime and trace lines
│       ├── textparse.py    shared timestamp / email / hash helpers
│       ├── pdf_source.py   PDF text extraction for business documents
│       └── ocr_source.py   optional local-OCR reader for screenshots (off by default)
├── tests/                  unittest suite (parsers, engine, validator)
└── evaluation/
    ├── main.py             sample scoring + in-process checks + end-to-end cases
    ├── compare.py          reference-vs-prediction scorer (status, steps, fields, sequence)
    ├── fixtures.py         minimal synthetic datasets used by the end-to-end cases
    ├── cases.py            eight end-to-end evaluation cases
    ├── baseline_report.md  measured state of the checkout before the hardening pass
    ├── evaluation_report.md
    ├── final_report.md
    └── usage_report.md
```

### How a case is decided

1. **Intent.** `request_text` is classified first. Access, correction, export,
   marketing-opt-out and similar wording is `NOT_APPLICABLE` with `steps_json=[]`,
   in any of the languages that appear in the corpus.
2. **Identity.** The request email is resolved against the current snapshot and
   the audit log, producing every email, phone, email hash, name and stable
   `user_id` the person has ever used. Evidence is linked through those keys, and
   through resource ids for records that carry no identifier (a lookup that now
   returns 404, an anonymized analytics row).
3. **State at `audit_at`.** Evidence captured after the cut-off is discarded.
   Replica tables are kept as separate physical locations, so a copy that
   survives on a failover/reporting/read replica is still a finding even when the
   primary is clean. Absence is only inferred from authoritative captures: an
   inventory exporting a `snapshot_scope`, or a `*_primary` table.
4. **Policy.** Each record is mapped to a data category and compared with
   `deletion_rules.csv` (delete, redact, anonymize, retain, or preserve a
   tombstone with only the stable reference).
5. **Plan.** Findings cite the file, activity or audit row that proves them;
   remediation and post-remediation verification are uncited future work. Phases
   are ordered `FINDING` → `REMEDIATION` → `VERIFICATION`, then by operation
   order, then `system_id`, then `resource_id`.

Two decisions are deliberately conservative. An authorised deletion whose
subject cannot be resolved from the supplied snapshots is `INCOMPLETE`, never
`COMPLETE`: the workflow's claim is not proof, and no resource or evidence id is
invented to fill the gap. The same rule applies when no admissible evidence
proves the final state at all - the case stays open instead of returning an
empty `COMPLETE` checklist.

### Verification

```bash
python3 code/main.py                       # 518 rows + contract validation
python3 code/main.py --requests dataset/sample_deletion_requests.csv --out /tmp/sample.csv
python3 code/evaluation/compare.py dataset/sample_output.csv /tmp/sample.csv
python3 code/evaluation/main.py            # sample metrics + 11 custom cases
python3 -m unittest discover -s code/tests -v   # 53 unit/regression tests
```

On the 32 public sample requests the agent scores 32/32 on deletion status,
0.866 on step fields, 0.682 on aligned steps and 0.406 on exact step sequence;
see `code/evaluation/evaluation_report.md` for the breakdown and the known
limits. `--out` is accepted as an alias for `--output`, and `--ocr` opts in to
reading screenshot evidence with a locally installed `tesseract` (off by
default, so a plain run stays byte-for-byte reproducible on a machine without
OCR tooling).


## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
code/evaluation/evaluation_report.md
code/evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` inside `code/evaluation/` (matching `code/evaluation/main.py` in this starter layout), write your complete evaluation metrics, custom evaluation test cases, and token/cost breakdowns directly inside those Markdown files, and keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

## Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

## Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

To package `code.zip` from the repository root without bundling the dataset:

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in `code/evaluation/` so they are bundled automatically with `code` (or add `evaluation` to the archive command above if you placed them in a top-level `evaluation/` directory).
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `code/evaluation/evaluation_report.md`, and `code/evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
