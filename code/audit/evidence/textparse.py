"""Low-level parsing helpers shared by the evidence readers.

Every helper here is deliberately tolerant: evidence files are operational
exports with several on-disk dialects (logfmt, JSON lines, syslog, bracket,
pipe, cloud-runtime envelopes).  A malformed line is skipped and counted, never
allowed to abort the run.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..identity import parse_ts_optional

_TS_PREFIX = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)

#: ``key=value`` or ``key="value with spaces"`` pairs.
_KV = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"((?:[^"\\]|\\.)*)"|([^\s|]+))')

#: ``[DEBUG]`` / ``| DEBUG |`` style severity markers.
_SEVERITY = re.compile(r"(?:\b(DEBUG|INFO|WARN|WARNING|ERROR|TRACE|CRITICAL)\b)")

#: ``event=...`` / ``event: ...`` / ``| event |`` event names.
_EVENT_KEYS = ("event", "event_name", "action")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_USER_RE = re.compile(r"\buser_\d+\b", re.IGNORECASE)
_HASH_RE = re.compile(r"\b[0-9a-f]{64}\b", re.IGNORECASE)
_RESOURCE_RE = re.compile(
    r"\b(?:account|search|support|analytics|export|backup|data_flow|tombstone|"
    r"audit_record|cache|marketing|email_delivery|legal|object|telemetry)_[0-9A-Za-z]+\b"
)
_PHONE_RE = re.compile(r"\+\d[\d\s\-().]{7,18}\d")


def split_timestamp(line: str) -> Tuple[Optional[datetime], str]:
    """Split a leading ISO timestamp from a log line."""

    match = _TS_PREFIX.match(line)
    if not match:
        return None, line
    return parse_ts_optional(match.group(1)), line[match.end():]


def parse_json_line(text: str) -> Optional[dict]:
    """Return the JSON object contained in ``text`` if there is one."""

    text = text.strip()
    start = text.find("{")
    if start < 0:
        return None
    candidate = text[start:]
    # Trim trailing non-JSON noise such as a stray ")" or "]".
    while candidate and candidate[-1] not in "}":
        candidate = candidate[:-1]
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        # Some exports embed a JSON string inside the message field.
        try:
            value = json.loads(candidate + "}")
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    flat: Dict[str, str] = {}
    _flatten(value, flat)
    return flat


def _flatten(value, out: Dict[str, str], prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}{key}"
            if isinstance(item, (dict, list)):
                _flatten(item, out, prefix=f"{name}.")
            else:
                out[name] = "" if item is None else str(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _flatten(item, out, prefix=f"{prefix}{index}.")


def parse_pairs(text: str) -> Dict[str, str]:
    """Parse ``key=value`` pairs, unquoting and unescaping values."""

    pairs: Dict[str, str] = {}
    for match in _KV.finditer(text):
        key = match.group(1)
        quoted = match.group(2)
        if quoted is not None:
            value = quoted.encode("utf-8").decode("unicode_escape", errors="ignore")
            value = value.replace('\\"', '"')
        else:
            value = match.group(3) or ""
        pairs[key] = value.strip()
    return pairs


def parse_pipe_fields(text: str) -> Dict[str, str]:
    """Parse ``a | b | c`` handover rows into ``field1..fieldN``."""

    parts = [part.strip() for part in text.split("|")]
    return {f"field{index + 1}": value for index, value in enumerate(parts)}


def detect_event(pairs: Dict[str, str]) -> Optional[str]:
    for key in _EVENT_KEYS:
        if pairs.get(key):
            return pairs[key]
    return None


def parse_line(line: str) -> Optional[Dict[str, str]]:
    """Parse one log line into a flat mapping.

    The mapping always contains ``_timestamp`` (ISO string, when present),
    ``_service`` and ``_event`` when those can be determined.
    """

    line = line.rstrip("\n")
    if not line.strip():
        return None
    when, rest = split_timestamp(line)
    record: Dict[str, str] = {}
    if when is not None:
        record["_timestamp"] = when.isoformat().replace("+00:00", "Z")

    structured = parse_json_line(rest)
    if structured:
        record.update(structured)
    else:
        record.update(parse_pairs(rest))

        # syslog: "<15>1 node-1 account_service tr-4973247 - - level=DEBUG"
        syslog = re.match(r"^\s*<\d+>\S+\s+\S+\s+(\S+)\s+(\S+)\s", rest)
        if syslog:
            record.setdefault("service", syslog.group(1))
            record.setdefault("trace_id", syslog.group(2))

        # bracket: "[DEBUG] [account_service] [tr-1] event: ..."
        bracket = re.match(
            r"^\s*\[[A-Z]+\]\s*\[([A-Za-z0-9_\-]+)\]\s*(?:\[([^\]]*)\])?\s*([A-Za-z0-9_]+):",
            rest,
        )
        if bracket:
            record.setdefault("service", bracket.group(1))
            if bracket.group(2):
                record.setdefault("trace_id", bracket.group(2))
            record.setdefault("event", bracket.group(3))

        # pipe: "2026-... | INFO | security_service | tr-1 | event | k=v"
        if "|" in rest:
            fields = [part.strip() for part in rest.split("|")]
            if len(fields) >= 5:
                if fields[1].upper() in {"DEBUG", "INFO", "WARN", "WARNING", "ERROR"}:
                    record.setdefault("severity", fields[1])
                    record.setdefault("service", fields[2])
                    record.setdefault("trace_id", fields[3])
                    record.setdefault("event", fields[4])
            elif len(fields) == 6 and fields[0].startswith("20"):
                # Handover row: ts | system | resource | type | result | subject
                record.setdefault("service", fields[1])
                record.setdefault("resource_id", fields[2])
                record.setdefault("record_type", fields[3])
                record.setdefault("result", fields[4])
                record.setdefault("subject_reference", fields[5])

    # Cloud-runtime envelope: message="instance=\"x\" event=\"y\" ..."
    message = record.get("message") or record.get("msg")
    if message:
        nested = parse_pairs(message)
        for key, value in nested.items():
            record.setdefault(key, value)

    if not record.get("service"):
        service = record.get("logger") or record.get("function") or record.get("source")
        if service:
            record["service"] = service

    event = detect_event(record)
    if event:
        record["_event"] = event
    return record


def find_emails(text: str) -> List[str]:
    return _EMAIL_RE.findall(text or "")


def find_user_ids(text: str) -> List[str]:
    return _USER_RE.findall(text or "")


def find_hashes(text: str) -> List[str]:
    return _HASH_RE.findall(text or "")


def find_resources(text: str) -> List[str]:
    return _RESOURCE_RE.findall(text or "")


def find_phones(text: str) -> List[str]:
    return [value.strip() for value in _PHONE_RE.findall(text or "")]
