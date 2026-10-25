"""Parser regression tests.

These cover the evidence-reading bugs that silently produced wrong audit states:
INSERT rows losing their values, replica tables being mistaken for the primary,
empty captures being treated as proof of absence, and undated records crashing
the cut-off comparison.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_CODE_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from audit.evidence.json_source import (  # noqa: E402
    parse_record_set,
    parse_service_export,
    parse_snapshot_bundle,
)
from audit.evidence.loader import EvidenceLoader  # noqa: E402
from audit.evidence.log_source import LogFile  # noqa: E402
from audit.evidence.pdf_source import extract_text, records_from_text  # noqa: E402
from audit.evidence.sql_source import SqlSnapshot, parse_inserts  # noqa: E402
from audit.identity import IdentityIndex, load_identities  # noqa: E402
from audit.models import ABSENT, ANONYMIZED, PRESENT, REDACTED  # noqa: E402

REPO_ROOT = _CODE_DIR.parent


def _snapshot(statements) -> SqlSnapshot:
    return SqlSnapshot(
        "-- Captured at: 2026-08-01T00:00:00Z\n" + "\n".join(statements) + "\n"
    )


class SqlSnapshotTests(unittest.TestCase):
    def test_insert_without_column_list_uses_declared_order(self):
        sql = (
            "CREATE TABLE customer_profiles_primary (resource_id TEXT, user_id TEXT, "
            "email TEXT);\n"
            "INSERT INTO customer_profiles_primary VALUES('account_001','user_1',"
            "'a@example.test');\n"
        )
        rows = parse_inserts(sql, SqlSnapshot(sql).tables)
        self.assertEqual(len(rows), 1)
        table, row = rows[0]
        self.assertEqual(table, "customer_profiles_primary")
        self.assertEqual(row["resource_id"], "account_001")
        self.assertEqual(row["user_id"], "user_1")
        self.assertEqual(row["email"], "a@example.test")

    def test_quoted_values_and_nulls_round_trip(self):
        snapshot = _snapshot(
            [
                "CREATE TABLE support_records (resource_id TEXT, requester_email TEXT, "
                "requester_phone TEXT, requester_name TEXT, customer_reference TEXT, "
                "message_excerpt TEXT);",
                "INSERT INTO support_records VALUES('support_1','o''brien@example.test',"
                "NULL,'O''Brien','user_1','it''s, fine');",
            ]
        )
        records = list(snapshot.iter_records())
        self.assertEqual(len(records), 1)
        _system, resource, category, state, fields = records[0]
        self.assertEqual(resource, "support_1")
        self.assertEqual(category, "SUPPORT_MESSAGE")
        self.assertEqual(state, PRESENT)
        self.assertEqual(fields["requester_email"], "o'brien@example.test")
        self.assertIsNone(fields["requester_phone"])
        self.assertEqual(fields["message_excerpt"], "it's, fine")

    def test_primary_table_defines_absence_scope_but_replica_does_not(self):
        snapshot = _snapshot(
            [
                "CREATE TABLE customer_profiles_primary (resource_id TEXT);",
                "CREATE TABLE customer_profiles_failover_replica (resource_id TEXT);",
                "INSERT INTO customer_profiles_primary VALUES('account_001');",
                "INSERT INTO customer_profiles_failover_replica VALUES('account_002');",
            ]
        )
        self.assertIn("account_service", snapshot.absent_scope)
        self.assertNotIn("account_failover_replica", snapshot.absent_scope)

    def test_empty_capture_is_not_absence_scope(self):
        snapshot = _snapshot(
            ["CREATE TABLE customer_profiles_primary (resource_id TEXT);"]
        )
        self.assertEqual(snapshot.absent_scope, set())

    def test_snapshot_scope_rows_declare_every_system(self):
        snapshot = _snapshot(
            [
                "CREATE TABLE snapshot_scope (origin_system TEXT);",
                "CREATE TABLE customer_data_primary (origin_system TEXT, resource_id TEXT);",
                "INSERT INTO snapshot_scope VALUES('account_service');",
                "INSERT INTO snapshot_scope VALUES('search_service');",
            ]
        )
        self.assertEqual(
            snapshot.absent_scope, {"account_service", "search_service"}
        )

    def test_collection_topologies_stay_separate(self):
        record = '{"record_id": "1", "resource_id": "analytics_9", "email": "a@example.test"}'
        snapshot = _snapshot(
            [
                "CREATE TABLE customer_data_primary (collection_name TEXT, origin_system TEXT, "
                "record_json TEXT);",
                "CREATE TABLE customer_data_reporting_replica (collection_name TEXT, "
                "origin_system TEXT, record_json TEXT);",
                f"INSERT INTO customer_data_primary VALUES"
                f"('customer_analytics_reports','analytics_platform','{record}');",
                f"INSERT INTO customer_data_reporting_replica VALUES"
                f"('customer_analytics_reports','analytics_platform','{record}');",
            ]
        )
        systems = {system for system, *_rest in snapshot.iter_records()}
        self.assertEqual(
            systems,
            {"analytics_platform", "analytics_platform@reporting_replica"},
        )


class JsonReaderTests(unittest.TestCase):
    def test_record_set_maps_results_and_categories(self):
        records = parse_record_set(
            {
                "records": [
                    {
                        "record_type": "ACCOUNT_TOMBSTONE",
                        "system": "identity_service",
                        "resource_id": "tombstone_1",
                        "user_id": "user_1",
                        "result": "PRESENT",
                    },
                    {
                        "record_type": "ANALYTICS_PROFILE",
                        "system": "analytics_platform",
                        "resource_id": "analytics_1",
                        "result": "ANONYMIZED",
                    },
                ]
            }
        )
        self.assertEqual(records[0].category, "ACCOUNT_TOMBSTONE")
        self.assertEqual(records[0].state, REDACTED)  # no direct identifier left
        self.assertEqual(records[1].state, ANONYMIZED)

    def test_service_export_profile_lookup_404_is_absence(self):
        records = parse_service_export(
            {
                "service_exports": {
                    "account_service": {
                        "profile_lookups": [
                            {
                                "resource_id": "account_5",
                                "status_code": 404,
                                "observed_at": "2026-08-01T00:00:00Z",
                            },
                            {
                                "resource_id": "account_6",
                                "status_code": 200,
                                "profile": {"email": "b@example.test"},
                                "observed_at": "2026-08-01T00:00:00Z",
                            },
                        ]
                    }
                }
            }
        )
        self.assertEqual(records[0].state, ABSENT)
        self.assertEqual(records[1].state, PRESENT)
        self.assertEqual(records[1].observed_at, "2026-08-01T00:00:00Z")

    def test_snapshot_bundle_reads_capture_time(self):
        records = parse_snapshot_bundle(
            {
                "exported_at": "2026-08-02T00:00:00Z",
                "snapshots": [
                    {
                        "data_store": "support_platform",
                        "collection": "support_records",
                        "captured_at": "2026-08-01T00:00:00Z",
                        "records": [
                            {
                                "resource_id": "support_2",
                                "requester_email": "",
                                "requester_name": "",
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, REDACTED)
        self.assertEqual(records[0].observed_at, "2026-08-01T00:00:00Z")


class LogReaderTests(unittest.TestCase):
    def test_only_terminal_events_become_records(self):
        text = (
            "2026-08-01T00:00:00Z | INFO | account_service | tr-1 | "
            "profile_lookup_started | lookup_key=b@example.test\n"
            "2026-08-01T00:00:02Z | INFO | account_service | tr-1 | "
            "profile_lookup_completed | resource_id=account_6 | rows=0\n"
        )
        records = LogFile(text).records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].resource_id, "account_6")
        self.assertEqual(records[0].state, ABSENT)

    def test_trace_key_is_inherited_from_the_started_line(self):
        text = (
            "2026-08-01T00:00:00Z | INFO | account_service | tr-9 | "
            "profile_lookup_started | lookup_key=hist@example.test\n"
            "2026-08-01T00:00:02Z | INFO | account_service | tr-9 | "
            "profile_lookup_completed | resource_id=account_9 | rows=1\n"
        )
        loader = EvidenceLoader(
            IdentityIndex({}), REPO_ROOT / "dataset"
        )
        record = LogFile(text).records()[0]
        _owners, keys, _identifiers = loader._resolve(record.fields, record.resource_id)
        self.assertIn("email:hist@example.test", keys)


class PdfReaderTests(unittest.TestCase):
    def test_supplied_documents_are_readable(self):
        documents = sorted((REPO_ROOT / "dataset" / "evidence" / "documents").glob("*.pdf"))
        if not documents:
            self.skipTest("no PDF evidence in this checkout")
        text = extract_text(documents[0])
        self.assertTrue(text.strip(), f"no text extracted from {documents[0].name}")
        self.assertIn("NORTHSTAR", text.upper())

    def test_document_records_cite_resource_ids(self):
        records = records_from_text(
            "Retention register\naccount_1234 retained for audit\n", "security_service"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].resource_id, "account_1234")


class TimestampTests(unittest.TestCase):
    def test_offsets_are_normalised_to_utc(self):
        from audit.evidence.textparse import parse_line
        from audit.identity import parse_ts, parse_ts_optional

        utc = parse_ts("2026-08-01T00:00:00Z")
        self.assertEqual(parse_ts("2026-08-01T02:00:00+02:00"), utc)
        self.assertEqual(parse_ts("2026-07-31T19:00:00-05:00"), utc)
        self.assertEqual(parse_ts("2026-08-01 00:00:00"), utc)
        self.assertEqual(parse_ts("2026-08-01T00:00:00.000Z"), utc)
        self.assertIsNone(parse_ts_optional("2026-08-01"))
        self.assertIsNone(parse_ts_optional(""))
        # A log line carrying an offset must compare correctly against a UTC cut-off.
        line = parse_line("2026-08-01T02:00:00+02:00 | INFO | account_service | tr-1 | "
                          "profile_lookup_completed | resource_id=account_1 | rows=1")
        self.assertEqual(parse_ts(line["_timestamp"]), utc)


class LoaderTests(unittest.TestCase):
    def test_undated_evidence_does_not_crash_and_stays_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "evidence").mkdir()
            (root / "evidence" / "support.json").write_text(
                '{"records":[{"record_type":"SUPPORT_MESSAGE","system":'
                '"support_platform","resource_id":"support_3","requester_email":'
                '"c@example.test","result":"FOUND"}]}',
                encoding="utf-8",
            )
            (root / "evidence_files.csv").write_text(
                "file_id,file_path,origin,captured_at\n"
                "evidence_1,evidence/support.json,support_platform,\n",
                encoding="utf-8",
            )
            corpus = EvidenceLoader(IdentityIndex({}), root).load(
                root / "evidence_files.csv"
            )
            self.assertEqual(len(corpus.observations), 1)
            observation = corpus.observations[0]
            self.assertIsNotNone(observation.observed_at)
            self.assertLess(observation.observed_at.year, 2000)


if __name__ == "__main__":
    unittest.main()
