"""Reader for log and readback evidence.

The operational exports are noisy: most lines are connection pools, cache
misses and retry chatter for unrelated traffic.  Only *terminal* events carry a
statement about a record (``*_completed``, ``record_lookup``, ``*_pii_scan``,
``deletion_marker_check``), so only those become observations.

``*_started`` lines are used solely to recover the lookup key (an email, hash or
``user_*`` reference) belonging to the same ``trace_id``; that key is what links
a resource back to a person.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from ..models import (
    ABSENT,
    ANONYMIZED,
    BLOCKED,
    DISABLED,
    PRESENT,
    REDACTED,
    RESTORE_ALLOWED,
)
from .textparse import parse_line
from .json_source import RawRecord, RESULT_TO_STATE, category_for


_TRACE_KEYS = (
    "lookup_key",
    "subject_key",
    "correlation_key",
    "scan_key",
    "query",
    "subject_ref",
)


def _truthy(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "allow", "allowed", "eligible"):
        return True
    if text in ("false", "0", "no", "deny", "denied", "blocked", "ineligible"):
        return False
    return None


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class LogFile:
    """Parsed log file with per-trace context."""

    def __init__(self, text: str):
        self.lines: List[Dict[str, str]] = []
        self.trace_context: Dict[str, Dict[str, str]] = {}
        self.unparsed = 0
        for raw in text.splitlines():
            parsed = parse_line(raw)
            if parsed is None:
                continue
            if not parsed.get("_timestamp") and not parsed.get("_event"):
                self.unparsed += 1
                continue
            self.lines.append(parsed)
        for line in self.lines:
            trace = line.get("trace_id")
            if not trace:
                continue
            context = self.trace_context.setdefault(trace, {})
            for key in _TRACE_KEYS:
                if line.get(key):
                    context.setdefault(key, line[key])

    def records(self) -> List[RawRecord]:
        out: List[RawRecord] = []
        for line in self.lines:
            out.extend(self._records_from_line(line))
        return out

    # ------------------------------------------------------------------
    def _link_fields(self, line: Dict[str, str]) -> Dict[str, object]:
        """Identity-bearing fields, including the trace's lookup key."""

        fields: Dict[str, object] = {}
        trace = line.get("trace_id")
        context = self.trace_context.get(trace or "", {})
        for key, value in line.items():
            if key.startswith("_") or value in (None, "", "null", "None", '""'):
                continue
            fields[key] = value
        for key in _TRACE_KEYS:
            if key not in fields and context.get(key):
                fields[key] = context[key]
        service = line.get("service") or line.get("logger")
        if service:
            fields.setdefault("service", service)
        return fields

    def _records_from_line(self, line: Dict[str, str]) -> List[RawRecord]:
        event = line.get("_event")
        if not event:
            return []
        timestamp = line.get("_timestamp")
        service = line.get("service") or line.get("logger") or ""
        resource = line.get("resource_id") or ""

        handler = _EVENT_HANDLERS.get(event)
        if handler is not None:
            return handler(self, line, service, resource, timestamp)
        return []

    # -- individual event families --------------------------------------
    def _profile_lookup(self, line, service, resource, timestamp) -> List[RawRecord]:
        rows = _int_or_none(line.get("rows"))
        status = _int_or_none(line.get("http_status"))
        if rows is None and status is None:
            return []
        absent = (rows == 0) or (status == 404)
        fields = self._link_fields(line)
        return [
            RawRecord(
                "account_service",
                resource,
                "USER_PROFILE",
                ABSENT if absent else PRESENT,
                fields,
                timestamp,
            )
        ]

    def _index_query(self, line, service, resource, timestamp) -> List[RawRecord]:
        result = (line.get("result") or "").strip().lower()
        hits = _int_or_none(line.get("total_hits"))
        if isinstance(line.get("hits"), str):
            hits = _int_or_none(line.get("hits"))
        absent = result in ("miss", "not_found", "none", "no_hit") or hits == 0
        fields = self._link_fields(line)
        return [
            RawRecord(
                "search_service",
                resource,
                "SEARCH_INDEX_ENTRY",
                ABSENT if absent else PRESENT,
                fields,
                timestamp,
            )
        ]

    def _analytics_profile(self, line, service, resource, timestamp) -> List[RawRecord]:
        fields = self._link_fields(line)
        direct = [
            line.get("email"),
            line.get("phone"),
            line.get("email_hash"),
            line.get("customer_reference"),
        ]
        try:
            count = int(str(line.get("direct_identifiers") or "0"))
        except ValueError:
            count = 0
        direct = [v for v in direct if v not in (None, "", "null", "None")]
        if count > 0:
            direct.append("direct_identifiers")
        return [
            RawRecord(
                "analytics_platform",
                resource,
                "ANALYTICS_PROFILE",
                PRESENT if direct else ANONYMIZED,
                fields,
                timestamp,
            )
        ]

    def _support_ticket(self, line, service, resource, timestamp) -> List[RawRecord]:
        fields = self._link_fields(line)
        direct = [
            line.get("requester_email"),
            line.get("requester_phone"),
            line.get("requester_name"),
        ]
        direct = [v for v in direct if v not in (None, "", "null", "None")]
        category = "SUPPORT_MESSAGE"
        if str(line.get("record_type") or "").upper() == "SUPPORT_ATTACHMENT":
            category = "SUPPORT_ATTACHMENT"
        return [
            RawRecord(
                "support_platform",
                resource,
                category,
                PRESENT if direct else REDACTED,
                fields,
                timestamp,
            )
        ]

    def _connector(self, line, service, resource, timestamp) -> List[RawRecord]:
        enabled = _truthy(line.get("enabled"))
        if enabled is None:
            enabled = (line.get("last_delivery_status") or "").lower() not in ("stopped", "disabled")
        fields = self._link_fields(line)
        return [
            RawRecord(
                "integration_service",
                resource,
                "DATA_FLOW",
                PRESENT if enabled else DISABLED,
                fields,
                timestamp,
            )
        ]

    def _restore_policy(self, line, service, resource, timestamp) -> List[RawRecord]:
        allowed = _truthy(line.get("restore_allowed"))
        decision = (line.get("decision") or "").lower()
        if allowed is None and decision:
            allowed = decision in ("allow", "allowed", "approve", "approved")
        if allowed is None:
            eligible = _truthy(line.get("eligible"))
            deletion_block = _truthy(line.get("deletion_block"))
            if eligible is not None:
                allowed = eligible and not bool(deletion_block)
        if allowed is None:
            return []
        fields = self._link_fields(line)
        return [
            RawRecord(
                "backup_service",
                resource,
                "BACKUP_ARCHIVE",
                RESTORE_ALLOWED if allowed else BLOCKED,
                fields,
                timestamp,
            )
        ]

    def _export(self, line, service, resource, timestamp) -> List[RawRecord]:
        available = _truthy(line.get("available"))
        if available is None:
            available = True
        fields = self._link_fields(line)
        return [
            RawRecord(
                "export_service",
                resource,
                "USER_EXPORT",
                PRESENT if available else ABSENT,
                fields,
                timestamp,
            )
        ]

    def _analytics_contact_lookup(
        self, line, service, resource, timestamp
    ) -> List[RawRecord]:
        if not resource:
            return []
        rows = _int_or_none(line.get("rows"))
        fields = self._link_fields(line)
        direct = [
            line.get("email"),
            line.get("phone"),
            line.get("email_hash"),
            line.get("correlation_key"),
        ]
        direct = [v for v in direct if v not in (None, "", "null", "None")]
        if rows == 0:
            state = ABSENT
            category = "SECONDARY_IDENTIFIER"
        else:
            state = PRESENT if direct else ANONYMIZED
            category = "SECONDARY_IDENTIFIER" if line.get("email_hash") else "ANALYTICS_PROFILE"
        return [
            RawRecord(
                "analytics_platform",
                resource,
                category,
                state,
                fields,
                timestamp,
            )
        ]

    def _audit_record(self, line, service, resource, timestamp) -> List[RawRecord]:
        rows = _int_or_none(line.get("rows"))
        if rows == 0:
            state = ABSENT
        else:
            state = PRESENT
        fields = self._link_fields(line)
        return [
            RawRecord(
                "security_service",
                resource,
                "SECURITY_AUDIT_LOG",
                state,
                fields,
                timestamp,
            )
        ]

    def _contact_query(self, line, service, resource, timestamp) -> List[RawRecord]:
        rows = _int_or_none(line.get("rows"))
        if rows is None:
            return []
        fields = self._link_fields(line)
        # A legacy contact-index hit carries no conversation context, so the
        # record is a subject-only support artefact that must be removed whole
        # rather than redacted in place.
        direct = [
            line.get("requester_email"),
            line.get("requester_phone"),
            line.get("requester_name"),
        ]
        direct = [v for v in direct if v not in (None, "", "null", "None")]
        category = "SUPPORT_MESSAGE" if direct else "SUPPORT_ATTACHMENT"
        return [
            RawRecord(
                "support_platform",
                resource,
                category,
                PRESENT if rows > 0 else ABSENT,
                fields,
                timestamp,
            )
        ]

    def _pii_scan(self, line, service, resource, timestamp) -> List[RawRecord]:
        matched = line.get("matched_value") or ""
        if not matched or matched in ("null", "None"):
            return []
        fields = self._link_fields(line)
        fields["matched_value"] = matched
        category = "SUPPORT_ATTACHMENT" if "attachment" in (line.get("_event") or "") else "SUPPORT_MESSAGE"
        return [
            RawRecord(
                "support_platform",
                resource,
                category,
                PRESENT,
                fields,
                timestamp,
            )
        ]

    def _record_lookup(self, line, service, resource, timestamp) -> List[RawRecord]:
        system = line.get("system") or ""
        category = category_for(str(line.get("record_type") or ""))
        if not category:
            return []
        result = (line.get("result") or "").upper()
        state = RESULT_TO_STATE.get(result)
        if state is None:
            return []
        fields = self._link_fields(line)
        if line.get("subject_reference"):
            fields["subject_reference"] = line["subject_reference"]
        return [RawRecord(system or _system_for(category), resource, category, state, fields, timestamp)]


