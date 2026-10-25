# Usage report — final full-dataset run

## 1. Run that produced `output.csv`

| Item | Value |
| --- | --- |
| Command | `python3 code/main.py` (run from the repository root) |
| Input | `dataset/deletion_requests.csv` (518 cases) plus `user_identities.csv`, `user_audit_log.csv`, `deletion_rules.csv`, `system_activity.csv`, `evidence_files.csv` and 94 evidence files |
| Output | `output.csv` at the repository root — 518 data rows, 3149 steps |
| Wall clock | 7.9 / 10.7 / 11.2 / 15.0 s across four timed full runs, single process (`time`), varying with machine load |
| Runtime | CPython 3.14.4 |
| Dependencies | standard library only — no `pip install`, no virtualenv, no compiled extension |
| Network access | none (no HTTP client in the pipeline) |
| Model calls | **0** |
| Exit status | 0 (validator: 0 structural issues, 0 semantic issues, 9 warnings) |

## 2. Model providers, model names and model calls

| Provider | Model | Calls in the final run |
| --- | --- | --- |
| none | none | **0** |

The delivered agent is a deterministic audit engine: it resolves identifiers,
parses evidence, reconstructs state at `audit_at`, applies the policy table and
plans the steps in ordinary Python. The final run makes **no inference calls** —
no chat completion, no embedding, no OCR (the OCR path is opt-in and was off), no
reranking.

## 3. Token usage for the final run

| Metric | Value |
| --- | --- |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Requests processed | 518 |
| Average tokens per request | 0.0 |
| Median tokens per request | 0 |

## 4. Estimated cost for the final run

| Metric | Value |
| --- | --- |
| Estimated total cost | **$0.00** |
| Estimated cost per request | **$0.00** |
| Per-model totals | not applicable — no provider is invoked |

Because no provider is invoked, there is no per-model breakdown to report and no
price table to apply. A cost figure derived from a hypothetical token count would
be fabricated, so none is given.

## 5. Where the compute time goes instead

Measured with an instrumented run of the same pipeline (10.78 s total: 518 cases,
one process, engine construction timed separately from case resolution):

| Stage | Measured | Why |
| --- | --- | --- |
| Load + parse the 94 evidence files once | 8.49 s | SQL snapshots, JSON exports, logs and PDFs are parsed up front |
| Resolve all 518 cases against the index | 2.21 s | observations are already indexed by identifier and by resource id |
| Serialise + write `output.csv` | 0.08 s | pure functions, no I/O per case |
| Validator pass over the written file | included in the CLI run | re-reads `output.csv` and the dataset CSVs |

Two supporting commands are also measured: `python3 code/evaluation/main.py`
takes about 9 s (it re-runs the engine over the sample plus 8 synthetic fixtures)
and `python3 -m unittest discover -s code/tests -v` takes 1.35 s.

## 6. The optional OCR path (not used for `output.csv`)

For completeness, since it is the only run that could consume external compute:
`python3 code/main.py --ocr` shells out to a locally installed `tesseract`
(a local binary, no network, no API key). Measured here with tesseract 5.5.0, that
run takes 34.3 s instead of 7.9 s, still makes **0 model calls and 0 API calls**,
and still costs **$0.00**. It changed 5 of the 518 rows, so it is not the
configuration that produced the submitted file (see the evaluation report §8).

## 7. Development-time model usage

The coding agent that built this solution used an assistant model during
development, not during the run. This environment exposes no provider-side usage
record (token counts, per-call breakdown, price), so no input/output token counts
or costs are claimed for that work. The figures that can be stated factually are
the run-level ones in §1–§4, all of which are zero-token, zero-cost.

## 8. Reproducing these numbers

```bash
python3 code/main.py                          # writes output.csv, prints distribution + validator
python3 code/main.py --quiet                  # same, less output
python3 code/evaluation/main.py               # sample metrics + 11 cases
python3 -m unittest discover -s code/tests    # 53 tests
```

Any process that repeats the final run observes the same zero-token, zero-API-cost
profile, because the pipeline contains no network client and no model SDK.
