"""Engine regression tests.

The headline bug these lock down: an authorised deletion whose subject could not
be resolved used to be reported ``COMPLETE`` with no steps and no evidence - the
exact "the dashboard says done" failure the challenge is about.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_CODE_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from audit.models import (  # noqa: E402
    ABSENT,
    ANONYMIZED,
    PRESENT,
    DeletionRequest,
    ResourceState,
)
from audit.planner import CasePlanner, steps_to_json  # noqa: E402
from audit.policy import DeletionPolicy, authorizes_deletion  # noqa: E402
from audit.validator import validate_output  # noqa: E402
from evaluation import cases as e2e  # noqa: E402
from evaluation.fixtures import FixtureDataset  # noqa: E402

DELETE_TEXT = "Please delete my account and all personal data."


class UnresolvedIdentityTests(unittest.TestCase):
    def _fixture(self, root: Path) -> FixtureDataset:
        fixture = FixtureDataset(root=root)
        fixture.add_identity("user_1", name="Alice", email="alice@example.test")
        return fixture

    def test_authorized_deletion_with_resolvable_identity_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fixture.add_request("case_1", "alice@example.test")
            fixture.add_evidence(
                "evidence_1",
                name="profile.json",
                origin="account_service",
                captured_at="2026-08-01T00:00:00Z",
                content=json.dumps(
                    {
                        "records": [
                            {
                                "record_type": "USER_PROFILE",
                                "system": "account_service",
                                "resource_id": "account_1",
                                "user_id": "user_1",
                                "email": "alice@example.test",
                                "full_name": "Alice",
                                "result": "FOUND",
                                "recorded_at": "2026-08-01T00:00:00Z",
                            }
                        ]
                    }
                ),
            )
            result = fixture.audit("case_1")
            self.assertEqual(result.deletion_status, "INCOMPLETE")
            self.assertEqual(result.steps[0].operation, "REPORT_REMAINING_RECORD")
            self.assertEqual(result.steps[0].resource_id, "account_1")

    def test_authorized_deletion_with_unresolved_identity_is_never_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fixture.add_request("case_2", "ghost@nowhere.test")
            result = fixture.audit("case_2")
            self.assertNotEqual(result.deletion_status, "COMPLETE")
            self.assertEqual(result.deletion_status, "INCOMPLETE")
            self.assertEqual(result.steps, [])
            self.assertTrue(any("unresolved" in note for note in result.notes))

    def test_non_deletion_request_with_unresolved_identity_stays_not_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fixture.add_request(
                "case_3",
                "ghost@nowhere.test",
                request_text="Please send me a copy of all my personal data.",
            )
            result = fixture.audit("case_3")
            self.assertEqual(result.deletion_status, "NOT_APPLICABLE")
            self.assertEqual(result.steps, [])

    def test_unresolved_identity_row_serializes_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fixture.add_request("case_4", "ghost@nowhere.test")
            rows, report, output_path = fixture.run_pipeline()
            self.assertEqual(rows[0]["deletion_status"], "INCOMPLETE")
            self.assertEqual(json.loads(rows[0]["steps_json"]), [])
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.rows, 1)
            self.assertEqual(rows[0]["case_id"], "case_4")
            text = output_path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("case_id,deletion_status,steps_json\n"))


class PlannerTests(unittest.TestCase):
    def _request(self) -> DeletionRequest:
        from audit.identity import parse_ts

        when = parse_ts("2026-08-01T00:00:00Z")
        return DeletionRequest(
            case_id="case_p",
            email="subject@example.test",
            requested_at=when,
            reported_status="COMPLETE",
            reported_at=when,
            audit_at=when,
            request_text=DELETE_TEXT,
        )

    def _policy(self) -> DeletionPolicy:
        return DeletionPolicy.load(None)

    def test_complete_without_citable_evidence_is_downgraded(self):
        state = ResourceState(
            system_id="account_service",
            resource_id="account_p",
            category="USER_PROFILE",
            state=ABSENT,
            evidence_id="",
            observed_at=self._request().audit_at,
        )
        result = CasePlanner(self._policy()).build(
            self._request(), None, {("account_service", "account_p"): state}, self._policy()
        )
        self.assertEqual(result.deletion_status, "INCOMPLETE")
        self.assertEqual(result.steps, [])
        self.assertIn("no admissible evidence proves completion", result.notes)

    def test_complete_verification_cites_evidence(self):
        state = ResourceState(
            system_id="account_service",
            resource_id="account_p",
            category="USER_PROFILE",
            state=ABSENT,
            evidence_id="evidence_7",
            observed_at=self._request().audit_at,
        )
        result = CasePlanner(self._policy()).build(
            self._request(), None, {("account_service", "account_p"): state}, self._policy()
        )
        self.assertEqual(result.deletion_status, "COMPLETE")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].evidence_id, "evidence_7")
        self.assertEqual(result.steps[0].state, "ABSENT")

    def test_serialization_is_stable_and_key_order_free(self):
        state = ResourceState(
            system_id="account_service",
            resource_id="account_p",
            category="USER_PROFILE",
            state=ABSENT,
            evidence_id="evidence_7",
            observed_at=self._request().audit_at,
        )
        result = CasePlanner(self._policy()).build(
            self._request(), None, {("account_service", "account_p"): state}, self._policy()
        )
        first = steps_to_json(result.steps)
        second = steps_to_json(result.steps)
        self.assertEqual(first, second)
        payload = json.loads(first)
        self.assertEqual(
            sorted(payload[0]),
            sorted(
                [
                    "step_no",
                    "phase",
                    "system_id",
                    "resource_id",
                    "operation",
                    "state",
                    "evidence_id",
                ]
            ),
        )


class IdentityIntentTests(unittest.TestCase):
    def test_deletion_phrasings(self):
        for text in (
            DELETE_TEXT,
            "I want my account erased.",
            "Bitte löschen Sie mein Konto.",
            "Supprimez mon compte, s'il vous plaît.",
        ):
            with self.subTest(text=text):
                self.assertTrue(authorizes_deletion(text))

    def test_non_deletion_phrasings(self):
        for text in (
            "Please send me a copy of my data.",
            "I would like my address corrected.",
            "Stop the marketing emails but keep my account.",
            "What personal information do you hold about me?",
        ):
            with self.subTest(text=text):
                self.assertFalse(authorizes_deletion(text))


class EndToEndCaseTests(unittest.TestCase):
    """The synthetic fixtures in ``evaluation/cases.py`` must all hold."""

    def test_all_synthetic_cases_pass(self):
        for name, ok, detail in e2e.run_all():
            with self.subTest(case=name):
                self.assertTrue(ok, f"{name}: {detail}")


class ValidatorAcceptanceTests(unittest.TestCase):
    def test_generated_fixture_output_passes_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FixtureDataset(root=Path(tmp))
            fixture.add_identity("user_1", name="Alice", email="alice@example.test")
            fixture.add_request("case_ok", "alice@example.test")
            fixture.add_evidence(
                "evidence_ok",
                name="tombstone.json",
                origin="identity_service",
                captured_at="2026-08-01T00:00:00Z",
                content=json.dumps(
                    {
                        "records": [
                            {
                                "record_type": "ACCOUNT_TOMBSTONE",
                                "system": "identity_service",
                                "resource_id": "tombstone_1",
                                "user_id": "user_1",
                                "recorded_at": "2026-08-01T00:00:00Z",
                            }
                        ]
                    }
                ),
            )
            _rows, report, output_path = fixture.run_pipeline()
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.steps, 1)
            again = validate_output(
                output_path,
                fixture.root / "deletion_requests.csv",
                evidence_path=fixture.root / "evidence_files.csv",
                activity_path=fixture.root / "system_activity.csv",
                audit_path=fixture.root / "user_audit_log.csv",
            )
            self.assertTrue(again.ok, again.errors)


if __name__ == "__main__":
    unittest.main()