def _system_for(category: str) -> str:
    from ..models import CATEGORY_SYSTEM_MAP

    return CATEGORY_SYSTEM_MAP.get(category, "unknown_service")


_EVENT_HANDLERS = {
    "profile_lookup_completed": LogFile._profile_lookup,
    "index_query_completed": LogFile._index_query,
    "analytics_profile_read_completed": LogFile._analytics_profile,
    "analytics_profile_read": LogFile._analytics_profile,
    "support_ticket_read_completed": LogFile._support_ticket,
    "message_read": LogFile._support_ticket,
    "connector_config_read_completed": LogFile._connector,
    "connector_config_loaded": LogFile._connector,
    "connector_heartbeat": LogFile._connector,
    "restore_policy_read_completed": LogFile._restore_policy,
    "restore_authorization_completed": LogFile._restore_policy,
    "restore_candidate_evaluated": LogFile._restore_policy,
    "export_metadata_read_completed": LogFile._export,
    "export_download_check_completed": LogFile._export,
    "audit_record_read_completed": LogFile._audit_record,
    "contact_query_completed": LogFile._contact_query,
    "contact_lookup_completed": LogFile._analytics_contact_lookup,
    "message_pii_scan": LogFile._pii_scan,
    "attachment_pii_scan": LogFile._pii_scan,
    "record_lookup": LogFile._record_lookup,
}
