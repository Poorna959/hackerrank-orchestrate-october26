"""Reader for SQLite-compatible SQL snapshots.

Snapshots come in two shapes:

* ``customer_data_inventory_*.sql`` - one table per system collection.
* ``customer_data_<topology>_*.sql`` / ``customer_search_primary_*.sql`` /
  ``customer_accounts_failover_replica_*.sql`` - a primary table plus replica
  table(s).  Replica drift is the core "ghost data" signal: a record removed
  from the primary but still present on a replica has not actually been deleted.

Absence inside a relational snapshot is meaningful.  A snapshot is a complete
capture of the tables it declares, so a resource that belongs to a captured
table but does not appear in *any* table of that system is genuinely absent at
the capture time.  ``SqlSnapshot.absent_scope`` records which systems that
reasoning may be applied to.
"""

from __future__ import annotations

import json
import re

from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from ..models import (
    ABSENT,
    ANONYMIZED,
    BLOCKED,
    DISABLED,
    PRESENT,
    REDACTED,
    RESTORE_ALLOWED,
    CATEGORY_SYSTEM_MAP,
    SYSTEM_CATEGORY_MAP,
)

_PRESENT_STATES = (PRESENT, REDACTED, ANONYMIZED, DISABLED, BLOCKED, RESTORE_ALLOWED)

_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z0-9_]+)\"?\s*\((.*?)\);",
    re.S | re.I,
)
_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+\"?([A-Za-z0-9_]+)\"?\s*(?:\(([^)]*)\))?\s*VALUES\s*(\(.*?\));",
    re.S | re.I,
)

#: table name -> (system_id, data_category).
#:
#: Replica tables are kept under a distinct internal system id.  A copy on a
#: replica is a separate physical location from the primary: the primary may
#: have processed the deletion while the replica still serves the record, and a
#: later capture of the primary says nothing about what the replica holds.  The
#: planner collapses these ids back to the public system id.
TABLE_MAP: Dict[str, Tuple[str, str]] = {
    "customer_profiles": ("account_service", "USER_PROFILE"),
    "customer_profiles_primary": ("account_service", "USER_PROFILE"),
    "customer_profiles_failover_replica": ("account_failover_replica", "USER_PROFILE"),
    "customer_profiles_read_replica": ("account_replication_cluster", "USER_PROFILE"),
    "customer_search_documents": ("search_service", "SEARCH_INDEX_ENTRY"),
    "search_documents_primary": ("search_service", "SEARCH_INDEX_ENTRY"),
    "search_documents_replica": ("search_replicated_index", "SEARCH_INDEX_ENTRY"),
    "customer_analytics_profiles": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support_records": ("support_platform", "SUPPORT_MESSAGE"),
    "customer_exports": ("export_service", "USER_EXPORT"),
    "connector_subscriptions": ("integration_service", "DATA_FLOW"),
    "backup_restore_entries": ("backup_service", "BACKUP_ARCHIVE"),
    "security_audit_records": ("security_service", "SECURITY_AUDIT_LOG"),
}

#: collection_name inside customer_data_* tables -> (system, category)
COLLECTION_MAP: Dict[str, Tuple[str, str]] = {
    "customer_analytics_reports": ("analytics_platform", "ANALYTICS_PROFILE"),
    "customer_search_documents": ("search_service", "SEARCH_INDEX_ENTRY"),
    "security_audit_events": ("security_service", "SECURITY_AUDIT_LOG"),
    "backup_catalog": ("backup_service", "BACKUP_ARCHIVE"),
    "connector_configurations": ("integration_service", "DATA_FLOW"),
    "customer_profiles": ("account_service", "USER_PROFILE"),
    "support_records": ("support_platform", "SUPPORT_MESSAGE"),
    "customer_exports": ("export_service", "USER_EXPORT"),
    "customer_marketing_profiles": ("marketing_platform", "MARKETING_PROFILE"),
    "customer_product_telemetry": ("product_telemetry", "PRODUCT_TELEMETRY"),
    "email_delivery_events": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
    "customer_contact_cache": ("cache_service", "CACHE_ENTRY"),
}


