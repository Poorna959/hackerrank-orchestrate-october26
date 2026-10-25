"""Minimal synthetic datasets for end-to-end evaluation cases.

Each fixture is a complete, tiny dataset written with the *real* schema: the same
CSV headers, the same evidence manifest and the same evidence dialects that
``dataset/`` uses (SQL snapshots, JSON record sets, logs, handover text).  That
lets an evaluation case exercise the production engine plus the production
writer and validator, instead of calling a helper in isolation.

Fixtures are built inside a caller-supplied temporary directory, so no test or
evaluation run ever mutates ``dataset/`` or the repository's ``output.csv``.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_CODE_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from audit.engine import AuditEngine  # noqa: E402
from audit.identity import load_requests  # noqa: E402
from audit.validator import ValidationReport, validate_output  # noqa: E402
from audit.models import CaseResult  # noqa: E402

REQUESTS_HEADER = [
    "case_id",
    "email",
    "requested_at",
    "reported_status",
    "reported_at",
    "audit_at",
    "request_text",
]
IDENTITIES_HEADER = ["user_id", "name", "email", "phone", "email_hash", "updated_at"]
AUDIT_HEADER = [
    "audit_id",
    "user_id",
    "event_time",
    "field_name",
    "old_value",
    "new_value",
]
ACTIVITY_HEADER = [
    "activity_id",
    "event_time",
    "event_type",
    "source_system",
    "target_system",
    "resource_id",
    "reported_result",
    "details",
]
EVIDENCE_HEADER = ["file_id", "file_path", "origin", "captured_at"]
RULES_HEADER = ["rule_id", "data_category", "condition", "required_action"]

#: Subset of ``deletion_rules.csv`` covering the categories the fixtures use.
DEFAULT_RULES: Tuple[Tuple[str, str, str, str], ...] = (
    ("rule_01", "USER_PROFILE", "Always", "DELETE"),
    ("rule_02", "SECONDARY_IDENTIFIER", "Always", "DELETE"),
    ("rule_03", "SEARCH_INDEX_ENTRY", "Always", "DELETE"),
    ("rule_05", "SUPPORT_MESSAGE", "Conversation context must be retained", "REDACT"),
    ("rule_06", "SUPPORT_ATTACHMENT", "Attachment belongs to the subject", "DELETE"),
    ("rule_07", "ANALYTICS_PROFILE", "Record is still attributable", "ANONYMIZE"),
    ("rule_12", "SECURITY_AUDIT_LOG", "Within the retention period", "RETAIN"),
    ("rule_14", "BACKUP_ARCHIVE", "Restore access can be blocked", "RETAIN"),
    ("rule_15", "ACCOUNT_TOMBSTONE", "Only the stable reference is needed", "REDACT"),
)

_DELETE_REQUEST = (
    "Please delete my account and all personal data associated with it."
)


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(list(header))
        for row in rows:
            writer.writerow(list(row))


def sql_snapshot(*, captured_at: str, header: str, statements: Sequence[str]) -> str:
    """A tiny SQLite-compatible snapshot with the corpus' header comment."""

    body = "\n".join(statements)
    return f"-- Captured at: {captured_at}\n-- {header}\n{body}\n"


def record_set(records: Sequence[dict]) -> str:
    return json.dumps({"records": list(records)}, indent=2)


