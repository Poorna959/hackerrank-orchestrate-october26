"""Reader for JSON evidence files.

Three families appear in ``dataset/evidence/records``:

* ``service_resource_export_*.json`` - a per-service capture with one array per
  collection (profile lookups, analytics profiles, connector configs, ...).
* ``customer_data_snapshot_bundle_*.json`` - ``snapshots`` keyed by data store
  and collection.
* flat record sets (``connector_configurations``, ``security_audit_ledger``,
  ``customer_export_jobs``, ``customer_analytics_report``,
  ``customer_export_manifest``) that share the
  ``record_type`` / ``result`` / ``subject_reference`` vocabulary.

Every reader returns ``RawRecord`` objects; identifier extraction and ownership
resolution happen in :mod:`audit.evidence.loader` so all formats share one
linking rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from ..models import (
    ABSENT,
    ANONYMIZED,
    BLOCKED,
    DISABLED,
    PRESENT,
    REDACTED,
    RESTORE_ALLOWED,
)

#: collection / record_type vocabulary -> data category
COLLECTION_TO_CATEGORY: Dict[str, str] = {
    "customer_profiles": "USER_PROFILE",
    "USER_PROFILE": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "SEARCH_INDEX_ENTRY": "SEARCH_INDEX_ENTRY",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "ANALYTICS_PROFILE": "ANALYTICS_PROFILE",
    "support_records": "SUPPORT_MESSAGE",
    "SUPPORT_MESSAGE": "SUPPORT_MESSAGE",
    "SUPPORT_ATTACHMENT": "SUPPORT_ATTACHMENT",
    "customer_exports": "USER_EXPORT",
    "customer_export_jobs": "USER_EXPORT",
    "USER_EXPORT": "USER_EXPORT",
    "connector_subscriptions": "DATA_FLOW",
    "connector_configurations": "DATA_FLOW",
    "DATA_FLOW": "DATA_FLOW",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "backup_catalog": "BACKUP_ARCHIVE",
    "BACKUP_ARCHIVE": "BACKUP_ARCHIVE",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "SECURITY_AUDIT_LOG": "SECURITY_AUDIT_LOG",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "ACCOUNT_TOMBSTONE": "ACCOUNT_TOMBSTONE",
    "marketing_profiles": "MARKETING_PROFILE",
    "customer_marketing_profiles": "MARKETING_PROFILE",
    "MARKETING_PROFILE": "MARKETING_PROFILE",
    "email_delivery_events": "EMAIL_DELIVERY_LOG",
    "EMAIL_DELIVERY_LOG": "EMAIL_DELIVERY_LOG",
    "legal_hold_records": "LEGAL_HOLD_RECORD",
    "LEGAL_HOLD_RECORD": "LEGAL_HOLD_RECORD",
    "product_telemetry": "PRODUCT_TELEMETRY",
    "PRODUCT_TELEMETRY": "PRODUCT_TELEMETRY",
    "customer_cache_entries": "CACHE_ENTRY",
    "CACHE_ENTRY": "CACHE_ENTRY",
    "attachment_catalog": "SUPPORT_ATTACHMENT",
}

#: result vocabulary -> state
RESULT_TO_STATE: Dict[str, str] = {
    "FOUND": PRESENT,
    "PRESENT": PRESENT,
    "IDENTIFIABLE": PRESENT,
    "NOT_FOUND": ABSENT,
    "ABSENT": ABSENT,
    "MISSING": ABSENT,
    "DELETED": ABSENT,
    "REDACTED": REDACTED,
    "ANONYMIZED": ANONYMIZED,
    "DISABLED": DISABLED,
    "BLOCKED": BLOCKED,
    "ACTIVE": PRESENT,
    "ENABLED": PRESENT,
    "RESTORE_ALLOWED": RESTORE_ALLOWED,
}


@dataclass
class RawRecord:
    """Format-independent record produced by an evidence reader."""

    system_id: str
    resource_id: str
    category: str
    state: str
    fields: Dict[str, object]
    observed_at: Optional[str] = None


#: Categories whose compliance state is derived from the fields present, not
#: from the record-level result word.
_DERIVED_STATE_CATEGORIES = frozenset(
    {"ACCOUNT_TOMBSTONE", "SUPPORT_MESSAGE", "ANALYTICS_PROFILE"}
)


def _state_for_result(result: str, category: str) -> Optional[str]:
    key = (result or "").strip().upper()
    if not key:
        return None
    if key == "ACTIVE" and category == "BACKUP_ARCHIVE":
        return RESTORE_ALLOWED
    if key == "ACTIVE" and category == "DATA_FLOW":
        return PRESENT
    return RESULT_TO_STATE.get(key)


def category_for(value: str) -> Optional[str]:
    if not value:
        return None
    return COLLECTION_TO_CATEGORY.get(value) or COLLECTION_TO_CATEGORY.get(value.upper())


def parse_service_export(payload: dict) -> List[RawRecord]:
    records: List[RawRecord] = []
    exports = payload.get("service_exports")
    if not isinstance(exports, dict):
        return records
    for system, collections in exports.items():
        if not isinstance(collections, dict):
            continue
        for collection, entries in collections.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                observed = entry.get("observed_at") or entry.get("captured_at")
                resource = str(entry.get("resource_id") or "")
                category = _service_export_category(system, collection)

                if collection == "profile_lookups":
                    profile = entry.get("profile")
                    status = entry.get("status_code")
                    if profile is None or status in (404, "404"):
                        records.append(
                            RawRecord(
                                "account_service",
                                resource,
                                "USER_PROFILE",
                                ABSENT,
                                {"lookup": entry},
                                observed,
                            )
                        )
                    else:
                        fields = dict(profile)
                        fields["lookup"] = entry
                        records.append(
                            RawRecord(
                                "account_service",
                                resource,
                                "USER_PROFILE",
                                PRESENT,
                                fields,
                                observed,
                            )
                        )
                    continue

                if collection == "index_queries":
                    hits = entry.get("hits") or {}
                    documents = hits.get("documents") if isinstance(hits, dict) else None
                    documents = documents or []
                    if documents:
                        for document in documents:
                            document = dict(document or {})
                            document.setdefault("resource_id", document.get("resource_id") or resource)
                            records.append(
                                RawRecord(
                                    "search_service",
                                    str(document.get("resource_id") or resource),
                                    "SEARCH_INDEX_ENTRY",
                                    PRESENT,
                                    document,
                                    observed,
                                )
                            )
                    else:
                        records.append(
                            RawRecord(
                                "search_service",
                                resource,
                                "SEARCH_INDEX_ENTRY",
                                ABSENT,
                                {"query": entry.get("query")},
                                observed,
                            )
                        )
                    continue

                if collection == "restore_entries":
                    allowed = entry.get("restore_allowed")
                    records.append(
                        RawRecord(
                            "backup_service",
                            resource,
                            "BACKUP_ARCHIVE",
                            RESTORE_ALLOWED if allowed else BLOCKED,
                            dict(entry),
                            observed,
                        )
                    )
                    continue

                if collection == "connectors":
                    enabled = entry.get("enabled")
                    records.append(
                        RawRecord(
                            "integration_service",
                            resource,
                            "DATA_FLOW",
                            PRESENT if enabled else DISABLED,
                            dict(entry),
                            observed,
                        )
                    )
                    continue

                state = PRESENT
                if category in ("SUPPORT_MESSAGE", "ANALYTICS_PROFILE", "ACCOUNT_TOMBSTONE"):
                    state = _record_state(category, entry)[0]
                records.append(
                    RawRecord(
                        str(system),
                        resource,
                        category,
                        state,
                        dict(entry),
                        observed,
                    )
                )
    return records


def _service_export_category(system: str, collection: str) -> str:
    mapping = {
        "profile_lookups": "USER_PROFILE",
        "profiles": "ANALYTICS_PROFILE",
        "restore_entries": "BACKUP_ARCHIVE",
        "exports": "USER_EXPORT",
        "connectors": "DATA_FLOW",
        "index_queries": "SEARCH_INDEX_ENTRY",
        "audit_records": "SECURITY_AUDIT_LOG",
        "tickets": "SUPPORT_MESSAGE",
    }
    if collection in mapping:
        return mapping[collection]
    return category_for(collection) or "UNKNOWN"


def parse_snapshot_bundle(payload: dict) -> List[RawRecord]:
    records: List[RawRecord] = []
    for snapshot in payload.get("snapshots") or []:
        if not isinstance(snapshot, dict):
            continue
        system = str(snapshot.get("data_store") or "unknown_service")
        collection = str(snapshot.get("collection") or "")
        category = category_for(collection) or "UNKNOWN"
        captured = snapshot.get("captured_at") or payload.get("exported_at")
        for entry in snapshot.get("records") or []:
            if not isinstance(entry, dict):
                continue
            resource = str(entry.get("resource_id") or "")
            state, fields = _record_state(category, entry)
            records.append(RawRecord(system, resource, category, state, fields, captured))
    return records


def _record_state(category: str, entry: dict):
    fields = dict(entry)
    if category == "ACCOUNT_TOMBSTONE":
        direct = [
            entry.get("name"),
            entry.get("full_name"),
            entry.get("email"),
            entry.get("phone"),
            entry.get("email_hash"),
        ]
        cleaned = [value for value in direct if value not in (None, "", "null")]
        return (PRESENT if cleaned else REDACTED, fields)
    if category == "ANALYTICS_PROFILE":
        direct = [
            entry.get("user_id") or entry.get("customer_reference"),
            entry.get("email"),
            entry.get("phone"),
            entry.get("email_hash"),
        ]
        cleaned = [value for value in direct if value not in (None, "", "null")]
        return (PRESENT if cleaned else ANONYMIZED, fields)
    if category == "SUPPORT_MESSAGE":
        direct = [
            entry.get("requester_email"),
            entry.get("requester_phone"),
            entry.get("requester_name"),
            entry.get("email"),
            entry.get("phone"),
            entry.get("name"),
            entry.get("matched_value"),
        ]
        cleaned = [value for value in direct if value not in (None, "", "null")]
        return (PRESENT if cleaned else REDACTED, fields)
    if category == "DATA_FLOW":
        enabled = entry.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("1", "true", "yes")
        return (PRESENT if enabled else DISABLED, fields)
    if category == "BACKUP_ARCHIVE":
        allowed = entry.get("restore_allowed")
        if isinstance(allowed, str):
            allowed = allowed.strip().lower() in ("1", "true", "yes")
        return (RESTORE_ALLOWED if allowed else BLOCKED, fields)
    return (PRESENT, fields)


def parse_record_set(payload: dict) -> List[RawRecord]:
    """Flat records sharing the ``record_type``/``result`` vocabulary."""

    records: List[RawRecord] = []
    entries = payload.get("records")
    if not isinstance(entries, list):
        return records
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        category = category_for(str(entry.get("record_type") or "")) or category_for(
            str(entry.get("collection") or "")
        )
        system = str(entry.get("system") or entry.get("service") or "")
        resource = str(entry.get("resource_id") or "")
        result = str(entry.get("result") or "")
        if category is None:
            category = _guess_category(entry, system, resource)
        if category is None:
            continue
        state = _state_for_result(result, category)
        derived = _record_state(category, entry)[0]
        if category in _DERIVED_STATE_CATEGORIES:
            # For these categories the record-level result only says whether the
            # row was found; whether the row is compliant depends on which
            # identifiers it still carries.  "FOUND" must therefore not be read
            # as "exposed": a tombstone with no direct identifier is REDACTED and
            # an unattributable analytics row is ANONYMIZED.
            if state in (None, PRESENT):
                state = derived
        elif state is None:
            state = derived
        records.append(
            RawRecord(
                system or "unknown_service",
                resource,
                category,
                state,
                dict(entry),
                entry.get("recorded_at") or entry.get("observed_at"),
            )
        )
    return records


def _guess_category(entry: dict, system: str, resource: str) -> Optional[str]:
    if entry.get("download_available") is not None:
        return "USER_EXPORT"
    if "reporting_window" in entry or "metrics" in entry:
        return "ANALYTICS_PROFILE"
    if entry.get("connector_name") or entry.get("enabled") is not None:
        return "DATA_FLOW"
    if entry.get("restore_allowed") is not None:
        return "BACKUP_ARCHIVE"
    if entry.get("archive_key") or entry.get("archive_path"):
        return "BACKUP_ARCHIVE"
    if entry.get("event_name") or entry.get("retention_until"):
        return "SECURITY_AUDIT_LOG"
    if entry.get("message_excerpt") or entry.get("requester_email"):
        return "SUPPORT_MESSAGE"
    if entry.get("object_key") or entry.get("owner_email"):
        return "USER_EXPORT"
    if entry.get("account_status") or entry.get("full_name"):
        return "USER_PROFILE"
    return None
