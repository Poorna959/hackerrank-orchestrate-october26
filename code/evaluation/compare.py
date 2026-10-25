#!/usr/bin/env python3
"""Compare a prediction CSV with a reference CSV (e.g. the sample answers).

Usage::

    python3 code/evaluation/compare.py dataset/sample_output.csv /tmp/sample.csv

Reports case-level status accuracy plus step-level, field-level and sequence
accuracy, and prints a per-case diff for every mismatch.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, List

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def _load(path: Path) -> Dict[str, dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    out = {}
    for row in rows:
        steps = row.get("steps_json") or "[]"
        try:
            parsed = json.loads(steps)
        except json.JSONDecodeError:
            parsed = []
        out[row["case_id"]] = {
            "status": (row.get("deletion_status") or "").strip(),
            "steps": parsed,
        }
    return out


def _key(step: dict) -> tuple:
    return tuple(str(step.get(field, "")) for field in FIELDS)


def _fmt(step: dict) -> str:
    return " | ".join(f"{field}={step.get(field, '')}" for field in FIELDS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("prediction", type=Path)
    parser.add_argument("--quiet-cases", action="store_true")
    args = parser.parse_args(argv)

    gold = _load(args.reference)
    pred = _load(args.prediction)

    cases = sorted(gold)
    status_ok = 0
    field_total = 0
    field_ok = 0
    step_total = 0
    step_ok = 0
    seq_ok = 0
    missing = [cid for cid in cases if cid not in pred]
    extra = [cid for cid in pred if cid not in gold]

    problems: Dict[str, List[str]] = {}
    for case_id in cases:
        expected = gold[case_id]
        actual = pred.get(case_id)
        notes: List[str] = []
        if actual is None:
            problems[case_id] = ["missing case in prediction"]
            continue
        if expected["status"] == actual["status"]:
            status_ok += 1
        else:
            notes.append(f"status: expected {expected['status']}, got {actual['status']}")

        want = expected["steps"]
        have = actual["steps"]
        if [_key(step) for step in want] == [_key(step) for step in have]:
            seq_ok += 1
        step_total += max(len(want), len(have))
        want_keys = [_key(step) for step in want]
        have_keys = [_key(step) for step in have]
        for index, key in enumerate(want_keys):
            field_total += len(FIELDS)
            if index < len(have_keys):
                got = have_keys[index]
                for position, field in enumerate(FIELDS):
                    if got[position] == key[position]:
                        field_ok += 1
                if got == key:
                    step_ok += 1
                else:
                    notes.append(f"  step {index + 1}: expected {key} got {got}")
            else:
                notes.append(f"  step {index + 1}: missing, expected {key}")
        for index in range(len(want_keys), len(have_keys)):
            notes.append(f"  step {index + 1}: unexpected {have_keys[index]}")
        if notes and not args.quiet_cases:
            problems[case_id] = notes

    print(f"cases: {len(cases)} (missing {len(missing)}, extra {len(extra)})")
    if cases:
        print(f"case status accuracy:      {status_ok}/{len(cases)} = {status_ok / len(cases):.3f}")
    if step_total:
        print(f"step (aligned) accuracy:   {step_ok}/{step_total} = {step_ok / step_total:.3f}")
    if field_total:
        print(f"field accuracy:            {field_ok}/{field_total} = {field_ok / field_total:.3f}")
    if cases:
        print(f"exact-sequence accuracy:   {seq_ok}/{len(cases)} = {seq_ok / len(cases):.3f}")
    if missing:
        print(f"missing: {missing[:10]}")
    if extra:
        print(f"extra: {extra[:10]}")

    for case_id, notes in problems.items():
        print(f"\n=== {case_id} ===")
        for note in notes:
            print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
