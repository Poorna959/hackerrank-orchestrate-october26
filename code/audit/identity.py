"""Dataset loading and identity resolution.

Identity resolution is the foundation of the audit: a single person can appear
under a current email/phone, an older email/phone recorded in
``user_audit_log.csv``, a SHA-256 email hash, and a stable internal ``user_id``.
Evidence records must be linked through *all* of those forms, otherwise
historical copies are silently missed.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import DeletionRequest, IdentityValue, UserIdentity


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_TS_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?\s*$"
)


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Raises ``ValueError`` for values that cannot be interpreted so malformed
    dataset timestamps are surfaced instead of silently ignored.
    """

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    match = _TS_RE.match(text)
    if not match:
        raise ValueError(f"unparseable timestamp: {value!r}")
    year, month, day, hour, minute, second, frac, tz = match.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    dt = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), micro
    )
    if tz in (None, "Z"):
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    tz = tz[1:].replace(":", "")
    offset_minutes = int(tz[:2]) * 60 + int(tz[2:4])
    from datetime import timedelta

    return (dt - sign * timedelta(minutes=offset_minutes)).replace(tzinfo=timezone.utc)


def parse_ts_optional(value: str) -> Optional[datetime]:
    try:
        return parse_ts(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Identifier normalisation
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def norm_email(value: str) -> str:
    return (value or "").strip().strip(".,;").lower()


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def norm_hash(value: str) -> str:
    return (value or "").strip().lower()


def norm_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", text.strip().lower())


def norm_user_id(value: str) -> str:
    return (value or "").strip().lower()


#: Identifier namespaces used to build link keys.  The namespace prefix keeps
#: e.g. a 10-digit phone number from colliding with a numeric user id.
NS_USER = "uid:"
NS_EMAIL = "email:"
NS_PHONE = "phone:"
NS_HASH = "hash:"
NS_NAME = "name:"


def identity_keys(identity: Optional[UserIdentity]) -> Set[str]:
    """Return every normalised link key that identifies this person."""

    if identity is None:
        return set()
    keys: Set[str] = set()
    if identity.user_id:
        keys.add(NS_USER + norm_user_id(identity.user_id))
    for email in identity.all_emails:
        keys.add(NS_EMAIL + norm_email(email))
    for phone in identity.all_phones:
        digits = norm_phone(phone)
        if len(digits) >= 7:
            keys.add(NS_PHONE + digits)
    for digest in identity.all_email_hashes:
        keys.add(NS_HASH + norm_hash(digest))
    for name in identity.all_names:
        normalized = norm_name(name)
        if normalized:
            keys.add(NS_NAME + normalized)
    return keys


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


class DatasetError(RuntimeError):
    """Raised when a required dataset file is missing or malformed."""


def _read_csv(path: Path, required: Sequence[str]) -> List[Dict[str, str]]:
    if not path.exists():
        raise DatasetError(f"required dataset file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [col for col in required if col not in header]
        if missing:
            raise DatasetError(
                f"{path.name} is missing required column(s): {', '.join(missing)}"
            )
        rows: List[Dict[str, str]] = []
        for raw in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            rows.append(row)
        return rows


def load_requests(path: Path) -> List[DeletionRequest]:
    rows = _read_csv(
        path,
        [
            "case_id",
            "email",
            "requested_at",
            "reported_status",
            "reported_at",
            "audit_at",
            "request_text",
        ],
    )
    requests: List[DeletionRequest] = []
    for row in rows:
        try:
            requests.append(
                DeletionRequest(
                    case_id=row["case_id"],
                    email=row["email"],
                    requested_at=parse_ts(row["requested_at"]),
                    reported_status=row["reported_status"],
                    reported_at=parse_ts(row["reported_at"]),
                    audit_at=parse_ts(row["audit_at"]),
                    request_text=row["request_text"],
                )
            )
        except ValueError as exc:
            raise DatasetError(f"{path.name}: {row.get('case_id')}: {exc}") from exc
    return requests


def load_identities(path: Path) -> Dict[str, UserIdentity]:
    rows = _read_csv(
        path, ["user_id", "name", "email", "phone", "email_hash", "updated_at"]
    )
    result: Dict[str, UserIdentity] = {}
    for row in rows:
        try:
            result[row["user_id"]] = UserIdentity(
                user_id=row["user_id"],
                name=row["name"],
                email=row["email"],
                phone=row["phone"],
                email_hash=row["email_hash"],
                updated_at=parse_ts(row["updated_at"]),
            )
        except ValueError as exc:
            raise DatasetError(f"{path.name}: {row.get('user_id')}: {exc}") from exc
    return result


def load_audit_log(path: Path) -> Dict[str, List[IdentityValue]]:
    rows = _read_csv(
        path, ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"]
    )
    history: Dict[str, List[IdentityValue]] = {}
    for row in rows:
        field = row["field_name"].strip().lower()
        if field not in {"email", "phone", "name", "email_hash"}:
            continue
        try:
            when = parse_ts(row["event_time"])
        except ValueError as exc:
            raise DatasetError(f"{path.name}: {row.get('audit_id')}: {exc}") from exc
        for value in (row["old_value"], row["new_value"]):
            value = (value or "").strip()
            if not value:
                continue
            history.setdefault(row["user_id"], []).append(
                IdentityValue(
                    field=field,
                    value=value,
                    effective_from=when,
                    audit_id=row["audit_id"],
                )
            )
    return history


def attach_history(
    identities: Dict[str, UserIdentity], history: Dict[str, List[IdentityValue]]
) -> None:
    """Replace each identity with a copy carrying its audit-log history."""

    for user_id, identity in list(identities.items()):
        values = tuple(history.get(user_id, ()))
        identities[user_id] = UserIdentity(
            user_id=identity.user_id,
            name=identity.name,
            email=identity.email,
            phone=identity.phone,
            email_hash=identity.email_hash,
            updated_at=identity.updated_at,
            historical=values,
        )


class IdentityIndex:
    """Maps any identifier form to the owning ``user_id``.

    Only unambiguous links are recorded.  A historical email that maps to two
    different people is dropped rather than guessed, which prevents unrelated
    customers from being merged.
    """

    def __init__(self, identities: Dict[str, UserIdentity]):
        self.identities = identities
        self._by_email: Dict[str, Optional[str]] = {}
        self._by_phone: Dict[str, Optional[str]] = {}
        self._by_hash: Dict[str, Optional[str]] = {}
        self._by_user: Dict[str, str] = {}
        self._by_name: Dict[str, Optional[str]] = {}

        for user_id, identity in identities.items():
            self._by_user[norm_user_id(user_id)] = user_id
            self._add(self._by_email, norm_email(identity.email), user_id)
            for email in identity.all_emails:
                self._add(self._by_email, norm_email(email), user_id)
            for phone in identity.all_phones:
                digits = norm_phone(phone)
                if len(digits) >= 7:
                    self._add(self._by_phone, digits, user_id)
            for digest in identity.all_email_hashes:
                self._add(self._by_hash, norm_hash(digest), user_id)
            for name in identity.all_names:
                normalized = norm_name(name)
                if normalized:
                    self._add(self._by_name, normalized, user_id)

    @staticmethod
    def _add(index: Dict[str, Optional[str]], key: str, user_id: str) -> None:
        if not key:
            return
        existing = index.get(key)
        if existing is None and key in index:
            return  # already known to be ambiguous
        if existing is not None and existing != user_id:
            index[key] = None  # ambiguous: never guess
            return
        index[key] = user_id

    # -- lookups ---------------------------------------------------------
    def by_user_id(self, value: str) -> Optional[str]:
        return self._by_user.get(norm_user_id(value))

    def by_email(self, value: str) -> Optional[str]:
        return self._by_email.get(norm_email(value))

    def by_phone(self, value: str) -> Optional[str]:
        digits = norm_phone(value)
        return self._by_phone.get(digits) if len(digits) >= 7 else None

    def by_hash(self, value: str) -> Optional[str]:
        return self._by_hash.get(norm_hash(value))

    def by_name(self, value: str) -> Optional[str]:
        return self._by_name.get(norm_name(value))

    def resolve_request_email(self, email: str) -> Optional[UserIdentity]:
        """Resolve the identity supplied on a request, current or historical."""

        user_id = self.by_email(email)
        if user_id:
            return self.identities.get(user_id)
        return None

    def keys_for(self, user_id: str) -> Set[str]:
        return identity_keys(self.identities.get(user_id))
