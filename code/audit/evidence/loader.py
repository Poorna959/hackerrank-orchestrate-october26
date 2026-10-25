"""Evidence loading: format dispatch and identifier linking.

Each evidence file is parsed into :class:`RawRecord` statements by a
format-specific reader.  This module then resolves *who* each record belongs to
by extracting identifiers from the record and matching them against the identity
index (current identifiers plus ``user_audit_log`` history).

Linking precedence matters:

1. an explicit internal reference (``user_id`` / ``customer_reference`` /
   ``subject_reference`` / ``subject_selector``) wins outright;
2. otherwise email, phone and email-hash values are used;
3. a human name is only used when nothing stronger is present.

This prevents a decoy record that merely shares a surname from being attributed
to the subject.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from ..identity import (
    IdentityIndex,
    NS_EMAIL,
    NS_HASH,
    NS_NAME,
    NS_PHONE,
    NS_USER,
    norm_email,
    norm_hash,
    norm_name,
    norm_phone,
    norm_user_id,
    parse_ts,
)
from .textparse import find_emails as find_emails_in_text

from ..models import Observation
from .json_source import (
    RawRecord,
    parse_record_set,
    parse_service_export,
    parse_snapshot_bundle,
)
from . import ocr_source
from .log_source import LogFile
from .pdf_source import extract_text, records_from_text
from .sql_source import SqlSnapshot

_REFERENCE_FIELDS = (
    "user_id",
    "customer_reference",
    "subject_reference",
    "subject_selector",
    "subject_key",
    "subject_ref",
    "correlation_key",
    "owner_user_id",
)
_EMAIL_FIELDS = (
    "email",
    "requester_email",
    "owner_email",
    "customer_email",
    "lookup_key",
    "query",
    "scan_key",
    "subject_ref",
    "matched_value",
    "subject_reference",
)
_HASH_FIELDS = ("email_hash", "hash")
_PHONE_FIELDS = ("phone", "requester_phone", "owner_phone")
_NAME_FIELDS = (
    "full_name",
    "display_name",
    "requester_name",
    "name",
    "customer_name",
)


@dataclass
class EvidenceFile:
    file_id: str
    path: Path
    origin: str
    captured_at: datetime


@dataclass
class EvidenceCorpus:
    """All observations plus per-file metadata."""

    files: Dict[str, EvidenceFile] = field(default_factory=dict)
    #: observations whose record could be tied to at least one identifier
    observations: List[Observation] = field(default_factory=list)
    #: every observation, indexed by resource id.  Readback traces and lookup
    #: exports describe a resource without naming its owner; the resource id is
    #: the only link, so it needs its own index.
    by_resource: Dict[str, List[Observation]] = field(default_factory=dict)
    #: systems that at least one snapshot captures completely
    absent_scope: Dict[str, Set[str]] = field(default_factory=dict)
    parse_notes: List[str] = field(default_factory=list)

    def observations_for(self, keys: FrozenSet[str]) -> List[Observation]:
        wanted = set(keys)
        return [obs for obs in self.observations if obs.link_keys & wanted]


def load_manifest(path: Path) -> List[EvidenceFile]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    files: List[EvidenceFile] = []
    for row in rows:
        file_id = (row.get("file_id") or "").strip()
        rel = (row.get("file_path") or "").strip()
        captured = (row.get("captured_at") or "").strip()
        if not file_id or not rel:
            continue
        files.append(
            EvidenceFile(
                file_id=file_id,
                path=path.parent / rel,
                origin=(row.get("origin") or "").strip(),
                captured_at=parse_ts(captured) if captured else None,
            )
        )
    return files


def _iter_strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_strings(item)


class EvidenceLoader:
    def __init__(self, index: IdentityIndex, dataset_dir: Path, ocr: bool = False):
        self.index = index
        self.dataset_dir = dataset_dir
        #: Screenshots are only read when OCR is explicitly requested: the
        #: default run must not depend on a local `tesseract` install.
        self.ocr = bool(ocr) and ocr_source.available()

    # ------------------------------------------------------------------
    def load(self, manifest_path: Path) -> EvidenceCorpus:
        corpus = EvidenceCorpus()
        for evidence in load_manifest(manifest_path):
            corpus.files[evidence.file_id] = evidence
            if not evidence.path.exists():
                corpus.parse_notes.append(
                    f"{evidence.file_id}: missing evidence file {evidence.path}"
                )
                continue
            try:
                records, absent_systems = self._parse_file(evidence)
            except Exception as exc:  # noqa: BLE001 - evidence must never abort a run
                corpus.parse_notes.append(f"{evidence.file_id}: {type(exc).__name__}: {exc}")
                continue
            for system in absent_systems:
                corpus.absent_scope.setdefault(system, set()).add(evidence.file_id)
            for record in records:
                observation = self._to_observation(evidence, record)
                if observation is None:
                    continue
                if observation.link_keys:
                    corpus.observations.append(observation)
                corpus.by_resource.setdefault(record.resource_id, []).append(observation)
        corpus.observations.sort(key=lambda obs: obs.sort_key())
        return corpus

    # ------------------------------------------------------------------
    def _parse_file(self, evidence: EvidenceFile) -> Tuple[List[RawRecord], Set[str]]:
        suffix = evidence.path.suffix.lower()
        absent_systems: Set[str] = set()
        records: List[RawRecord] = []

        if suffix in (".png", ".jpg", ".jpeg"):
            if not self.ocr:
                return [], set()
            return records_from_text(
                ocr_source.extract_text(evidence.path),
                _system_from_origin(evidence.origin),
            ), set()

        text = evidence.path.read_text(encoding="utf-8", errors="replace")

        if suffix == ".sql":
            snapshot = SqlSnapshot(text)
            absent_systems = set(snapshot.absent_scope)
            for system, resource, category, state, fields in snapshot.iter_records():
                records.append(
                    RawRecord(system, resource, category, state, fields, None)
                )
            return records, absent_systems

        if suffix == ".json":
            try:
                payload = json.loads(text)
            except ValueError:
                return [], set()
            if isinstance(payload, dict):
                if "service_exports" in payload:
                    return parse_service_export(payload), set()
                if "snapshots" in payload:
                    return parse_snapshot_bundle(payload), set()
                if "records" in payload:
                    return parse_record_set(payload), set()
            return [], set()

        if suffix in (".log", ".txt"):
            return self._parse_log(evidence, text), set()

        if suffix == ".pdf":
            extracted = extract_text(evidence.path)
            system = _system_from_origin(evidence.origin)
            return records_from_text(extracted, system), set()

        return [], set()

    def _parse_log(self, evidence: EvidenceFile, text: str) -> List[RawRecord]:
        if evidence.path.suffix.lower() == ".txt" and "|" in text:
            handover = _parse_handover(text)
            if handover:
                return handover
        log = LogFile(text)
        return log.records()

    # ------------------------------------------------------------------
    def _to_observation(
        self, evidence: EvidenceFile, record: RawRecord
    ) -> Optional[Observation]:
        if not record.resource_id:
            return None
        owners, keys, identifiers = self._resolve(record.fields, record.resource_id)
        observed = evidence.captured_at
        if record.observed_at:
            from ..identity import parse_ts_optional

            parsed = parse_ts_optional(record.observed_at)
            if parsed is not None:
                observed = parsed
        if observed is None:
            # A record with no timestamp in the record and none in the manifest
            # cannot be placed in time.  Treat it as undated-and-oldest so the
            # cut-off comparison stays total; an authoritative capture taken at
            # or before the cut-off still overrules it.
            observed = _UNDATED
        return Observation(
            file_id=evidence.file_id,
            captured_at=evidence.captured_at,
            observed_at=observed,
            system_id=record.system_id,
            resource_id=record.resource_id,
            category=record.category,
            state=record.state,
            link_keys=frozenset(keys),
            identifiers=frozenset(identifiers),
            detail=_short_detail(record.fields),
            source=evidence.origin,
        )

    def _resolve(
        self, fields: Dict[str, object], resource_id: str
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """Return (owners, link keys, direct identifiers) for one record."""

        owners: Set[str] = set()
        keys: Set[str] = set()
        identifiers: Set[str] = set()

        references = _collect(fields, _REFERENCE_FIELDS)
        for value in references:
            user_id = self.index.by_user_id(value)
            if user_id:
                owners.add(user_id)
                keys.add(NS_USER + norm_user_id(value))

        emails = _collect(fields, _EMAIL_FIELDS)
        # Any email-looking value anywhere in the payload still counts.
        for value in _iter_strings(fields):
            emails.update(find_emails_in_text(value))
        for email in emails:
            user_id = self.index.by_email(email)
            keys.add(NS_EMAIL + norm_email(email))
            identifiers.add(NS_EMAIL + norm_email(email))
            if user_id:
                owners.add(user_id)

        hashes = _collect(fields, _HASH_FIELDS)
        for value in _iter_strings(fields):
            for candidate in _hash_candidates(value):
                hashes.add(candidate)
        for digest in hashes:
            user_id = self.index.by_hash(digest)
            keys.add(NS_HASH + norm_hash(digest))
            identifiers.add(NS_HASH + norm_hash(digest))
            if user_id:
                owners.add(user_id)

        phones = _collect(fields, _PHONE_FIELDS)
        for value in phones:
            digits = norm_phone(value)
            if len(digits) >= 7:
                keys.add(NS_PHONE + digits)
                identifiers.add(NS_PHONE + digits)
                user_id = self.index.by_phone(value)
                if user_id:
                    owners.add(user_id)

        if owners:
            # An internal reference or a strong identifier already answered the
            # ownership question; names add nothing but false positives.
            for owner in owners:
                keys.add(NS_USER + norm_user_id(owner))
            return owners, keys, identifiers

        for value in _collect(fields, _NAME_FIELDS):
            normalized = norm_name(value)
            if not normalized:
                continue
            user_id = self.index.by_name(value)
            if user_id:
                owners.add(user_id)
                keys.add(NS_NAME + normalized)
                keys.add(NS_USER + norm_user_id(user_id))

        return owners, keys, identifiers


#: Timestamp used for evidence that carries no time information at all.
_UNDATED = datetime.min.replace(tzinfo=timezone.utc)


def _collect(fields: Dict[str, object], names: Sequence[str]) -> Set[str]:
    values: Set[str] = set()
    for name in names:
        value = fields.get(name)
        for text in _iter_strings(value):
            text = text.strip()
            if text and text.lower() not in ("null", "none", "n/a"):
                values.add(text)
    return values


def _hash_candidates(text: str) -> Set[str]:
    import re

    return set(re.findall(r"\b[0-9a-fA-F]{64}\b", text or ""))


def _short_detail(fields: Dict[str, object]) -> str:
    try:
        return json.dumps(fields, sort_keys=True, default=str)[:400]
    except (TypeError, ValueError):
        return str(fields)[:400]


def _system_from_origin(origin: str) -> str:
    from ..models import SYSTEM_CATEGORY_MAP

    if origin in SYSTEM_CATEGORY_MAP:
        return origin
    return "support_platform"


def _parse_handover(text: str) -> List[RawRecord]:
    """Parse the shift-handover text export (``a | b | c | d | e`` rows)."""

    from ..identity import parse_ts_optional
    from ..models import CATEGORY_SYSTEM_MAP
    from .json_source import RESULT_TO_STATE, category_for

    records: List[RawRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        fields = [part.strip() for part in line[2:].split("|")]
        if len(fields) < 6:
            continue
        timestamp, system, resource, record_type, result, subject = fields[:6]
        if parse_ts_optional(timestamp) is None:
            continue
        category = category_for(record_type)
        if not category:
            continue
        state = RESULT_TO_STATE.get(result.upper())
        if state is None:
            continue
        from ..models import RESTORE_ALLOWED

        if result.upper() == "ACTIVE":
            if category == "BACKUP_ARCHIVE":
                state = RESTORE_ALLOWED
            elif category == "DATA_FLOW":
                state = "PRESENT"
        payload = {
            "subject_reference": subject,
            "email": subject if "@" in subject else None,
            "result": result,
            "record_type": record_type,
        }
        records.append(
            RawRecord(
                system or CATEGORY_SYSTEM_MAP.get(category, ""),
                resource,
                category,
                state,
                payload,
                timestamp,
            )
        )
    return records