@dataclass
class FixtureDataset:
    """A dataset being assembled on disk."""

    root: Path
    requests: List[Tuple[object, ...]] = field(default_factory=list)
    identities: List[Tuple[object, ...]] = field(default_factory=list)
    audit_rows: List[Tuple[object, ...]] = field(default_factory=list)
    activity_rows: List[Tuple[object, ...]] = field(default_factory=list)
    evidence_rows: List[Tuple[object, ...]] = field(default_factory=list)
    rules: Tuple[Tuple[str, str, str, str], ...] = DEFAULT_RULES
    _files: Dict[str, str] = field(default_factory=dict)

    # -- authoring helpers ---------------------------------------------
    def add_request(
        self,
        case_id: str,
        email: str,
        *,
        audit_at: str = "2026-08-10T00:00:00Z",
        reported_status: str = "COMPLETE",
        reported_at: str = "2026-08-05T00:00:00Z",
        requested_at: str = "2026-08-01T00:00:00Z",
        request_text: str = _DELETE_REQUEST,
    ) -> None:
        self.requests.append(
            (case_id, email, requested_at, reported_status, reported_at, audit_at, request_text)
        )

    def add_identity(
        self,
        user_id: str,
        *,
        name: str = "",
        email: str = "",
        phone: str = "",
        email_hash: str = "",
        updated_at: str = "2026-07-01T00:00:00Z",
    ) -> None:
        self.identities.append((user_id, name, email, phone, email_hash, updated_at))

    def add_audit_event(
        self,
        audit_id: str,
        user_id: str,
        field_name: str,
        old_value: str,
        new_value: str,
        *,
        event_time: str = "2026-01-01T00:00:00Z",
    ) -> None:
        self.audit_rows.append(
            (audit_id, user_id, event_time, field_name, old_value, new_value)
        )

    def add_activity(
        self,
        activity_id: str,
        event_time: str,
        event_type: str,
        *,
        source_system: str = "deletion_worker",
        target_system: str = "account_service",
        resource_id: str = "",
        reported_result: str = "SUCCESS",
        details: str = "",
    ) -> None:
        self.activity_rows.append(
            (
                activity_id,
                event_time,
                event_type,
                source_system,
                target_system,
                resource_id,
                reported_result,
                details,
            )
        )

    def add_evidence(
        self,
        file_id: str,
        *,
        name: str,
        origin: str,
        captured_at: str,
        content: str,
    ) -> None:
        relative = f"evidence/{name}"
        self._files[relative] = content
        self.evidence_rows.append((file_id, relative, origin, captured_at))

    # -- build / run ----------------------------------------------------
    def build(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        _write_csv(self.root / "deletion_requests.csv", REQUESTS_HEADER, self.requests)
        _write_csv(self.root / "user_identities.csv", IDENTITIES_HEADER, self.identities)
        _write_csv(self.root / "user_audit_log.csv", AUDIT_HEADER, self.audit_rows)
        _write_csv(self.root / "system_activity.csv", ACTIVITY_HEADER, self.activity_rows)
        _write_csv(self.root / "evidence_files.csv", EVIDENCE_HEADER, self.evidence_rows)
        _write_csv(self.root / "deletion_rules.csv", RULES_HEADER, self.rules)
        for relative, content in self._files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return self.root

    def engine(self, *, ocr: bool = False) -> AuditEngine:
        return AuditEngine(self.build(), ocr=ocr)

    def audit(self, case_id: str, *, ocr: bool = False) -> CaseResult:
        engine = self.engine(ocr=ocr)
        requests = {request.case_id: request for request in engine.requests}
        return engine.audit_case(requests[case_id])

    def run_pipeline(self) -> Tuple[List[dict], ValidationReport, Path]:
        """Run the real engine, writer and validator over this fixture."""

        from main import _write_output  # the production serialiser

        root = self.build()
        engine = AuditEngine(root)
        results = engine.run()
        rows = [result.as_row() for result in sorted(results, key=lambda r: r.case_id)]
        output_path = root / "output.csv"
        _write_output(rows, output_path)
        report = validate_output(
            output_path,
            root / "deletion_requests.csv",
            evidence_path=root / "evidence_files.csv",
            activity_path=root / "system_activity.csv",
            audit_path=root / "user_audit_log.csv",
        )
        return rows, report, output_path


def step_tuples(result: CaseResult) -> List[Tuple[str, str, str, str, str, str]]:
    """Compact view of a case's steps for assertions and failure messages."""

    return [
        (
            step.phase,
            step.system_id,
            step.resource_id,
            step.operation,
            step.state,
            step.evidence_id,
        )
        for step in result.steps
    ]


def load_row(output_path: Path, case_id: str) -> Optional[dict]:
    with output_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["case_id"] == case_id:
                return row
    return None
