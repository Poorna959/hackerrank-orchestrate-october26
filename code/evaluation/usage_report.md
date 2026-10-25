# Usage report — final full-dataset run

## 1. Run that produced `output.csv`

| Item | Value |
| --- | --- |
| Command | `python3 code/main.py` (run from the repository root) |
| Input | `dataset/deletion_requests.csv` (518 cases) plus identities, audit history, rules, activity stream and 94 evidence files |
| Output | `output.csv` at the repository root: 518 data rows, 3173 steps |
| Wall clock | ~3.4 s, single process |
| Network access | none |
| Custom cases | 3/3 pass (`python3 code/evaluation/main.py`) |

## 2. Model providers, model names and model calls

| Provider | Model | Calls in the final run |
| --- | --- | --- |
| none | none | **0** |

The delivered agent is a deterministic audit engine: it resolves identifiers,
parses evidence, reconstructs state at `audit_at`, applies the policy table and
plans the steps with ordinary Python. The final run makes **no inference calls**
— no chat completion, no embedding, no OCR, no reranking.

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
| Per-model totals | not applicable (no model is called) |

Because no provider is invoked, there is no per-model breakdown to report: the
single-row table in §2 is the complete list of models used by the submitted
pipeline.

## 5. Where the model time went instead (development only)

The coding agent that built this solution used an assistant model during
development, not during the run. No provider-side usage record is available to
this environment, so no input/output token counts or costs are claimed for that
work; inventing them would misreport the submission. The figures that can be
stated factually are the run-level ones in §1–§4.

What that development effort bought, in run-time terms:

| Design choice | Effect on the run |
| --- | --- |
| Parse evidence once, index observations by identifier **and** by resource id | one pass over 94 files, ~0.9 s |
| Cache parsed SQL snapshots and log lines in memory | 518 cases share the same parse |
| Pure functions for state resolution and planning | deterministic, replayable, no retries |
| No per-case file I/O beyond the initial load | 3.4 s total for 518 cases |

## 6. Reproducing these numbers

```bash
python3 code/main.py                          # writes output.csv, prints status distribution
python3 code/evaluation/main.py               # sample accuracy + three custom cases
python3 code/main.py --quiet --no-validate    # engine only
```

Any process that repeats the final run will observe the same zero-token, zero-cost
profile, because the pipeline contains no network client and no model SDK.
