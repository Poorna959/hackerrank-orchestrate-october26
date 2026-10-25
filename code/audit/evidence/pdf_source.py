"""Dependency-free PDF text extraction for document evidence.

The supplied business documents are generated with a text-based PDF writer:
content streams are ASCII85 + Flate compressed and text is drawn with literal
``(...) Tj`` / ``[(...) ...] TJ`` operators over an embedded subset font whose
``ToUnicode`` map is the identity for the printable ASCII range.  That makes a
small, self-contained extractor sufficient - no third-party PDF library and no
external binaries are required at evaluation time.
"""

from __future__ import annotations

import base64
import re
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..models import (
    ABSENT,
    ANONYMIZED,
    BLOCKED,
    DISABLED,
    PRESENT,
    REDACTED,
    RESTORE_ALLOWED,
)
from .json_source import RawRecord, category_for
from .textparse import find_emails, find_hashes, find_resources, find_user_ids

_OBJ_RE = re.compile(rb"(\d+)\s+0\s+obj(.*?)endobj", re.S)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def _decode_stream(header: bytes, data: bytes) -> Optional[bytes]:
    try:
        if b"ASCII85Decode" in header:
            cleaned = data.strip()
            if not cleaned.startswith(b"<~"):
                cleaned = b"<~" + cleaned
            if not cleaned.rstrip().endswith(b"~>"):
                cleaned = cleaned.rstrip() + b"~>"
            data = base64.a85decode(cleaned, adobe=True)
        if b"ASCIIHexDecode" in header:
            cleaned = re.sub(rb"\s", b"", data).rstrip(b">")
            data = bytes.fromhex(cleaned.decode("ascii"))
        if b"FlateDecode" in header:
            data = zlib.decompress(data)
    except Exception:
        return None
    return data


def _split_stream(body: bytes):
    """Return (header, stream bytes) for an object body, or (None, None)."""

    index = body.find(b"stream")
    if index < 0:
        return None, None
    header = body[:index]
    rest = body[index + 6:]
    rest = rest.lstrip(b"\r\n")
    end = rest.rfind(b"endstream")
    if end < 0:
        return header, rest
    return header, rest[:end]


def _unescape_pdf_string(raw: bytes) -> str:
    out: List[str] = []
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index:index + 1]
        if char == b"\\" and index + 1 < length:
            nxt = raw[index + 1:index + 2]
            mapping = {
                b"n": b"\n",
                b"r": b"\r",
                b"t": b"\t",
                b"b": b"\b",
                b"f": b"\f",
                b"(": b"(",
                b")": b")",
                b"\\": b"\\",
            }
            if nxt in mapping:
                out.append(mapping[nxt].decode("latin-1"))
                index += 2
                continue
            octal = re.match(rb"[0-7]{1,3}", raw[index + 1:index + 4])
            if octal:
                out.append(chr(int(octal.group(0), 8)))
                index += 1 + len(octal.group(0))
                continue
            index += 2
            continue
        out.append(char.decode("latin-1"))
        index += 1
    return "".join(out)


_TEXT_OP = re.compile(rb"\((?:\\.|[^()\\])*\)\s*(?:Tj|'|\")|\[(?:[^\[\]]|\\.)*\]\s*TJ", re.S)
_STRING = re.compile(rb"\((?:\\.|[^()\\])*\)", re.S)


def extract_text(path: Path) -> str:
    """Return the readable text of a PDF, or an empty string."""

    data = path.read_bytes()
    chunks: List[str] = []
    for match in _OBJ_RE.finditer(data):
        header, stream = _split_stream(match.group(2))
        if header is None or stream is None:
            continue
        decoded = _decode_stream(header, stream)
        if not decoded or (b"Tj" not in decoded and b"TJ" not in decoded):
            continue
        pieces: List[str] = []
        for op in _TEXT_OP.finditer(decoded):
            for literal in _STRING.finditer(op.group(0)):
                pieces.append(_unescape_pdf_string(literal.group(0)[1:-1]))
            pieces.append("\n")
        chunks.append("".join(pieces))
    text = "\n".join(chunks)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


#: Status words a business document uses to describe a record.
_STATUS_WORDS: Tuple[Tuple[str, str], ...] = (
    ("ANONYMIZED", ANONYMIZED),
    ("ANONYMISED", ANONYMIZED),
    ("REDACTED", REDACTED),
    ("NOT_FOUND", ABSENT),
    ("DELETED", ABSENT),
    ("REMOVED", ABSENT),
    ("RESTORE BLOCKED", BLOCKED),
    ("BLOCKED", BLOCKED),
    ("RESTORE ALLOWED", RESTORE_ALLOWED),
    ("DISABLED", DISABLED),
)


def records_from_text(text: str, default_system: str) -> List[RawRecord]:
    """Turn document text into record statements.

    Business documents are dossiers: they list a resource id, a record type and
    whatever identifiers the record still holds.  A record is PRESENT unless the
    document explicitly says its identifiers were removed.
    """

    if not text.strip():
        return []
    resources = find_resources(text)
    if not resources:
        return []

    emails = find_emails(text)
    hashes = find_hashes(text)
    users = find_user_ids(text)
    identifiers = {"emails": emails, "hashes": hashes, "user_ids": users}
    lowered = text.lower()

    records: List[RawRecord] = []
    seen = set()
    for resource in resources:
        if resource in seen:
            continue
        seen.add(resource)
        category = _category_for_resource(resource)
        if category == "SUPPORT_MESSAGE" and re.search(
            r"attachment|attached|uploader|upload history|\bfile\b", lowered
        ):
            # A support artefact that is an uploaded document, not a
            # conversation, belongs only to the subject and must be removed.
            category = "SUPPORT_ATTACHMENT"
        state = PRESENT
        for word, mapped in _STATUS_WORDS:
            if word.lower() in lowered and _near(lowered, resource.lower(), word.lower()):
                state = mapped
                break
        if category == "ACCOUNT_TOMBSTONE":
            # A tombstone is compliant only when no direct identifier remains.
            state = PRESENT if (emails or hashes) else REDACTED
        fields: Dict[str, object] = dict(identifiers)
        fields["document_text"] = text
        records.append(
            RawRecord(default_system, resource, category, state, fields, None)
        )
    return records


def _near(text: str, resource: str, word: str, window: int = 220) -> bool:
    position = text.find(resource)
    if position < 0:
        return False
    return word in text[max(0, position - window): position + window]


def _category_for_resource(resource: str) -> str:
    from ..models import RESOURCE_PREFIX_MAP

    for prefix, (_system, category) in RESOURCE_PREFIX_MAP.items():
        if resource.startswith(prefix):
            return category
    return "UNKNOWN"