def split_sql_values(text: str) -> List[Optional[str]]:
    """Split the inside of a ``VALUES(...)`` tuple into python strings."""

    values: List[Optional[str]] = []
    current: List[str] = []
    in_string = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            if char == "'":
                if index + 1 < length and text[index + 1] == "'":
                    current.append("'")
                    index += 2
                    continue
                in_string = False
                index += 1
                continue
            current.append(char)
            index += 1
            continue
        if char == "'":
            in_string = True
            index += 1
            continue
        if char == ",":
            values.append(_finish(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    values.append(_finish(current))
    return values


def _finish(chars: Sequence[str]) -> Optional[str]:
    text = "".join(chars).strip()
    if not text:
        return ""
    if text.upper() == "NULL":
        return None
    return text


def parse_create_tables(sql: str) -> Dict[str, List[str]]:
    tables: Dict[str, List[str]] = {}
    for name, body in _CREATE_RE.findall(sql):
        columns: List[str] = []
        for part in body.split(","):
            part = part.strip()
            if not part or part.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "CHECK", "UNIQUE")):
                continue
            token = part.split()[0].strip('"')
            if token:
                columns.append(token)
        tables[name] = columns
    return tables


def parse_inserts(
    sql: str, tables: Optional[Dict[str, List[str]]] = None
) -> List[Tuple[str, Dict[str, Optional[str]]]]:
    rows: List[Tuple[str, Dict[str, Optional[str]]]] = []
    for statement in _INSERT_RE.finditer(sql):
        table = statement.group(1)
        columns_text = statement.group(2)
        tuple_text = statement.group(3)
        inner = tuple_text[tuple_text.find("(") + 1: tuple_text.rfind(")")]
        values = split_sql_values(inner)
        if columns_text:
            columns = [c.strip().strip('"') for c in columns_text.split(",")]
        elif tables and tables.get(table):
            # ``INSERT INTO t VALUES(...)`` without a column list: SQLite binds
            # positionally to the declared column order.
            columns = list(tables[table])
        else:
            columns = []
        row: Dict[str, Optional[str]] = {}
        for index, value in enumerate(values):
            key = columns[index] if index < len(columns) else f"col{index}"
            row[key] = value
        rows.append((table, row))
    return rows


