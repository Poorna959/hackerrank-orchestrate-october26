"""End-to-end evaluation cases built on minimal synthetic datasets.

Every case here writes a complete temporary dataset with the production schema,
then runs the production engine, serialiser and validator over it.  Nothing is
read from or written to the repository's ``dataset/`` or ``output.csv``.

The cases target the behaviours that the public sample cannot demonstrate, and
each one is a regression test for a specific failure mode:

``E1`` unresolved identity, no evidence          -> must not claim COMPLETE
``E2`` unresolved identity, evidence by address  -> still auditable
``E3`` replica drift behind a clean primary      -> remaining record
``E4`` evidence captured after audit_at          -> not admissible proof
``E5`` historical identifier only                -> still the same person
``E6`` policy retention and tombstone            -> COMPLETE with proof
``E7`` workflow row naming another subject       -> not attributed
``E8`` data-catalog link row                     -> not proof of existence
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Tuple

from evaluation.fixtures import (
    FixtureDataset,
    sql_snapshot,
    record_set,
    step_tuples,
)

CaseOutcome = Tuple[str, bool, str]


def _fixture(root: Path) -> FixtureDataset:
    return FixtureDataset(root=root / "dataset")


def _detail(result, extra: str = "") -> str:
    steps = step_tuples(result)
    text = f"status={result.deletion_status}, steps={steps}"
    return f"{text}, {extra}" if extra else text


# ---------------------------------------------------------------------------
# E1 / E2 - unresolved identity
# ---------------------------------------------------------------------------


def case_unresolved_identity() -> CaseOutcome:
    """An authorised deletion with no resolvable subject is never COMPLETE."""

    name = "E1 unresolved identity cannot be reported COMPLETE"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity(
            "user_1", name="Alice Meyer", email="alice@example.test"
        )
        fixture.add_request("case_901", "ghost@nowhere.test")
        fixture.add_evidence(
            "evidence_01",
            name="inventory.sql",
            origin="records_assurance_export",
            captured_at="2026-08-01T00:00:00Z",
            content=sql_snapshot(
                captured_at="2026-08-01T00:00:00Z",
                header="customer_data_inventory",
                statements=[
                    "CREATE TABLE customer_profiles_primary (resource_id TEXT, user_id TEXT, "
                    "email TEXT, phone TEXT, email_hash TEXT, full_name TEXT, account_status TEXT);",
                    "INSERT INTO customer_profiles_primary VALUES('account_001','user_1',"
                    "'alice@example.test','','','Alice Meyer','active');",
                ],
            ),
        )
        result = fixture.audit("case_901")
        ok = (
            result.deletion_status == "INCOMPLETE"
            and result.steps == []
            and any("unresolved" in note for note in result.notes)
        )
        return name, ok, _detail(result)


def case_unresolved_identity_with_evidence() -> CaseOutcome:
    """Evidence carrying the request address is still attributable to it."""

    name = "E2 unresolved subject with address-linked evidence"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity("user_1", name="Alice Meyer", email="alice@example.test")
        fixture.add_request("case_902", "ghost@nowhere.test")
        fixture.add_evidence(
            "evidence_02",
            name="support_records.json",
            origin="support_platform",
            captured_at="2026-08-02T00:00:00Z",
            content=record_set(
                [
                    {
                        "record_type": "SUPPORT_MESSAGE",
                        "system": "support_platform",
                        "resource_id": "support_501",
                        "requester_email": "ghost@nowhere.test",
                        "message_excerpt": "please delete my data",
                        "result": "FOUND",
                        "recorded_at": "2026-08-01T12:00:00Z",
                    }
                ]
            ),
        )
        rows, report, output_path = fixture.run_pipeline()
        row = rows[0]
        result_steps = step_tuples(fixture.audit("case_902"))
        finding = [step for step in result_steps if step[0] == "FINDING"]
        ok = (
            row["deletion_status"] == "INCOMPLETE"
            and report.ok
            and finding
            and finding[0][1] == "support_platform"
            and finding[0][2] == "support_501"
            and finding[0][3] == "REPORT_EXPOSED_PII"
            and finding[0][5] == "evidence_02"
            and output_path.exists()
        )
        return name, ok, f"steps={result_steps}, validator_ok={report.ok}"


# ---------------------------------------------------------------------------
# E3 - replica drift
# ---------------------------------------------------------------------------


def case_replica_drift() -> CaseOutcome:
    """A profile that survives only on the failover replica is still present."""

    name = "E3 replica drift behind a clean primary"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity(
            "user_7", name="Bob Stone", email="bob@example.test"
        )
        fixture.add_request("case_903", "bob@example.test")
        fixture.add_evidence(
            "evidence_03",
            name="customer_accounts_failover_replica.sql",
            origin="account_service",
            captured_at="2026-08-03T00:00:00Z",
            content=sql_snapshot(
                captured_at="2026-08-03T00:00:00Z",
                header="account topology: primary + failover replica",
                statements=[
                    "CREATE TABLE customer_profiles_primary (resource_id TEXT, user_id TEXT, "
                    "email TEXT, phone TEXT, email_hash TEXT, full_name TEXT, account_status TEXT);",
                    "CREATE TABLE customer_profiles_failover_replica (resource_id TEXT, user_id TEXT, "
                    "email TEXT, phone TEXT, email_hash TEXT, full_name TEXT, account_status TEXT);",
                    "INSERT INTO customer_profiles_primary VALUES('account_700','user_8',"
                    "'carol@example.test','','','Carol Diaz','active');",
                    "INSERT INTO customer_profiles_failover_replica VALUES('account_701','user_7',"
                    "'bob@example.test','','','Bob Stone','deleted');",
                ],
            ),
        )
        result = fixture.audit("case_903")
        steps = step_tuples(result)
        findings = [step for step in steps if step[0] == "FINDING"]
        remediation = [step for step in steps if step[0] == "REMEDIATION"]
        ok = (
            result.deletion_status == "INCOMPLETE"
            and findings
            and findings[0][1] == "account_service"
            and findings[0][2] == "account_701"
            and findings[0][3] == "REPORT_REMAINING_RECORD"
            and findings[0][5] == "evidence_03"
            and remediation
            and remediation[0][3] == "DELETE_RECORD"
            and all(step[5] == "" for step in remediation)
        )
        return name, ok, _detail(result)


# ---------------------------------------------------------------------------
# E4 - temporal cut-off
# ---------------------------------------------------------------------------


def case_post_cutoff_evidence() -> CaseOutcome:
    """Evidence captured after ``audit_at`` cannot prove the earlier state."""

    name = "E4 post-cutoff evidence is not admissible"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity(
            "user_4", name="Dana Whit", email="dana@example.test"
        )
        fixture.add_request(
            "case_904",
            "dana@example.test",
            audit_at="2026-08-10T00:00:00Z",
        )
        fixture.add_evidence(
            "evidence_40",
            name="support_before.json",
            origin="support_platform",
            captured_at="2026-08-01T00:00:00Z",
            content=record_set(
                [
                    {
                        "record_type": "SUPPORT_MESSAGE",
                        "system": "support_platform",
                        "resource_id": "support_801",
                        "requester_email": "dana@example.test",
                        "message_excerpt": "my order never arrived",
                        "result": "FOUND",
                        "recorded_at": "2026-07-20T00:00:00Z",
                    }
                ]
            ),
        )
        fixture.add_evidence(
            "evidence_41",
            name="support_after.json",
            origin="support_platform",
            captured_at="2026-08-20T00:00:00Z",
            content=record_set(
                [
                    {
                        "record_type": "SUPPORT_MESSAGE",
                        "system": "support_platform",
                        "resource_id": "support_801",
                        "requester_email": "dana@example.test",
                        "result": "REDACTED",
                        "recorded_at": "2026-08-20T00:00:00Z",
                    }
                ]
            ),
        )
        result = fixture.audit("case_904")
        steps = step_tuples(result)
        cited = {step[5] for step in steps if step[5]}
        findings = [step for step in steps if step[0] == "FINDING"]
        ok = (
            result.deletion_status == "INCOMPLETE"
            and findings
            and findings[0][3] == "REPORT_EXPOSED_PII"
            and findings[0][5] == "evidence_40"
            and "evidence_41" not in cited
        )
        return name, ok, f"{_detail(result)} cited={sorted(cited)}"


# ---------------------------------------------------------------------------
# E5 - historical identifier
# ---------------------------------------------------------------------------


def case_historical_identifier() -> CaseOutcome:
    """A record that only holds an old email still belongs to the subject."""

    name = "E5 historical identifier still links the record"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity(
            "user_9", name="Elena Sokolova", email="new@example.test"
        )
        fixture.add_audit_event(
            "audit_901", "user_9", "email", "old@example.test", "new@example.test"
        )
        fixture.add_request("case_905", "new@example.test")
        fixture.add_evidence(
            "evidence_50",
            name="search_documents.json",
            origin="search_service",
            captured_at="2026-08-02T00:00:00Z",
            content=record_set(
                [
                    {
                        "record_type": "SEARCH_INDEX_ENTRY",
                        "system": "search_service",
                        "resource_id": "search_901",
                        "email": "old@example.test",
                        "display_name": "Elena Sokolova",
                        "result": "FOUND",
                        "recorded_at": "2026-08-01T00:00:00Z",
                    }
                ]
            ),
        )
        result = fixture.audit("case_905")
        steps = step_tuples(result)
        findings = [step for step in steps if step[0] == "FINDING"]
        ok = (
            result.deletion_status == "INCOMPLETE"
            and findings
            and findings[0][1] == "search_service"
            and findings[0][2] == "search_901"
            and findings[0][3] == "REPORT_REMAINING_RECORD"
            and findings[0][5] == "evidence_50"
        )
        return name, ok, _detail(result)


# ---------------------------------------------------------------------------
# E6 - retention policy
# ---------------------------------------------------------------------------


def case_retention_and_tombstone() -> CaseOutcome:
    """A retained audit record and a compliant tombstone compose a COMPLETE case."""

    name = "E6 retention and tombstone produce a proved COMPLETE"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity(
            "user_11", name="Farid Haddad", email="farid@example.test"
        )
        fixture.add_request("case_906", "farid@example.test")
        fixture.add_evidence(
            "evidence_60",
            name="identity_records.json",
            origin="identity_service",
            captured_at="2026-08-02T00:00:00Z",
            content=record_set(
                [
                    {
                        "record_type": "ACCOUNT_TOMBSTONE",
                        "system": "identity_service",
                        "resource_id": "tombstone_11",
                        "user_id": "user_11",
                        "result": "REDACTED",
                        "recorded_at": "2026-08-01T00:00:00Z",
                    },
                    {
                        "record_type": "SECURITY_AUDIT_LOG",
                        "system": "security_service",
                        "resource_id": "audit_record_11",
                        "subject_reference": "user_11",
                        "result": "FOUND",
                        "recorded_at": "2026-08-01T00:00:00Z",
                    },
                ]
            ),
        )
        rows, report, _output_path = fixture.run_pipeline()
        steps = step_tuples(fixture.audit("case_906"))
        operations = {step[3] for step in steps}
        ok = (
            rows[0]["deletion_status"] == "COMPLETE"
            and report.ok
            and steps
            and all(step[0] == "VERIFICATION" for step in steps)
            and all(step[5] == "evidence_60" for step in steps)
            and "VERIFY_RECORD_REDACTED" in operations
            and "VERIFY_RECORD_PRESENT" in operations
        )
        return name, ok, f"steps={steps}, validator_ok={report.ok}"


# ---------------------------------------------------------------------------
# E7 - attribution safety
# ---------------------------------------------------------------------------


def case_ambiguous_ownership() -> CaseOutcome:
    """A workflow row that names another subject's account is not attributed."""

    name = "E7 workflow row of another subject is not attributed"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity(
            "user_12", name="Gina Rossi", email="gina@example.test"
        )
        fixture.add_identity("user_13", name="Hugo Lang", email="hugo@example.test")
        fixture.add_request(
            "case_907",
            "gina@example.test",
            reported_at="2026-08-05T00:00:00Z",
        )
        fixture.add_activity(
            "activity_901",
            "2026-08-05T00:10:00Z",
            "DELETE_RECORD",
            resource_id="account_951",
        )
        fixture.add_evidence(
            "evidence_70",
            name="accounts.json",
            origin="account_service",
            captured_at="2026-08-04T00:00:00Z",
            content=record_set(
                [
                    {
                        "record_type": "USER_PROFILE",
                        "system": "account_service",
                        "resource_id": "account_951",
                        "user_id": "user_13",
                        "email": "hugo@example.test",
                        "full_name": "Hugo Lang",
                        "result": "FOUND",
                        "recorded_at": "2026-08-04T00:00:00Z",
                    },
                    {
                        "record_type": "SEARCH_INDEX_ENTRY",
                        "system": "search_service",
                        "resource_id": "search_951",
                        "email": "gina@example.test",
                        "result": "FOUND",
                        "recorded_at": "2026-08-04T00:00:00Z",
                    },
                ]
            ),
        )
        result = fixture.audit("case_907")
        steps = step_tuples(result)
        resources = {step[2] for step in steps}
        ok = (
            result.deletion_status == "INCOMPLETE"
            and "search_951" in resources
            and "account_951" not in resources
        )
        return name, ok, f"resources={sorted(resources)}"


