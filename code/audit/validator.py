"""Independent validator for the generated ``output.csv``.

The validator deliberately re-reads the produced file from disk and re-derives
every requirement from the problem statement instead of trusting the pipeline.
It returns a structured report so ``main.py`` can fail loudly on any violation.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import (
    OPERATION_ORDER,
    OPERATION_PHASE,
    OPERATION_STATE,
    PHASES,
)

ALLOWED_STATUS = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
REQUIRED_COLUMNS = ["case_id", "deletion_status", "steps_json"]
STEP_COLUMNS = [
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
]


@dataclass
class ValidationReport:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rows: int = 0
    cases: int = 0
    steps: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, message: str) -> None:
        self.errors.append(message)

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return (
            f"validation {status}: {self.rows} rows, {self.cases} cases, "
            f"{self.steps} steps, {len(self.errors)} error(s), "
            f"{len(self.warnings)} warning(s)"
        )


def _natural_key(case_id: str) -> Tuple[str, int, str]:
    match = re.match(r"^(.*?)(\d+)$", case_id)
    if match:
        return (match.group(1), int(match.group(2)), "")
    return (case_id, -1, case_id)


def validate_output(
    output_path: Path,
    requests_path: Path,
    evidence_path: Optional[Path] = None,
    activity_path: Optional[Path] = None,
    audit_path: Optional[Path] = None,
) -> ValidationReport:
    report = ValidationReport()

    if not output_path.exists():
        report.add(f"output file not found: {output_path}")
        return report
    if not requests_path.exists():
        report.add(f"requests file not found: {requests_path}")
        return report

    with requests_path.open(newline="", encoding="utf-8-sig") as handle:
        expected_ids = [row["case_id"] for row in csv.DictReader(handle)]
    report.cases = len(expected_ids)

    valid_evidence = _evidence_ids(evidence_path, activity_path, audit_path)

    with output_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            report.add("output file is empty")
            return report
        if header != REQUIRED_COLUMNS:
            report.add(f"header must be {REQUIRED_COLUMNS}, found {header}")
            return report
        seen: List[str] = []
        for line_no, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            report.rows += 1
            if len(row) != 3:
                report.add(f"line {line_no}: expected 3 columns, found {len(row)}")
                continue
            case_id, status, steps_json = row
            seen.append(case_id)
            if status not in ALLOWED_STATUS:
                report.add(f"{case_id}: invalid deletion_status {status!r}")
                continue
            try:
                steps = json.loads(steps_json)
            except json.JSONDecodeError as exc:
                report.add(f"{case_id}: steps_json is not valid JSON ({exc})")
                continue
            if not isinstance(steps, list):
                report.add(f"{case_id}: steps_json must be a JSON array")
                continue
            report.steps += len(steps)
            _validate_steps(case_id, status, steps, valid_evidence, report)

    if report.rows != len(expected_ids):
        report.add(f"expected {len(expected_ids)} data rows, found {report.rows}")
    duplicates = {cid for cid in seen if seen.count(cid) > 1}
    if duplicates:
        report.add(f"duplicate case_id(s): {sorted(duplicates)[:5]}")
    missing = set(expected_ids) - set(seen)
    extra = set(seen) - set(expected_ids)
    if missing:
        report.add(f"missing case_id(s): {sorted(missing)[:5]} ({len(missing)} total)")
    if extra:
        report.add(f"unexpected case_id(s): {sorted(extra)[:5]}")
    if seen != sorted(seen):
        report.add("rows are not sorted by case_id ascending")
    return report


def _validate_steps(
    case_id: str,
    status: str,
    steps: Sequence[object],
    valid_evidence: Optional[Set[str]],
    report: ValidationReport,
) -> None:
    if status == "NOT_APPLICABLE":
        if steps:
            report.add(f"{case_id}: NOT_APPLICABLE must use an empty steps array")
        return
    if status == "COMPLETE":
        if not steps:
            report.warnings.append(f"{case_id}: COMPLETE with no verification steps")
        for step in steps:
            if not isinstance(step, dict):
                report.add(f"{case_id}: step is not a JSON object")
                return
            if step.get("phase") != "VERIFICATION":
                report.add(f"{case_id}: COMPLETE may only contain VERIFICATION steps")
                break
    if status == "INCOMPLETE":
        if not any(isinstance(s, dict) and s.get("phase") == "FINDING" for s in steps):
            report.add(f"{case_id}: INCOMPLETE requires at least one FINDING step")

    previous_key: Optional[Tuple[int, int, str, str]] = None
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            report.add(f"{case_id}: step {index} is not a JSON object")
            return
        keys = list(step.keys())
        if sorted(keys) != sorted(STEP_COLUMNS):
            report.add(f"{case_id}: step {index} has unexpected fields {sorted(keys)}")
            return
        if step.get("step_no") != index:
            report.add(f"{case_id}: step numbers must be consecutive from 1 (step {index})")
        phase = step.get("phase")
        operation = step.get("operation")
        state = step.get("state")
        if phase not in PHASES:
            report.add(f"{case_id}: step {index} invalid phase {phase!r}")
            return
        if operation not in OPERATION_ORDER:
            report.add(f"{case_id}: step {index} invalid operation {operation!r}")
            return
        if OPERATION_PHASE[operation] != phase:
            report.add(f"{case_id}: step {index} operation {operation} not allowed in {phase}")
        if OPERATION_STATE[operation] != state:
            report.add(
                f"{case_id}: step {index} operation {operation} requires state "
                f"{OPERATION_STATE[operation]}, found {state!r}"
            )
        evidence = step.get("evidence_id")
        if not isinstance(evidence, str):
            report.add(f"{case_id}: step {index} evidence_id must be a string")
            return
        if phase == "FINDING":
            if not evidence:
                report.add(f"{case_id}: step {index} FINDING requires an evidence reference")
            elif valid_evidence is not None and evidence not in valid_evidence:
                report.add(f"{case_id}: step {index} unknown evidence id {evidence!r}")
        if phase == "VERIFICATION":
            if status == "INCOMPLETE" and evidence:
                report.add(f"{case_id}: step {index} future verification must use empty evidence_id")
            if status == "COMPLETE":
                if not evidence:
                    report.add(f"{case_id}: step {index} COMPLETE verification needs evidence")
                elif valid_evidence is not None and evidence not in valid_evidence:
                    report.add(f"{case_id}: step {index} unknown evidence id {evidence!r}")
        if phase == "REMEDIATION" and evidence:
            report.add(f"{case_id}: step {index} remediation must use empty evidence_id")

        system = step.get("system_id")
        resource = step.get("resource_id")
        if not isinstance(system, str) or not system:
            report.add(f"{case_id}: step {index} system_id must be a non-empty string")
        if not isinstance(resource, str) or not resource:
            report.add(f"{case_id}: step {index} resource_id must be a non-empty string")
        key = (
            PHASES.index(phase),
            OPERATION_ORDER[operation],
            str(system),
            str(resource),
        )
        if previous_key is not None and key < previous_key:
            report.add(f"{case_id}: step {index} breaks phase/operation/system ordering")
        previous_key = key


def _evidence_ids(
    evidence_path: Optional[Path],
    activity_path: Optional[Path],
    audit_path: Optional[Path],
) -> Optional[Set[str]]:
    if evidence_path is None and activity_path is None:
        return None
    ids: Set[str] = set()
    if evidence_path is not None and evidence_path.exists():
        with evidence_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("file_id"):
                    ids.add(row["file_id"].strip())
    if activity_path is not None and activity_path.exists():
        with activity_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("activity_id"):
                    ids.add(row["activity_id"].strip())
    if audit_path is not None and audit_path.exists():
        with audit_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("audit_id"):
                    ids.add(row["audit_id"].strip())
    return ids
