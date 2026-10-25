"""Core data models for the Ghost Data deletion-audit pipeline.

The pipeline is deliberately deterministic: every value produced for
``output.csv`` is derived from participant-facing evidence files in
``dataset/`` plus the policy table ``deletion_rules.csv``.  No model calls and
no network access are required at prediction time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Audit phases / operations (mirrors problem_statement.md exactly)
# ---------------------------------------------------------------------------

PHASES: Tuple[str, ...] = ("FINDING", "REMEDIATION", "VERIFICATION")

FINDING_OPERATIONS: Tuple[str, ...] = (
    "REPORT_REMAINING_RECORD",
    "REPORT_EXPOSED_PII",
    "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
)

REMEDIATION_OPERATIONS: Tuple[str, ...] = (
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
)

VERIFICATION_OPERATIONS: Tuple[str, ...] = (
    "VERIFY_RECORD_ABSENT",
    "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
)

OPERATION_ORDER: Dict[str, int] = {}
for _phase_ops in (FINDING_OPERATIONS, REMEDIATION_OPERATIONS, VERIFICATION_OPERATIONS):
    for _op in _phase_ops:
        OPERATION_ORDER[_op] = len(OPERATION_ORDER)

OPERATION_STATE: Dict[str, str] = {
    "REPORT_REMAINING_RECORD": "PRESENT",
    "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED",
    "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED",
    "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT",
    "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

OPERATION_PHASE: Dict[str, str] = {}
for _op in FINDING_OPERATIONS:
    OPERATION_PHASE[_op] = "FINDING"
for _op in REMEDIATION_OPERATIONS:
    OPERATION_PHASE[_op] = "REMEDIATION"
for _op in VERIFICATION_OPERATIONS:
    OPERATION_PHASE[_op] = "VERIFICATION"

# ---------------------------------------------------------------------------
# Resource states
# ---------------------------------------------------------------------------

#: Record exists with the personal data the policy cares about still attached.
PRESENT = "PRESENT"
#: Record does not exist in the observed system.
ABSENT = "ABSENT"
#: Record exists, the identifying fields required by policy were removed.
REDACTED = "REDACTED"
#: Record exists but can no longer be linked to the subject.
ANONYMIZED = "ANONYMIZED"
#: A connector / sync process that could recreate data is switched off.
DISABLED = "DISABLED"
#: A backup archive cannot be restored into the live estate.
BLOCKED = "BLOCKED"
#: A backup archive can still be restored (recreation risk).
RESTORE_ALLOWED = "RESTORE_ALLOWED"

# ---------------------------------------------------------------------------
# Data categories and the required treatment from deletion_rules.csv
# ---------------------------------------------------------------------------

DELETE = "DELETE"
REDACT = "REDACT"
ANONYMIZE = "ANONYMIZE"
RETAIN = "RETAIN"

#: Fallback policy table.  ``deletion_rules.csv`` is parsed at run time and the
#: values found there take precedence; this mapping documents the categories the
#: engine knows how to evaluate and keeps behaviour stable if a category is
#: missing from a reduced dataset.
DEFAULT_CATEGORY_ACTION: Dict[str, str] = {
    "USER_PROFILE": DELETE,
    "SECONDARY_IDENTIFIER": DELETE,
    "SEARCH_INDEX_ENTRY": DELETE,
    "CACHE_ENTRY": DELETE,
    "SUPPORT_MESSAGE": REDACT,
    "SUPPORT_ATTACHMENT": DELETE,
    "ANALYTICS_PROFILE": ANONYMIZE,
    "PRODUCT_TELEMETRY": ANONYMIZE,
    "USER_EXPORT": DELETE,
    "MARKETING_PROFILE": DELETE,
    "EMAIL_DELIVERY_LOG": REDACT,
    "SECURITY_AUDIT_LOG": RETAIN,
    "LEGAL_HOLD_RECORD": RETAIN,
    "BACKUP_ARCHIVE": RETAIN,
    "ACCOUNT_TOMBSTONE": REDACT,
    "DATA_FLOW": "CONTROL",
}

#: Categories whose live records must be removed entirely.
DELETABLE_CATEGORIES = frozenset(
    c for c, a in DEFAULT_CATEGORY_ACTION.items() if a == DELETE
)

#: Resource-id prefix -> (system_id, data_category).  Used when an evidence
#: format records a resource id but not the owning system.
RESOURCE_PREFIX_MAP: Dict[str, Tuple[str, str]] = {
    "account_": ("account_service", "USER_PROFILE"),
    "search_": ("search_service", "SEARCH_INDEX_ENTRY"),
    "cache_": ("cache_service", "CACHE_ENTRY"),
    "support_": ("support_platform", "SUPPORT_MESSAGE"),
    "analytics_": ("analytics_platform", "ANALYTICS_PROFILE"),
    "export_": ("export_service", "USER_EXPORT"),
    "marketing_": ("marketing_platform", "MARKETING_PROFILE"),
    "email_delivery_": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
    "audit_record_": ("security_service", "SECURITY_AUDIT_LOG"),
    "legal_": ("legal_service", "LEGAL_HOLD_RECORD"),
    "backup_": ("backup_service", "BACKUP_ARCHIVE"),
    "data_flow_": ("integration_service", "DATA_FLOW"),
    "tombstone_": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "object_": ("object_storage", "SUPPORT_ATTACHMENT"),
    "telemetry_": ("product_telemetry", "PRODUCT_TELEMETRY"),
}

#: system_id -> (default category, resource prefix)
SYSTEM_CATEGORY_MAP: Dict[str, str] = {
    "account_service": "USER_PROFILE",
    "account_failover_replica": "USER_PROFILE",
    "account_replication_cluster": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "support_platform": "SUPPORT_MESSAGE",
    "support_replica": "SUPPORT_MESSAGE",
    "analytics_platform": "ANALYTICS_PROFILE",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "integration_service": "DATA_FLOW",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "object_storage": "SUPPORT_ATTACHMENT",
    "product_telemetry": "PRODUCT_TELEMETRY",
}

#: Canonical system for a given data category (used to normalise replica names).
CATEGORY_SYSTEM_MAP: Dict[str, str] = {
    "USER_PROFILE": "account_service",
    "SEARCH_INDEX_ENTRY": "search_service",
    "CACHE_ENTRY": "cache_service",
    "SUPPORT_MESSAGE": "support_platform",
    "SUPPORT_ATTACHMENT": "support_platform",
    "ANALYTICS_PROFILE": "analytics_platform",
    "PRODUCT_TELEMETRY": "product_telemetry",
    "USER_EXPORT": "export_service",
    "MARKETING_PROFILE": "marketing_platform",
    "EMAIL_DELIVERY_LOG": "email_delivery_service",
    "SECURITY_AUDIT_LOG": "security_service",
    "LEGAL_HOLD_RECORD": "legal_service",
    "BACKUP_ARCHIVE": "backup_service",
    "DATA_FLOW": "integration_service",
    "ACCOUNT_TOMBSTONE": "identity_service",
}


# ---------------------------------------------------------------------------
# Input / output records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeletionRequest:
    """One row of ``deletion_requests.csv``."""

    case_id: str
    email: str
    requested_at: datetime
    reported_status: str
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True)
class UserIdentity:
    """One row of ``user_identities.csv`` plus audit-log history."""

    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime
    historical: Tuple["IdentityValue", ...] = ()

    @property
    def all_emails(self) -> Tuple[str, ...]:
        values = {self.email} if self.email else set()
        values.update(v.value for v in self.historical if v.field == "email")
        return tuple(sorted(v for v in values if v))

    @property
    def all_phones(self) -> Tuple[str, ...]:
        values = {self.phone} if self.phone else set()
        values.update(v.value for v in self.historical if v.field == "phone")
        return tuple(sorted(v for v in values if v))

    @property
    def all_names(self) -> Tuple[str, ...]:
        values = {self.name} if self.name else set()
        values.update(v.value for v in self.historical if v.field == "name")
        return tuple(sorted(v for v in values if v))

    @property
    def all_email_hashes(self) -> Tuple[str, ...]:
        values = {self.email_hash} if self.email_hash else set()
        values.update(v.value for v in self.historical if v.field == "email_hash")
        return tuple(sorted(v for v in values if v))


@dataclass(frozen=True)
class IdentityValue:
    """One historical value recorded in ``user_audit_log.csv``."""

    field: str
    value: str
    effective_from: datetime
    audit_id: str


@dataclass(frozen=True)
class Observation:
    """A statement about the state of one resource, taken from one evidence file.

    ``observed_at`` is the record-level timestamp when the source provides one
    (for example a readback trace line); otherwise it is the capture time of the
    enclosing evidence file.
    """

    file_id: str
    captured_at: datetime
    observed_at: datetime
    system_id: str
    resource_id: str
    category: str
    state: str
    link_keys: FrozenSet[str] = frozenset()
    identifiers: FrozenSet[str] = frozenset()
    detail: str = ""
    source: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return (self.system_id, self.resource_id)

    def sort_key(self) -> Tuple[datetime, datetime, str, str]:
        return (self.observed_at, self.captured_at, self.system_id, self.file_id)


@dataclass
class ResourceState:
    """Resolved state of one resource at a case's ``audit_at`` cutoff."""

    system_id: str
    resource_id: str
    category: str
    state: str
    evidence_id: str
    observed_at: datetime
    identifiers: FrozenSet[str] = frozenset()
    detail: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return (self.system_id, self.resource_id)


@dataclass(frozen=True)
class Step:
    """One element of ``steps_json``."""

    step_no: int
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "step_no": self.step_no,
            "phase": self.phase,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


@dataclass
class CaseResult:
    """Final decision for one case."""

    case_id: str
    deletion_status: str
    steps: List[Step] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def as_row(self) -> Dict[str, str]:
        from .planner import steps_to_json

        return {
            "case_id": self.case_id,
            "deletion_status": self.deletion_status,
            "steps_json": steps_to_json(self.steps),
        }
