"""Step planning and JSON serialisation.

Findings, remediation and verification are emitted in the exact order required
by the problem statement:

* phase order ``FINDING`` -> ``REMEDIATION`` -> ``VERIFICATION``;
* inside a phase, the operation order of the allowed-operations table;
* then ``system_id`` and ``resource_id`` ascending.

The planning rules are derived from the sample answers and the stated contract:

``COMPLETE``
    Only verification steps, each citing the evidence that proves the state.
``INCOMPLETE``
    Findings with evidence, a remediation step per finding with an empty
    ``evidence_id``, then the checks that must pass afterwards (also empty
    ``evidence_id`` because they describe future work).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import (
    ABSENT,
    ANONYMIZED,
    BLOCKED,
    DELETE,
    DISABLED,
    OPERATION_ORDER,
    OPERATION_STATE,
    PRESENT,
    REDACT,
    REDACTED,
    RESTORE_ALLOWED,
    RETAIN,
    CaseResult,
    DeletionRequest,
    ResourceState,
    Step,
    UserIdentity,
)
from .policy import DeletionPolicy

FINDING_FOR_VIOLATION = {
    "REMAINING_RECORD": "REPORT_REMAINING_RECORD",
    "EXPOSED_PII": "REPORT_EXPOSED_PII",
    "ACTIVE_DATA_FLOW": "REPORT_ACTIVE_DATA_FLOW",
    "RESTORE_RISK": "REPORT_RESTORE_RISK",
}

REMEDIATION_FOR_FINDING = {
    "REPORT_REMAINING_RECORD": "DELETE_RECORD",
    "REPORT_EXPOSED_PII": None,  # depends on the policy action
    "REPORT_ACTIVE_DATA_FLOW": "DISABLE_DATA_FLOW",
    "REPORT_RESTORE_RISK": "BLOCK_RESTORE",
}

VERIFICATION_FOR_STATE = {
    ABSENT: "VERIFY_RECORD_ABSENT",
    PRESENT: "VERIFY_RECORD_PRESENT",
    REDACTED: "VERIFY_RECORD_REDACTED",
    ANONYMIZED: "VERIFY_RECORD_ANONYMIZED",
    DISABLED: "VERIFY_DATA_FLOW_DISABLED",
    BLOCKED: "VERIFY_RESTORE_BLOCKED",
}

VERIFICATION_AFTER_REMEDIATION = {
    "DELETE_RECORD": "VERIFY_RECORD_ABSENT",
    "REDACT_RECORD": "VERIFY_RECORD_REDACTED",
    "ANONYMIZE_RECORD": "VERIFY_RECORD_ANONYMIZED",
    "DISABLE_DATA_FLOW": "VERIFY_DATA_FLOW_DISABLED",
    "BLOCK_RESTORE": "VERIFY_RESTORE_BLOCKED",
}


@dataclass
class _Draft:
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str

    def sort_key(self) -> Tuple[int, int, str, str]:
        return (
            0 if self.phase == "FINDING" else 1 if self.phase == "REMEDIATION" else 2,
            OPERATION_ORDER.get(self.operation, 99),
            self.system_id,
            self.resource_id,
        )


class CasePlanner:
    def __init__(self, policy: DeletionPolicy):
        self.policy = policy

    # ------------------------------------------------------------------
    def build(
        self,
        request: DeletionRequest,
        identity: UserIdentity,
        states: Dict[Tuple[str, str], ResourceState],
        policy: DeletionPolicy,
    ) -> CaseResult:
        violations = self._violations(states)
        if not violations:
            steps = self._complete_steps(states)
            return CaseResult(
                case_id=request.case_id,
                deletion_status="COMPLETE",
                steps=steps,
                notes=["all relevant records satisfy policy"],
            )

        steps = self._incomplete_steps(states, violations)
        return CaseResult(
            case_id=request.case_id,
            deletion_status="INCOMPLETE",
            steps=steps,
            notes=[f"{len(violations)} policy violation(s)"],
        )

    # ------------------------------------------------------------------
    def _violations(
        self, states: Dict[Tuple[str, str], ResourceState]
    ) -> List[Tuple[ResourceState, str]]:
        violations: List[Tuple[ResourceState, str]] = []
        for state in states.values():
            category = state.category
            if category == "DATA_FLOW":
                if state.state not in (DISABLED, ABSENT):
                    violations.append((state, "ACTIVE_DATA_FLOW"))
                continue
            if category == "BACKUP_ARCHIVE":
                if state.state in (RESTORE_ALLOWED, PRESENT):
                    violations.append((state, "RESTORE_RISK"))
                continue
            if self.policy.is_compliant(category, state.state):
                continue
            verb = "EXPOSED_PII" if self.policy.present_but_exposed(category, state.state) else "REMAINING_RECORD"
            violations.append((state, verb))
        violations.sort(key=lambda item: (item[0].system_id, item[0].resource_id))
        return violations

    # ------------------------------------------------------------------
    def _complete_steps(
        self, states: Dict[Tuple[str, str], ResourceState]
    ) -> List[Step]:
        drafts: List[_Draft] = []
        for state in states.values():
            if not state.evidence_id:
                # A complete deletion is proved by evidence; a state that no
                # supplied file establishes cannot be a verification step.
                continue
            operation = self._verification_operation(state)
            if operation is None:
                continue
            drafts.append(
                _Draft(
                    phase="VERIFICATION",
                    system_id=state.system_id,
                    resource_id=state.resource_id,
                    operation=operation,
                    state=OPERATION_STATE[operation],
                    evidence_id=state.evidence_id,
                )
            )
        return _finalize(drafts)

    def _verification_operation(self, state: ResourceState) -> Optional[str]:
        category = state.category
        if category == "DATA_FLOW":
            return "VERIFY_DATA_FLOW_DISABLED" if state.state == DISABLED else None
        if category == "BACKUP_ARCHIVE":
            return "VERIFY_RESTORE_BLOCKED" if state.state == BLOCKED else None
        action = self.policy.action_for(category)
        if action == DELETE:
            # Only the primary account record is proved absent; secondary copies
            # that were successfully removed are not reported as checklist
            # items in the reference answers.
            if category == "USER_PROFILE" and state.state == ABSENT:
                return "VERIFY_RECORD_ABSENT"
            return None
        if action == RETAIN:
            return "VERIFY_RECORD_PRESENT" if state.state == PRESENT else None
        if action == REDACT:
            return "VERIFY_RECORD_REDACTED" if state.state == REDACTED else None
        if action == "ANONYMIZE":
            return "VERIFY_RECORD_ANONYMIZED" if state.state == ANONYMIZED else None
        return None

    # ------------------------------------------------------------------
    def _incomplete_steps(
        self,
        states: Dict[Tuple[str, str], ResourceState],
        violations: Sequence[Tuple[ResourceState, str]],
    ) -> List[Step]:
        drafts: List[_Draft] = []

        for state, kind in violations:
            operation = FINDING_FOR_VIOLATION[kind]
            drafts.append(
                _Draft(
                    phase="FINDING",
                    system_id=state.system_id,
                    resource_id=state.resource_id,
                    operation=operation,
                    state=OPERATION_STATE[operation],
                    evidence_id=state.evidence_id,
                )
            )

        for state, kind in violations:
            finding = FINDING_FOR_VIOLATION[kind]
            remediation = REMEDIATION_FOR_FINDING[finding]
            if remediation is None:
                action = self.policy.action_for(state.category)
                remediation = "REDACT_RECORD" if action == REDACT else "ANONYMIZE_RECORD"
            drafts.append(
                _Draft(
                    phase="REMEDIATION",
                    system_id=state.system_id,
                    resource_id=state.resource_id,
                    operation=remediation,
                    state=OPERATION_STATE[remediation],
                    evidence_id="",
                )
            )

        seen: Set[Tuple[str, str, str]] = set()
        for state in states.values():
            operation = self._post_verification(state, violations)
            if operation is None:
                continue
            key = (state.system_id, state.resource_id, operation)
            if key in seen:
                continue
            seen.add(key)
            drafts.append(
                _Draft(
                    phase="VERIFICATION",
                    system_id=state.system_id,
                    resource_id=state.resource_id,
                    operation=operation,
                    state=OPERATION_STATE[operation],
                    evidence_id="",
                )
            )

        return _finalize(drafts)

    def _post_verification(
        self,
        state: ResourceState,
        violations: Sequence[Tuple[ResourceState, str]],
    ) -> Optional[str]:
        category = state.category
        if category == "DATA_FLOW":
            return "VERIFY_DATA_FLOW_DISABLED" if state.state != DISABLED else None
        if category == "BACKUP_ARCHIVE":
            return "VERIFY_RESTORE_BLOCKED" if state.state != BLOCKED else None
        action = self.policy.action_for(category)
        if category == "USER_PROFILE":
            return "VERIFY_RECORD_ABSENT"
        if action == DELETE:
            # Secondary copies become checklist items only when remediation was
            # required for them.
            for other, _kind in violations:
                if other.key == state.key:
                    return "VERIFY_RECORD_ABSENT"
            return None
        if action == RETAIN:
            return "VERIFY_RECORD_PRESENT" if state.state == PRESENT else None
        if action == REDACT:
            return "VERIFY_RECORD_REDACTED"
        if action == "ANONYMIZE":
            return "VERIFY_RECORD_ANONYMIZED"
        return None


def _finalize(drafts: List[_Draft]) -> List[Step]:
    drafts.sort(key=lambda draft: draft.sort_key())
    return [
        Step(
            step_no=index + 1,
            phase=draft.phase,
            system_id=draft.system_id,
            resource_id=draft.resource_id,
            operation=draft.operation,
            state=draft.state,
            evidence_id=draft.evidence_id,
        )
        for index, draft in enumerate(drafts)
    ]


def steps_to_json(steps: Sequence[Step]) -> str:
    """Serialise steps deterministically as a compact JSON array."""

    payload = [step.as_dict() for step in steps]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