# ---------------------------------------------------------------------------
# E8 - link rows
# ---------------------------------------------------------------------------


def case_link_rows_are_not_evidence() -> CaseOutcome:
    """A catalog link states ownership, never that the record still exists."""

    name = "E8 catalog link row is not proof of existence"
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _fixture(Path(tmp))
        fixture.add_identity(
            "user_14", name="Ines Costa", email="ines@example.test"
        )
        fixture.add_request("case_908", "ines@example.test")
        fixture.add_activity(
            "activity_910",
            "2026-08-04T00:00:00Z",
            "LINK_RESOURCE_IDENTITY",
            source_system="data_catalog",
            target_system="support_platform",
            resource_id="object_951",
            details="Linked object_951 to user_14.",
        )
        fixture.add_evidence(
            "evidence_80",
            name="search_documents.json",
            origin="search_service",
            captured_at="2026-08-02T00:00:00Z",
            content=record_set(
                [
                    {
                        "record_type": "SEARCH_INDEX_ENTRY",
                        "system": "search_service",
                        "resource_id": "search_971",
                        "email": "ines@example.test",
                        "result": "FOUND",
                        "recorded_at": "2026-08-01T00:00:00Z",
                    }
                ]
            ),
        )
        result = fixture.audit("case_908")
        steps = step_tuples(result)
        resources = {step[2] for step in steps}
        ok = (
            result.deletion_status == "INCOMPLETE"
            and "search_971" in resources
            and "object_951" not in resources
        )
        return name, ok, f"resources={sorted(resources)}"


ALL_CASES = (
    case_unresolved_identity,
    case_unresolved_identity_with_evidence,
    case_replica_drift,
    case_post_cutoff_evidence,
    case_historical_identifier,
    case_retention_and_tombstone,
    case_ambiguous_ownership,
    case_link_rows_are_not_evidence,
)


def run_all() -> List[CaseOutcome]:
    return [case() for case in ALL_CASES]