class SqlSnapshot:
    """Parsed SQL evidence file."""

    def __init__(self, sql: str):
        self.tables = parse_create_tables(sql)
        self.rows = parse_inserts(sql, self.tables)
        self.header_capture = None
        for line in sql.splitlines()[:12]:
            match = re.search(r"Captured at:\s*(\S+)", line)
            if match:
                self.header_capture = match.group(1)
            if line.strip().startswith("--") and "topology" in line.lower():
                self.topology = line.split(":", 1)[-1].strip()
        self.rows_by_table: Dict[str, List[Dict[str, Optional[str]]]] = {}
        for table, row in self.rows:
            self.rows_by_table.setdefault(table, []).append(row)
        #: systems whose tables this snapshot captures completely.
        #:
        #: Only tables that hold the authoritative copy are accepted.  An
        #: inventory export declares its scope explicitly in ``snapshot_scope``;
        #: a replica export is authoritative only for its ``*_primary`` table,
        #: because a stale replica legitimately lacks records that still exist.
        #: A table with no rows is never treated as proof of absence: several
        #: exports in the corpus are near-empty captures that never collected
        #: data, and accepting those would erase every live record.
        self.absent_scope: Set[str] = set()
        self._compute_absent_scope()

    def _compute_absent_scope(self) -> None:
        declared = {
            (row.get("origin_system") or "").strip()
            for row in self.rows_by_table.get("snapshot_scope", [])
        }
        declared.discard("")
        if declared:
            self.absent_scope.update(declared)
            return
        for table, rows in self.rows_by_table.items():
            if not rows:
                continue
            if table == "customer_data_primary":
                for row in rows:
                    origin = (row.get("origin_system") or "").strip()
                    if origin:
                        self.absent_scope.add(origin)
                continue
            if table.endswith("_primary"):
                mapped = TABLE_MAP.get(table)
                if mapped:
                    self.absent_scope.add(mapped[0])

    # -- helpers ---------------------------------------------------------
    def resource_presence(self) -> Dict[Tuple[str, str], int]:
        """Count how many tables of each system hold each resource id."""

        counts: Dict[Tuple[str, str], int] = {}
        for system, resource, _category, _state, _fields in self.iter_records():
            counts[(system, resource)] = counts.get((system, resource), 0) + 1
        return counts

    def iter_records(self):
        """Yield ``(system, resource_id, category, state, fields)`` tuples."""

        for table, rows in self.rows_by_table.items():
            mapped = TABLE_MAP.get(table)
            if mapped:
                system, category = mapped
                for row in rows:
                    parsed = self._row_state(table, category, row)
                    if parsed is not None:
                        yield (system, row.get("resource_id") or "", category, parsed[0], parsed[1])
                continue
            if table.startswith("customer_data_"):
                for row in rows:
                    record_json = row.get("record_json") or ""
                    try:
                        record = json.loads(record_json)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    collection = (row.get("collection_name") or record.get("collection_name") or "")
                    origin = (row.get("origin_system") or record.get("origin_system") or "")
                    system = origin or COLLECTION_MAP.get(collection, ("", ""))[0]
                    category = COLLECTION_MAP.get(collection, ("", ""))[1]
                    if not category:
                        category = SYSTEM_CATEGORY_MAP.get(origin, "UNKNOWN")
                    topology = table[len("customer_data_"):]
                    if topology != "primary":
                        system = f"{system}@{topology}"
                    resource = record.get("resource_id") or row.get("resource_id") or ""
                    parsed = self._json_record_state(category, record)
                    yield (system, resource, category, parsed[0], parsed[1])

    def _row_state(
        self, table: str, category: str, row: Dict[str, Optional[str]]
    ) -> Optional[Tuple[str, Dict[str, object]]]:
        fields: Dict[str, object] = {"row": row, "table": table}

        if category == "USER_PROFILE":
            fields.update(
                {
                    "email": row.get("email"),
                    "phone": row.get("phone"),
                    "email_hash": row.get("email_hash"),
                    "full_name": row.get("full_name"),
                    "user_id": row.get("user_id") or row.get("customer_reference"),
                    "account_status": row.get("account_status"),
                }
            )
            return (PRESENT, fields)

        if category == "SEARCH_INDEX_ENTRY":
            fields.update(
                {
                    "email": row.get("email"),
                    "phone": row.get("phone"),
                    "email_hash": row.get("email_hash"),
                    "display_name": row.get("display_name"),
                    "customer_reference": row.get("customer_reference"),
                    "source_resource_id": row.get("source_resource_id"),
                }
            )
            return (PRESENT, fields)

        if category == "ANALYTICS_PROFILE":
            fields.update(
                {
                    "email": row.get("email"),
                    "phone": row.get("phone"),
                    "email_hash": row.get("email_hash"),
                    "customer_reference": row.get("customer_reference"),
                }
            )
            state = ANONYMIZED if not _has_identifier(fields) else PRESENT
            return (state, fields)

        if category == "SUPPORT_MESSAGE":
            fields.update(
                {
                    "requester_email": row.get("requester_email"),
                    "requester_phone": row.get("requester_phone"),
                    "requester_name": row.get("requester_name"),
                    "customer_reference": row.get("customer_reference"),
                    "message_excerpt": row.get("message_excerpt"),
                }
            )
            # The internal customer reference may stay: only direct identifiers
            # (name, email, phone) have to be removed from a retained message.
            state = REDACTED if not _direct_identifier(fields) else PRESENT
            return (state, fields)

        if category == "USER_EXPORT":
            fields.update(
                {
                    "owner_email": row.get("owner_email"),
                    "owner_phone": row.get("owner_phone"),
                    "customer_reference": row.get("customer_reference"),
                    "object_key": row.get("object_key"),
                }
            )
            return (PRESENT, fields)

        if category == "DATA_FLOW":
            enabled = str(row.get("enabled") or "").strip()
            fields.update(
                {
                    "customer_email": row.get("customer_email"),
                    "customer_reference": row.get("customer_reference"),
                    "connector_name": row.get("connector_name"),
                }
            )
            return (DISABLED if enabled in ("0", "false", "False", "no") else PRESENT, fields)

        if category == "BACKUP_ARCHIVE":
            allowed = str(row.get("restore_allowed") or "").strip()
            fields.update(
                {
                    "customer_email": row.get("customer_email"),
                    "customer_reference": row.get("customer_reference"),
                    "archive_key": row.get("archive_key"),
                }
            )
            return (RESTORE_ALLOWED if allowed in ("1", "true", "True", "yes") else BLOCKED, fields)

        if category == "SECURITY_AUDIT_LOG":
            fields.update(
                {
                    "subject_reference": row.get("subject_reference"),
                    "event_code": row.get("event_code"),
                    "retention_until": row.get("retention_until"),
                }
            )
            return (PRESENT, fields)

        return None

    def _json_record_state(
        self, category: str, record: Dict[str, object]
    ) -> Tuple[str, Dict[str, object]]:
        fields: Dict[str, object] = {"record": record}
        if category == "USER_PROFILE":
            document = record
            fields.update(
                {
                    "email": document.get("email"),
                    "phone": document.get("phone"),
                    "email_hash": document.get("email_hash"),
                    "full_name": document.get("full_name"),
                    "user_id": document.get("user_id") or document.get("customer_reference"),
                    "account_status": document.get("account_status"),
                }
            )
            return (PRESENT, fields)

        if category == "SEARCH_INDEX_ENTRY":
            document = record.get("document") if isinstance(record.get("document"), dict) else record
            fields.update(
                {
                    "email": document.get("email"),
                    "phone": document.get("phone"),
                    "email_hash": document.get("email_hash"),
                    "display_name": document.get("display_name"),
                    "customer_reference": document.get("customer_reference"),
                }
            )
            return (PRESENT, fields)

        if category == "ANALYTICS_PROFILE":
            fields.update(
                {
                    "email": record.get("email"),
                    "phone": record.get("phone"),
                    "email_hash": record.get("email_hash"),
                    "customer_reference": record.get("user_id") or record.get("customer_reference"),
                }
            )
            return (ANONYMIZED if not _has_identifier(fields) else PRESENT, fields)

        if category == "SUPPORT_MESSAGE":
            document = record.get("document") if isinstance(record.get("document"), dict) else record
            fields.update(
                {
                    "requester_email": document.get("requester_email") or document.get("email"),
                    "requester_phone": document.get("requester_phone") or document.get("phone"),
                    "requester_name": document.get("requester_name") or document.get("name"),
                    "customer_reference": document.get("customer_reference"),
                }
            )
            return (REDACTED if not _direct_identifier(fields) else PRESENT, fields)

        if category == "USER_EXPORT":
            fields.update(
                {
                    "owner_email": record.get("owner_email") or record.get("email"),
                    "owner_phone": record.get("owner_phone") or record.get("phone"),
                    "customer_reference": record.get("customer_reference"),
                }
            )
            return (PRESENT, fields)

        if category == "DATA_FLOW":
            enabled = record.get("enabled")
            fields.update(
                {
                    "customer_email": record.get("customer_email") or record.get("email"),
                    "customer_reference": record.get("subject_selector") or record.get("customer_reference"),
                }
            )
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() in ("1", "true", "yes")
            return (PRESENT if enabled else DISABLED, fields)

        if category == "BACKUP_ARCHIVE":
            allowed = record.get("restore_allowed")
            if isinstance(allowed, str):
                allowed = allowed.strip().lower() in ("1", "true", "yes")
            fields.update(
                {
                    "customer_email": record.get("customer_email") or record.get("email"),
                    "customer_reference": record.get("subject_reference") or record.get("customer_reference"),
                }
            )
            return (RESTORE_ALLOWED if allowed else BLOCKED, fields)

        if category == "SECURITY_AUDIT_LOG":
            fields.update(
                {
                    "subject_reference": record.get("subject_reference"),
                    "event_code": record.get("event_code") or record.get("event_name"),
                }
            )
            return (PRESENT, fields)

        # Unknown collection: treat as a present record that carries whatever
        # identifiers it contains so the engine can still reason about it.
        fields.update(record)
        return (PRESENT, fields)


def _direct_identifier(fields: Dict[str, object]) -> bool:
    """Direct identifiers that a REDACT action has to remove."""

    for key in (
        "email",
        "phone",
        "email_hash",
        "requester_email",
        "requester_phone",
        "requester_name",
        "owner_email",
        "owner_phone",
        "customer_email",
        "display_name",
        "full_name",
        "name",
        "matched_value",
    ):
        value = fields.get(key)
        if value not in (None, "", "null", "None"):
            return True
    return False


def _has_identifier(fields: Dict[str, object]) -> bool:
    for key in (
        "email",
        "phone",
        "email_hash",
        "requester_email",
        "requester_phone",
        "requester_name",
        "owner_email",
        "owner_phone",
        "customer_email",
        "display_name",
        "full_name",
        "customer_reference",
        "user_id",
        "subject_reference",
        "customer_reference",
    ):
        value = fields.get(key)
        if value not in (None, "", "null", "None"):
            return True
    return False
