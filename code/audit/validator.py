"""Independent validator for the generated ``output.csv``.

The validator deliberately re-reads the produced file from disk and re-derives
every requirement from the problem statement instead of trusting the pipeline.
It returns a structured report so ``main.py`` can fail loudly on any violation.

Checks are split into two groups so a reader can tell "the file is malformed"
from "the file is well formed but argues something the evidence does not
support":

``structural``
    header, row count, one unique sorted row per request, status vocabulary,
    JSON array shape, exact step fields, consecutive numbering, phase/operation
    compatibility, operation/state compatibility, phase ordering and the empty
    arrays required by ``NOT_APPLICABLE``.

``semantic``
    citation rules: findings must cite a real ``file_id``/``activity_id``/
    ``audit_id``, a case may only cite evidence observed at or before its
    ``audit_at``, ``COMPLETE`` must carry evidence-backed verification,
    ``INCOMPLETE`` must not cite evidence on future work, ``REMEDIATION`` must be
    uncited, and no step may be duplicated.

A warning is used where the contract states a goal that the supplied evidence
can make unreachable (for example an ``INCOMPLETE`` case whose remaining data is
visible only in a screenshot).  Warnings never turn into a pass certificate for
a malformed file, and they are never used to excuse a citation of evidence the
agent did not have.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .identity import parse_ts_optional
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
    structural: List[str] = field(default_factory=list)
    semantic: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rows: int = 0
    cases: int = 0
    steps: int = 0

    @property
    def errors(self) -> List[str]:
        return self.structural + self.semantic

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, message: str) -> None:
        self.structural.append(message)

    def add_semantic(self, message: str) -> None:
        self.semantic.append(message)

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return (
            f"validation {status}: {self.rows} rows, {self.cases} cases, "
            f"{self.steps} steps, structural issues={len(self.structural)}, "
            f"semantic issues={len(self.semantic)}, "
            f"warning(s)={len(self.warnings)}"
        )


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

    cutoffs: Dict[str, object] = {}
    with requests_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            case_id = (row.get("case_id") or "").strip()
            if not case_id:
                continue
            cutoffs[case_id] = parse_ts_optional(row.get("audit_at") or "")
    expected_ids = list(cutoffs)
    report.cases = len(expected_ids)

    cited_ids = _evidence_ids(evidence_path, activity_path, audit_path)
    cited_times = _evidence_times(evidence_path, activity_path, audit_path)

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
            _validate_steps(
                case_id,
                status,
                steps,
                cited_ids,
                cited_times,
                cutoffs.get(case_id),
                report,
            )

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
    evidence_times: Dict[str, object],
    cutoff: Optional[object],
    report: ValidationReport,
) -> None:
    if status == "NOT_APPLICABLE":
        if steps:
            report.add(f"{case_id}: NOT_APPLICABLE must use an empty steps array")
        return

    if status == "COMPLETE":
        if not steps:
            report.add_semantic(
                f"{case_id}: COMPLETE requires evidence-backed verification steps"
            )

    if status == "INCOMPLETE":
        if not steps:
            report.warnings.append(
                f"{case_id}: INCOMPLETE with no citable findings (no admissible "
                "evidence identifies the remaining data)"
            )
        elif not any(isinstance(s, dict) and s.get("phase") == "FINDING" for s in steps):
            report.warnings.append(f"{case_id}: INCOMPLETE with no FINDING step")

    previous_key: Optional[Tuple[int, int, str, str]] = None
    seen_steps: Set[Tuple[str, str, str, str]] = set()
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            report.add(f"{case_id}: step {index} is not a JSON object")
            return
        keys = list(step.keys())
        if sorted(keys) != sorted(STEP_COLUMNS):
            report.add(f"{case_id}: step {index} has unexpected fields {sorted(keys)}")
            return
        if step.get("step_no") != index:
            report.add(
                f"{case_id}: step numbers must be consecutive from 1 (step {index})"
            )
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
            report.add(
                f"{case_id}: step {index} operation {operation} not allowed in {phase}"
            )
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
                report.add_semantic(
                    f"{case_id}: step {index} FINDING requires an evidence reference"
                )
            else:
                if valid_evidence is not None and evidence not in valid_evidence:
                    report.add_semantic(
                        f"{case_id}: step {index} unknown evidence id {evidence!r}"
                    )
                _check_admissible(case_id, index, evidence, evidence_times, cutoff, report)
        elif phase == "REMEDIATION":
            if evidence:
                report.add_semantic(
                    f"{case_id}: step {index} remediation must use empty evidence_id"
                )
        else:  # VERIFICATION
            if status == "INCOMPLETE" and evidence:
                report.add_semantic(
                    f"{case_id}: step {index} future verification must use empty evidence_id"
                )
            if status == "COMPLETE":
                if not evidence:
                    report.add_semantic(
                        f"{case_id}: step {index} COMPLETE verification needs evidence"
                    )
                else:
                    if valid_evidence is not None and evidence not in valid_evidence:
                        report.add_semantic(
                            f"{case_id}: step {index} unknown evidence id {evidence!r}"
                        )
                    _check_admissible(
                        case_id, index, evidence, evidence_times, cutoff, report
                    )

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

        duplicate_key = (phase, str(operation), str(system), str(resource))
        if duplicate_key in seen_steps:
            report.add_semantic(f"{case_id}: step {index} duplicates an earlier step")
        seen_steps.add(duplicate_key)


def _check_admissible(
    case_id: str,
    index: int,
    evidence: str,
    evidence_times: Dict[str, object],
    cutoff: Optional[object],
    report: ValidationReport,
) -> None:
    """Reject a citation of evidence that did not exist at ``audit_at``."""

    when = evidence_times.get(evidence)
    if when is None or cutoff is None:
        return
    if when > cutoff:
        report.add_semantic(
            f"{case_id}: step {index} cites {evidence}, which was observed after "
            f"audit_at ({when.isoformat()} > {cutoff.isoformat()})"
        )


def _rows(path: Optional[Path]) -> List[Dict[str, str]]:
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _evidence_ids(
    evidence_path: Optional[Path],
    activity_path: Optional[Path],
    audit_path: Optional[Path],
) -> Optional[Set[str]]:
    if evidence_path is None and activity_path is None and audit_path is None:
        return None
    ids: Set[str] = set()
    for row in _rows(evidence_path):
        file_id = (row.get("file_id") or "").strip()
        if file_id:
            ids.add(file_id)
    for row in _rows(activity_path):
        activity_id = (row.get("activity_id") or "").strip()
        if activity_id:
            ids.add(activity_id)
    for row in _rows(audit_path):
        audit_id = (row.get("audit_id") or "").strip()
        if audit_id:
            ids.add(audit_id)
    return ids


def _evidence_times(
    evidence_path: Optional[Path],
    activity_path: Optional[Path],
    audit_path: Optional[Path],
) -> Dict[str, object]:
    """Map every citable id to the time it became observable."""

    times: Dict[str, object] = {}
    for row in _rows(evidence_path):
        file_id = (row.get("file_id") or "").strip()
        when = parse_ts_optional(row.get("captured_at") or "")
        if file_id and when is not None:
            times[file_id] = when
    for row in _rows(activity_path):
        activity_id = (row.get("activity_id") or "").strip()
        when = parse_ts_optional(row.get("event_time") or "")
        if activity_id and when is not None:
            times[activity_id] = when
    for row in _rows(audit_path):
        audit_id = (row.get("audit_id") or "").strip()
        when = parse_ts_optional(row.get("event_time") or "")
        if audit_id and when is not None:
            times[audit_id] = when
    return times
