"""Validator tests: good output passes, every contract violation fails.

Each test writes a real CSV to a temporary directory and re-reads it through the
validator, so the checks are exercised exactly as a run uses them.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Sequence

_CODE_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from audit.validator import validate_output  # noqa: E402


def step(
    step_no: int,
    phase: str,
    system: str,
    resource: str,
    operation: str,
    state: str,
    evidence: str = "",
) -> dict:
    return {
        "step_no": step_no,
        "phase": phase,
        "system_id": system,
        "resource_id": resource,
        "operation": operation,
        "state": state,
        "evidence_id": evidence,
    }


class ValidatorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write(
            "deletion_requests.csv",
            "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n"
            "case_001,a@example.test,2026-07-01T00:00:00Z,COMPLETE,"
            "2026-07-02T00:00:00Z,2026-07-10T00:00:00Z,delete it\n"
            "case_002,b@example.test,2026-07-01T00:00:00Z,COMPLETE,"
            "2026-07-02T00:00:00Z,2026-07-10T00:00:00Z,delete it\n",
        )
        self.write(
            "evidence_files.csv",
            "file_id,file_path,origin,captured_at\n"
            "evidence_1,evidence/a.sql,account_service,2026-07-05T00:00:00Z\n"
            "evidence_2,evidence/b.sql,account_service,2026-07-20T00:00:00Z\n",
        )
        self.write(
            "system_activity.csv",
            "activity_id,event_time,event_type,source_system,target_system,"
            "resource_id,reported_result,details\n"
            "activity_1,2026-07-06T00:00:00Z,DELETE_RECORD,deletion_worker,"
            "account_service,account_001,SUCCESS,x\n",
        )
        self.write(
            "user_audit_log.csv",
            "audit_id,user_id,event_time,field_name,old_value,new_value\n"
            "audit_1,user_1,2026-07-03T00:00:00Z,email,old@example.test,"
            "a@example.test\n",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_output(self, rows: Sequence[Sequence[object]]) -> Path:
        path = self.root / "output.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["case_id", "deletion_status", "steps_json"])
            for row in rows:
                writer.writerow(list(row))
        return path

    def validate(self, rows: Sequence[Sequence[object]]):
        path = self.write_output(rows)
        return validate_output(
            path,
            self.root / "deletion_requests.csv",
            evidence_path=self.root / "evidence_files.csv",
            activity_path=self.root / "system_activity.csv",
            audit_path=self.root / "user_audit_log.csv",
        )

    @staticmethod
    def complete_row(case_id: str = "case_001", evidence: str = "evidence_1") -> List[object]:
        return [
            case_id,
            "COMPLETE",
            json.dumps(
                [
                    step(
                        1,
                        "VERIFICATION",
                        "account_service",
                        "account_001",
                        "VERIFY_RECORD_ABSENT",
                        "ABSENT",
                        evidence,
                    )
                ]
            ),
        ]

    @staticmethod
    def incomplete_row(case_id: str = "case_002") -> List[object]:
        return [
            case_id,
            "INCOMPLETE",
            json.dumps(
                [
                    step(
                        1,
                        "FINDING",
                        "search_service",
                        "search_1",
                        "REPORT_REMAINING_RECORD",
                        "PRESENT",
                        "evidence_1",
                    ),
                    step(
                        2,
                        "REMEDIATION",
                        "search_service",
                        "search_1",
                        "DELETE_RECORD",
                        "ABSENT",
                        "",
                    ),
                    step(
                        3,
                        "VERIFICATION",
                        "search_service",
                        "search_1",
                        "VERIFY_RECORD_ABSENT",
                        "ABSENT",
                        "",
                    ),
                ]
            ),
        ]


class PositiveTests(ValidatorTestBase):
    def test_valid_file_passes(self):
        report = self.validate([self.complete_row(), self.incomplete_row()])
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.rows, 2)
        self.assertEqual(report.steps, 4)
        self.assertEqual(report.warnings, [])

    def test_not_applicable_empty_array_passes(self):
        report = self.validate(
            [self.complete_row(), ["case_002", "NOT_APPLICABLE", "[]"]]
        )
        self.assertTrue(report.ok, report.errors)

    def test_incomplete_without_findings_is_a_warning_not_a_pass_certificate(self):
        report = self.validate(
            [self.complete_row(), ["case_002", "INCOMPLETE", "[]"]]
        )
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("no citable findings", report.warnings[0])


class StructuralFailureTests(ValidatorTestBase):
    def test_wrong_header_fails(self):
        path = self.write("output.csv", "case,status,steps\ncase_001,COMPLETE,[]\n")
        report = validate_output(path, self.root / "deletion_requests.csv")
        self.assertFalse(report.ok)
        self.assertTrue(any("header" in e for e in report.structural))

    def test_missing_row_fails(self):
        report = self.validate([self.complete_row()])
        self.assertFalse(report.ok)
        self.assertTrue(any("expected 2 data rows" in e for e in report.structural))

    def test_duplicate_and_unexpected_case_fails(self):
        report = self.validate(
            [self.complete_row(), self.complete_row(), ["case_999", "COMPLETE", "[]"]]
        )
        self.assertFalse(report.ok)
        messages = " ".join(report.structural)
        self.assertIn("duplicate case_id", messages)
        self.assertIn("unexpected case_id", messages)

    def test_unsorted_rows_fail(self):
        report = self.validate([self.complete_row("case_002"), self.complete_row()])
        self.assertFalse(report.ok)
        self.assertTrue(any("not sorted" in e for e in report.structural))

    def test_invalid_status_fails(self):
        report = self.validate([["case_001", "DONE", "[]"], self.incomplete_row()])
        self.assertFalse(report.ok)
        self.assertTrue(any("invalid deletion_status" in e for e in report.structural))

    def test_invalid_json_fails(self):
        report = self.validate([["case_001", "COMPLETE", "not json"], self.incomplete_row()])
        self.assertFalse(report.ok)
        self.assertTrue(any("not valid JSON" in e for e in report.structural))

    def test_non_array_json_fails(self):
        report = self.validate([["case_001", "COMPLETE", "{}"], self.incomplete_row()])
        self.assertFalse(report.ok)
        self.assertTrue(any("JSON array" in e for e in report.structural))

    def test_not_applicable_with_steps_fails(self):
        report = self.validate(
            [
                [
                    "case_001",
                    "NOT_APPLICABLE",
                    json.dumps([step(1, "VERIFICATION", "a", "b", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1")]),
                ],
                self.incomplete_row(),
            ]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("empty steps array" in e for e in report.structural))

    def test_non_consecutive_step_numbers_fail(self):
        payload = json.loads(self.complete_row()[2])
        payload[0]["step_no"] = 5
        report = self.validate(
            [["case_001", "COMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("consecutive" in e for e in report.structural))

    def test_wrong_operation_for_phase_fails(self):
        payload = [
            step(1, "FINDING", "account_service", "account_001", "DELETE_RECORD", "ABSENT", "evidence_1")
        ]
        report = self.validate(
            [["case_001", "COMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("not allowed in" in e for e in report.structural))

    def test_wrong_state_for_operation_fails(self):
        payload = [
            step(1, "VERIFICATION", "account_service", "account_001", "VERIFY_RECORD_ABSENT", "PRESENT", "evidence_1")
        ]
        report = self.validate(
            [["case_001", "COMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("requires state" in e for e in report.structural))

    def test_extra_step_field_fails(self):
        payload = json.loads(self.complete_row()[2])
        payload[0]["note"] = "extra"
        report = self.validate(
            [["case_001", "COMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("unexpected fields" in e for e in report.structural))

    def test_phase_order_break_fails(self):
        payload = [
            step(1, "VERIFICATION", "account_service", "account_001", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
            step(2, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"),
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("ordering" in e for e in report.structural))


class SemanticFailureTests(ValidatorTestBase):
    def test_complete_without_verification_fails(self):
        report = self.validate([["case_001", "COMPLETE", "[]"], self.incomplete_row()])
        self.assertFalse(report.ok)
        self.assertTrue(
            any("COMPLETE requires evidence-backed" in e for e in report.semantic)
        )

    def test_complete_verification_without_evidence_fails(self):
        payload = [
            step(1, "VERIFICATION", "account_service", "account_001", "VERIFY_RECORD_ABSENT", "ABSENT", "")
        ]
        report = self.validate(
            [["case_001", "COMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("needs evidence" in e for e in report.semantic))

    def test_finding_without_evidence_fails(self):
        payload = [
            step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "")
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("requires an evidence reference" in e for e in report.semantic))

    def test_unknown_evidence_id_fails(self):
        payload = [
            step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_404")
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("unknown evidence id" in e for e in report.semantic))

    def test_evidence_after_audit_at_fails(self):
        payload = [
            step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_2")
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("after audit_at" in e for e in report.semantic))

    def test_activity_citation_after_audit_at_fails(self):
        payload = [
            step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "activity_1")
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        # activity_1 predates audit_at, so this citation is legitimate.
        self.assertTrue(report.ok, report.errors)

        self.write(
            "system_activity.csv",
            "activity_id,event_time,event_type,source_system,target_system,"
            "resource_id,reported_result,details\n"
            "activity_2,2026-07-25T00:00:00Z,DELETE_RECORD,deletion_worker,"
            "account_service,account_001,SUCCESS,later\n",
        )
        payload[0]["evidence_id"] = "activity_2"
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("after audit_at" in e for e in report.semantic))

    def test_remediation_with_evidence_fails(self):
        payload = [
            step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"),
            step(2, "REMEDIATION", "search_service", "search_1", "DELETE_RECORD", "ABSENT", "evidence_1"),
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("remediation must use empty" in e for e in report.semantic))

    def test_future_verification_with_evidence_fails(self):
        payload = [
            step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"),
            step(2, "VERIFICATION", "search_service", "search_1", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1"),
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("future verification" in e for e in report.semantic))

    def test_duplicate_step_fails(self):
        payload = [
            step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"),
            step(2, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"),
        ]
        report = self.validate(
            [["case_001", "INCOMPLETE", json.dumps(payload)], self.incomplete_row()]
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("duplicates an earlier step" in e for e in report.semantic))


if __name__ == "__main__":
    unittest.main()
