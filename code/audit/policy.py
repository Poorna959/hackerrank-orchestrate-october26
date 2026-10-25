"""Request applicability and deletion-policy evaluation.

Two policy layers live here:

* :func:`authorizes_deletion` decides whether the free-text request actually
  authorises erasure.  Access, correction, portability, marketing opt-out and
  account-maintenance requests are ``NOT_APPLICABLE`` even when the surrounding
  workflow treats them as deletion cases.
* :class:`DeletionPolicy` turns ``deletion_rules.csv`` into the required
  treatment for every data category and decides whether an observed resource
  state satisfies it.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .models import (
    ABSENT,
    ANONYMIZE,
    ANONYMIZED,
    BLOCKED,
    DEFAULT_CATEGORY_ACTION,
    DELETE,
    DELETABLE_CATEGORIES,
    DISABLED,
    PRESENT,
    REDACT,
    REDACTED,
    RESTORE_ALLOWED,
    RETAIN,
)

# ---------------------------------------------------------------------------
# Request applicability
# ---------------------------------------------------------------------------

#: Verbs/nouns that authorise erasure, across the languages present in the data.
_DELETION_TERMS = re.compile(
    r"delet|erasure|erase|remov|clos(e|ing) (my|the|this)|suppress|elimina|exclus"
    r"|excluir|apagar|borrar|cancell|cancela|supprim|löschen|loeschen|entfern"
    r"|lösch|verwijder|verwijderen|raderen|usuń|usun|usunięc|slett|slette|slet"
    r"|poista|删除|消除|註銷|注销|삭제|해지|削除|退会|حذف|مسح|إلغاء|удал|удалить"
    r"|διαγραφ|silin|kustuta|izbris|brisan|törl|töröl|sterge|șterg|şterge"
    r"|silme|kaldır|terminat|auflös|aufheb|deregist|desativ"
    # A follow-up that asks for confirmation that an erasure already happened is
    # still an erasure request; it must be audited, not rejected.
    r"|is gone|are gone|no longer exist|not identify me|cannot identify me"
    r"|de-identif|deidentif|still existed|remains? in the system",
    re.IGNORECASE,
)

#: Phrases that explicitly *withhold* deletion authorisation.
_NEGATION_TERMS = re.compile(
    r"not asking for deletion|not a (request|deletion) (to|for)|no deletion"
    r"|not (a )?(request )?to (erase|delete|remove)|do not (delete|close|erase|remove)"
    r"|don'?t (delete|close|erase|remove|change)|without (removing|deleting|closing)"
    r"|not (delete|close|erase)|leave (the|my) (account|profile|purchase)[^.]*?(active|place|as it is)"
    r"|keep (the|my) (account|profile)[^.]*?(as it is|active|in place)"
    r"|continue using|still (need|use|want)"
    r"|nicht löschen|ne pas supprimer|no eliminar|não excluir|no borrar|non cancellare"
    r"|niet verwijderen|не удал|삭제하지|削除しない|لا تحذف",
    re.IGNORECASE,
)

#: Non-deletion actions that are commonly mistaken for deletion requests.
_NON_DELETION_ACTION = re.compile(
    r"correct (it|the|my)|misspelled|rectif|update my (name|address|phone|number)"
    r"|change the email address|replace it with the new one|resetting the login"
    r"|reset(ting)? (my )?(password|login)|stop the promotional emails"
    r"|marketing lists only|from (your )?marketing|unsubscribe|opt[ -]?out"
    r"|download my account information|copy of (the|last year's|my) (information|invoices|data)"
    r"|access my (data|information)|what personal information you hold"
    r"|information request|portab|export my data",
    re.IGNORECASE,
)


def authorizes_deletion(request_text: str) -> bool:
    """Return ``True`` only when the request clearly asks for erasure."""

    text = (request_text or "").strip()
    if not text:
        return False
    if _NEGATION_TERMS.search(text):
        return False
    deletion = bool(_DELETION_TERMS.search(text))
    if not deletion:
        return False
    # "Please remove me from marketing lists only" is an opt-out, not deletion.
    if _NON_DELETION_ACTION.search(text) and not re.search(
        r"delet|erase|exclus|elimina|löschen|삭제|削除|delete", text, re.IGNORECASE
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Deletion rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    condition: str
    action: str


class DeletionPolicy:
    """Policy table plus per-state compliance decisions."""

    def __init__(self, rules: List[Rule]):
        self.rules = rules
        self.action_by_category: Dict[str, str] = dict(DEFAULT_CATEGORY_ACTION)
        for rule in rules:
            # Later rows for the same category refine the default.
            self.action_by_category[rule.category] = rule.action.upper()
        # DATA_FLOW is a process, not a record: it is controlled, not deleted.
        self.action_by_category.setdefault("DATA_FLOW", "CONTROL")

    @classmethod
    def load(cls, path: Optional[Path]) -> "DeletionPolicy":
        rules: List[Rule] = []
        if path is not None and path.exists():
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    category = (row.get("data_category") or "").strip().upper()
                    action = (row.get("required_action") or "").strip().upper()
                    if not category or not action:
                        continue
                    rules.append(
                        Rule(
                            rule_id=(row.get("rule_id") or "").strip(),
                            category=category,
                            condition=(row.get("condition") or "").strip(),
                            action=action,
                        )
                    )
        return cls(rules)

    # -- policy questions ------------------------------------------------
    def action_for(self, category: str) -> str:
        return self.action_by_category.get(category, DELETE)

    def is_compliant(self, category: str, state: str) -> bool:
        action = self.action_for(category)
        if category == "DATA_FLOW":
            return state == DISABLED
        if category == "BACKUP_ARCHIVE":
            return state in (BLOCKED, ABSENT)
        if action == DELETE:
            return state == ABSENT
        if action == REDACT:
            # Retention keeps the record; the identifying fields must be gone.
            return state in (REDACTED, ABSENT)
        if action == ANONYMIZE:
            return state in (ANONYMIZED, ABSENT)
        if action == RETAIN:
            return state == PRESENT
        return True

    def present_but_exposed(self, category: str, state: str) -> bool:
        """True when the record exists but still exposes required-removed data."""

        action = self.action_for(category)
        if action in (REDACT, ANONYMIZE):
            return state == PRESENT
        return False
