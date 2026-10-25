#!/usr/bin/env python3
"""Evaluation harness for the deletion-audit agent.

Runs three things and prints one report:

1. the agent over ``dataset/sample_deletion_requests.csv`` and scores every row
   against ``dataset/sample_output.csv`` (case status, aligned steps, individual
   step fields, and exact step sequence);
2. the ordering rules, checked directly on the planner with synthetic states;
3. three custom evaluation cases written for this project (see
   ``evaluation_report.md``).

Usage::

    python3 code/evaluation/main.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

_CODE_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from audit.engine import AuditEngine  # noqa: E402
from audit.identity import load_requests  # noqa: E402
from audit.models import (  # noqa: E402
    ABSENT,
    ANONYMIZED,
    BLOCKED,
    DISABLED,
    PRESENT,
    RESTORE_ALLOWED,
    DeletionRequest,
    ResourceState,
)
from audit.planner import CasePlanner  # noqa: E402
from audit.policy import DeletionPolicy, authorizes_deletion  # noqa: E402
from evaluation.compare import main as compare_main  # noqa: E402

REPO_ROOT = _CODE_DIR.parent
DATASET = REPO_ROOT / "dataset"


def run_sample(dataset: Path = DATASET) -> Path:
    """Audit the sample requests and write the predictions to a temp file."""

    engine = AuditEngine(dataset)
    engine.requests = load_requests(dataset / "sample_deletion_requests.csv")
    results = engine.run()
    out = Path(tempfile.mkdtemp()) / "sample_predictions.csv"
    import csv

    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["case_id", "deletion_status", "steps_json"],
            lineterminator="\n",
        )
        writer.writeheader()
        for result in sorted(results, key=lambda item: item.case_id):
            writer.writerow(result.as_row())
    return out


# ---------------------------------------------------------------------------
# Custom evaluation cases
# ---------------------------------------------------------------------------


def custom_case_1_non_deletion_request() -> Tuple[str, bool, str]:
    """A request that does not authorise deletion must stay NOT_APPLICABLE.

    The wording is taken from the families that appear in the corpus (access,
    correction, export, marketing opt-out) plus a deletion request that must
    still be recognised as authorising deletion.
    """

    positive = [
        "Please close my account and process the associated personal-data deletion.",
        "Ich möchte mein Konto löschen und alle Daten entfernen.",
        "Supprimez définitivement mon compte, s'il vous plaît.",
    ]
    negative = [
        "Please send me a copy of everything you hold about me.",
        "I would like my phone number on the account corrected.",
        "Please stop sending me marketing email; keep my account.",
        "Veuillez me communiquer mes données personnelles.",
    ]
    ok = all(authorizes_deletion(text) for text in positive) and not any(
        authorizes_deletion(text) for text in negative
    )
    return (
        "C1 non-deletion request detection",
        ok,
        f"{len(positive)} deletion phrasings accepted, {len(negative)} non-deletion phrasings rejected",
    )


def custom_case_2_replica_drift(dataset: Path = DATASET) -> Tuple[str, bool, str]:
    """A profile that survives only on the failover replica is still present.

    ``case_05`` is a request that the workflow reports as complete, while
    ``evidence_54`` (the failover-replica capture) still holds the profile and
    the primary table does not.  The audit must report the remaining record
    instead of trusting the primary capture.
    """

    engine = AuditEngine(dataset)
    requests = {r.case_id: r for r in load_requests(dataset / "sample_deletion_requests.csv")}
    result = engine.audit_case(requests["case_05"])
    findings = [step for step in result.steps if step.phase == "FINDING"]
    ok = (
        result.deletion_status == "INCOMPLETE"
        and any(step.operation == "REPORT_REMAINING_RECORD" for step in findings)
        and any(step.system_id == "account_service" for step in findings)
    )
    detail = f"status={result.deletion_status}, findings={[(s.system_id, s.resource_id, s.operation) for s in findings]}"
    return ("C2 replica drift is a remaining record", ok, detail)


def custom_case_3_remediation_order(dataset: Path = DATASET) -> Tuple[str, bool, str]:
    """Data flows and restore paths are handled before dependent deletes.

    Built from a synthetic state set: an active connector, a restorable backup,
    a search copy and an exposed support message.  The plan must disable the
    flow, block the restore, then delete/redact, and verify in operation order
    with an empty ``evidence_id`` for future work.
    """

    policy = DeletionPolicy.load(DATASET / "deletion_rules.csv")
    planner = CasePlanner(policy)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    request = DeletionRequest(
        case_id="custom_001",
        email="subject@example.test",
        requested_at=now,
        reported_status="COMPLETE",
        reported_at=now,
        audit_at=now,
        request_text="Please delete my account and all personal data.",
    )
    states: Dict[Tuple[str, str], ResourceState] = {}
    for system, resource, category, state in (
        ("integration_service", "data_flow_9001", "DATA_FLOW", PRESENT),
        ("backup_service", "backup_9001", "BACKUP_ARCHIVE", RESTORE_ALLOWED),
        ("search_service", "search_9001", "SEARCH_INDEX_ENTRY", PRESENT),
        ("support_platform", "support_9001", "SUPPORT_MESSAGE", PRESENT),
        ("account_service", "account_9001", "USER_PROFILE", ABSENT),
        ("analytics_platform", "analytics_9001", "ANALYTICS_PROFILE", ANONYMIZED),
    ):
        states[(system, resource)] = ResourceState(
            system_id=system,
            resource_id=resource,
            category=category,
            state=state,
            evidence_id="evidence_test",
            observed_at=now,
        )

    result = planner.build(request, None, states, policy)
    remediation = [s.operation for s in result.steps if s.phase == "REMEDIATION"]
    verification = [s.operation for s in result.steps if s.phase == "VERIFICATION"]
    empty_evidence = all(
        not step.evidence_id for step in result.steps if step.phase != "FINDING"
    )
    expected_remediation = [
        "DISABLE_DATA_FLOW",
        "BLOCK_RESTORE",
        "DELETE_RECORD",
        "REDACT_RECORD",
    ]
    expected_verification = [
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_REDACTED",
        "VERIFY_RECORD_ANONYMIZED",
        "VERIFY_DATA_FLOW_DISABLED",
        "VERIFY_RESTORE_BLOCKED",
    ]
    ok = (
        result.deletion_status == "INCOMPLETE"
        and remediation == expected_remediation
        and verification == expected_verification
        and empty_evidence
        and [step.step_no for step in result.steps] == list(range(1, len(result.steps) + 1))
    )
    return (
        "C3 remediation and verification ordering",
        ok,
        f"remediation={remediation}, verification={verification}, empty_evidence={empty_evidence}",
    )


def main() -> int:
    predictions = run_sample()
    print("=== sample dataset accuracy ===")
    compare_main([str(DATASET / "sample_output.csv"), str(predictions)])

    print("\n=== custom evaluation cases ===")
    checks: List[Tuple[str, bool, str]] = [
        custom_case_1_non_deletion_request(),
        custom_case_2_replica_drift(),
        custom_case_3_remediation_order(),
    ]
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    failed = [name for name, ok, _detail in checks if not ok]
    print(f"\ncustom cases: {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
