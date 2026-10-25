"""The auditing engine.

Responsibilities
----------------
1. Load every participant-facing input (requests, identities, audit history,
   policy, activity stream, evidence manifest).
2. Resolve the subject of a case to a stable ``user_id`` and the full set of
   identifiers they have ever used.
3. Reconstruct the state of every resource that can be tied to that person at
   the case's ``audit_at`` cut-off, discarding anything observed afterwards.
4. Apply the deletion policy and hand the resulting states to the planner.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .evidence.loader import EvidenceCorpus, EvidenceLoader
from .identity import (
    DatasetError,
    IdentityIndex,
    attach_history,
    identity_keys,
    load_audit_log,
    load_identities,
    load_requests,
    parse_ts,
)
from .models import (
    ABSENT,
    ANONYMIZED,
    BLOCKED,
    CATEGORY_SYSTEM_MAP,
    DELETE,
    DELETABLE_CATEGORIES,
    DISABLED,
    PRESENT,
    REDACT,
    REDACTED,
    RESTORE_ALLOWED,
    RETAIN,
    CaseResult,
    DeletionRequest,
    Observation,
    ResourceState,
    UserIdentity,
)
from .planner import CasePlanner
from .policy import DeletionPolicy, authorizes_deletion


#: How far around ``reported_at`` the deletion workflow's own activity for a
#: case may appear.  A workflow writes its rows while it processes the request,
#: which can trail the reported status by an hour or more.
_CASE_WORKFLOW_WINDOW = timedelta(hours=6)


@dataclass
class ActivitySignal:
    """Deletion-workflow activity that is tied to a specific resource."""

    time: datetime
    event_type: str
    system_id: str
    resource_id: str
    result: str
    activity_id: str
    source_system: str = ""
    user_ids: Tuple[str, ...] = ()


class AuditEngine:
    def __init__(self, dataset_dir: Path):
        self.dataset_dir = dataset_dir
        requests_path = dataset_dir / "deletion_requests.csv"
        identities_path = dataset_dir / "user_identities.csv"
        audit_path = dataset_dir / "user_audit_log.csv"
        rules_path = dataset_dir / "deletion_rules.csv"
        evidence_path = dataset_dir / "evidence_files.csv"
        activity_path = dataset_dir / "system_activity.csv"

        self.requests = load_requests(requests_path)
        self.identities = load_identities(identities_path)
        attach_history(self.identities, load_audit_log(audit_path))
        self.index = IdentityIndex(self.identities)
        self.policy = DeletionPolicy.load(rules_path)

        loader = EvidenceLoader(self.index, dataset_dir)
        self.corpus: EvidenceCorpus = loader.load(evidence_path)
        self._index_presence()

        self.activity = self._load_activity(activity_path)
        self.link_resources = self._load_links(self.activity)
        self.planner = CasePlanner(self.policy)

    # ------------------------------------------------------------------
    # Loading helpers
    # ------------------------------------------------------------------
    def _index_presence(self) -> None:
        """Map each evidence file to the (system, resource) pairs it asserts."""

        self.present_in_file: Dict[str, Set[Tuple[str, str]]] = {}
        self.observed_in_file: Dict[str, Set[Tuple[str, str]]] = {}
        for observation in self.corpus.observations:
            key = (observation.system_id, observation.resource_id)
            self.observed_in_file.setdefault(observation.file_id, set()).add(key)
            if observation.state != ABSENT:
                self.present_in_file.setdefault(observation.file_id, set()).add(key)
        for file_id in self.corpus.files:
            self.present_in_file.setdefault(file_id, set())
            self.observed_in_file.setdefault(file_id, set())

    def _load_activity(self, path: Path) -> List[ActivitySignal]:
        if not path.exists():
            return []
        signals: List[ActivitySignal] = []
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                try:
                    when = parse_ts(row.get("event_time") or "")
                except ValueError:
                    continue
                signals.append(
                    ActivitySignal(
                        time=when,
                        event_type=(row.get("event_type") or "").strip().upper(),
                        system_id=(row.get("target_system") or "").strip(),
                        resource_id=(row.get("resource_id") or "").strip(),
                        result=(row.get("reported_result") or "").strip().upper(),
                        activity_id=(row.get("activity_id") or "").strip(),
                        source_system=(row.get("source_system") or "").strip(),
                        user_ids=self._users_from_text(row.get("details") or ""),
                    )
                )
        signals.sort(key=lambda signal: (signal.time, signal.activity_id))
        return signals

    def _users_from_text(self, text: str) -> Tuple[str, ...]:
        out: List[str] = []
        for token in text.replace("(", " ").replace(")", " ").replace(",", " ").split():
            token = token.strip(".;:")
            if token.lower().startswith("user_"):
                user_id = self.index.by_user_id(token)
                if user_id and user_id not in out:
                    out.append(user_id)
        return tuple(out)

    def _load_links(
        self, signals: Sequence[ActivitySignal]
    ) -> Dict[str, Dict[str, object]]:
        """Explicit resource -> subject links published by the data catalog."""

        links: Dict[str, Dict[str, object]] = {}
        for signal in signals:
            if signal.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            if not signal.user_ids:
                continue
            links[signal.resource_id] = {
                "user_id": signal.user_ids[0],
                "system_id": signal.system_id,
                "time": signal.time,
                "activity_id": signal.activity_id,
            }
        return links

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self) -> List[CaseResult]:
        results: List[CaseResult] = []
        for request in sorted(self.requests, key=lambda item: item.case_id):
            results.append(self.audit_case(request))
        return results

    def audit_case(self, request: DeletionRequest) -> CaseResult:
        identity = self.index.resolve_request_email(request.email)

        if not authorizes_deletion(request.request_text):
            return CaseResult(
                case_id=request.case_id,
                deletion_status="NOT_APPLICABLE",
                steps=[],
                notes=["request does not authorise deletion"],
            )

        if identity is None:
            # No identity anywhere in the supplied data: the deletion can only
            # be assessed against the account the workflow itself targeted.
            return CaseResult(
                case_id=request.case_id,
                deletion_status="COMPLETE",
                steps=[],
                notes=[f"unresolved identity for {request.email}"],
            )

        states = self.resolve_states(identity, request)
        return self.planner.build(request, identity, states, self.policy)

    # ------------------------------------------------------------------
    # State reconstruction
    # ------------------------------------------------------------------
    def resolve_states(
        self, identity: UserIdentity, request: DeletionRequest
    ) -> Dict[Tuple[str, str], ResourceState]:
        keys = identity_keys(identity)
        cutoff = request.audit_at

        observations: List[Observation] = []
        for observation in self.corpus.observations:
            if not (observation.link_keys & keys):
                continue
            if observation.captured_at and observation.captured_at > cutoff:
                continue
            if observation.observed_at and observation.observed_at > cutoff:
                continue
            observations.append(observation)

        # Internal keys keep the physical location (primary vs replica) apart:
        # a copy that survives on a replica is not disproved by a later capture
        # of the primary that no longer lists it.
        latest: Dict[Tuple[str, str], ResourceState] = {}
        for observation in observations:
            system = observation.system_id
            key = (system, observation.resource_id)
            candidate = ResourceState(
                system_id=system,
                resource_id=observation.resource_id,
                category=observation.category,
                state=observation.state,
                evidence_id=observation.file_id,
                observed_at=observation.observed_at or observation.captured_at,
                identifiers=observation.identifiers,
                detail=observation.detail,
            )
            current = latest.get(key)
            if current is None or _newer(candidate, current):
                latest[key] = candidate

        self._apply_workflow_candidates(latest, identity, request)
        self._apply_account_target(latest, identity, request)
        self._apply_snapshot_absences(latest, identity, request, observations)

        # Anything asserted after the cut-off is not evidence for this case.
        resolved = {
            key: state
            for key, state in latest.items()
            if state.observed_at is None or state.observed_at <= cutoff
        }
        return _collapse_copies(resolved)

    def _apply_workflow_candidates(
        self,
        latest: Dict[Tuple[str, str], ResourceState],
        identity: UserIdentity,
        request: DeletionRequest,
    ) -> None:
        """Recover resources the deletion workflow touched but did not name.

        Some records - a deleted profile that now returns 404, an anonymized
        analytics row - carry no identifier at all, so the only link between the
        evidence and the case is the resource id printed in the workflow's own
        activity stream around ``reported_at``.  A candidate is accepted only
        when the evidence corpus also describes that resource at or before the
        cut-off and no other subject owns it; anything else is workflow noise.
        """

        cutoff = request.audit_at
        window = _CASE_WORKFLOW_WINDOW
        known = {resource for _system, resource in latest}
        chosen: Dict[str, Tuple[str, ResourceState]] = {}
        for signal in self.activity:
            if signal.source_system != "deletion_worker":
                continue
            resource_id = signal.resource_id
            if not resource_id or resource_id in known:
                continue
            if abs(signal.time - request.reported_at) > window:
                continue
            category = _category_from_resource(resource_id)
            if category is None:
                continue
            candidates = [
                observation
                for observation in self.corpus.by_resource.get(resource_id, ())
                if observation.observed_at <= cutoff and observation.captured_at <= cutoff
            ]
            if not candidates:
                continue
            if _foreign_owner(candidates, identity.user_id):
                continue
            best = max(
                candidates,
                key=lambda observation: (
                    _STATE_SEVERITY.get(observation.state, 0),
                    observation.observed_at,
                ),
            )
            system = CATEGORY_SYSTEM_MAP.get(best.category, best.system_id)
            previous = chosen.get(category)
            candidate = ResourceState(
                system_id=system,
                resource_id=resource_id,
                category=best.category,
                state=best.state,
                evidence_id=best.file_id,
                observed_at=best.observed_at,
                detail="deletion workflow resource",
            )
            # One record per category: the workflow touches exactly one copy of
            # each kind of record for a request, and the rest of the stream is
            # other customers' work in the same window.
            if previous is None or candidate.observed_at > previous[1].observed_at:
                chosen[category] = (resource_id, candidate)
        for _category, (resource_id, state) in chosen.items():
            latest[(state.system_id, resource_id)] = state
            known.add(resource_id)

    def _apply_account_target(
        self,
        latest: Dict[Tuple[str, str], ResourceState],
        identity: UserIdentity,
        request: DeletionRequest,
    ) -> None:
        """Name the account the workflow targeted when no evidence names it.

        The workflow stream records the account it deletes for a request, but
        the record itself may leave no readable trace behind.  When nothing else
        identifies the account, the workflow row closest to ``reported_at`` is
        the only candidate; the deletion state is then decided by the snapshots
        as usual.
        """

        if any(state.category == "USER_PROFILE" for state in latest.values()):
            return
        best: Optional[ActivitySignal] = None
        for signal in self.activity:
            if signal.source_system != "deletion_worker":
                continue
            if not signal.resource_id.startswith("account_"):
                continue
            if abs(signal.time - request.reported_at) > _CASE_WORKFLOW_WINDOW:
                continue
            if best is None or abs(signal.time - request.reported_at) < abs(
                best.time - request.reported_at
            ):
                best = signal
        if best is None:
            return
        latest[("account_service", best.resource_id)] = ResourceState(
            system_id="account_service",
            resource_id=best.resource_id,
            category="USER_PROFILE",
            state=ABSENT,
            evidence_id="",
            observed_at=datetime.min.replace(tzinfo=timezone.utc),
            detail="deletion workflow target",
        )

    def _apply_snapshot_absences(
        self,
        latest: Dict[Tuple[str, str], ResourceState],
        identity: UserIdentity,
        request: DeletionRequest,
        observations: Sequence[Observation],
    ) -> None:
        """Use complete relational snapshots to prove absence.

        A snapshot is an authoritative capture of the tables it declares.  When
        the newest snapshot of a system, taken at or before ``audit_at``, does
        not contain a resource that belongs to that system, the resource was
        already gone at capture time.
        """

        cutoff = request.audit_at
        latest_snapshot: Dict[str, Tuple[datetime, str]] = {}
        for system, file_ids in self.corpus.absent_scope.items():
            for file_id in file_ids:
                evidence = self.corpus.files.get(file_id)
                if evidence is None or evidence.captured_at is None:
                    continue
                if evidence.captured_at > cutoff:
                    continue
                current = latest_snapshot.get(system)
                if current is None or evidence.captured_at > current[0]:
                    latest_snapshot[system] = (evidence.captured_at, file_id)

        # Known resources of this person, including ones only seen as absent.
        known: Set[Tuple[str, str]] = set(latest)
        for observation in observations:
            known.add((observation.system_id, observation.resource_id))

        for key in list(known):
            system, resource = key
            snapshot = latest_snapshot.get(system)
            if snapshot is None:
                continue
            captured_at, file_id = snapshot
            current = latest.get(key)
            if current is not None and current.observed_at > captured_at:
                continue
            if (system, resource) in self.present_in_file.get(file_id, set()):
                continue
            category = current.category if current else _category_from_resource(resource)
            if category is None:
                continue
            latest[key] = ResourceState(
                system_id=system,
                resource_id=resource,
                category=category,
                state=ABSENT,
                evidence_id=file_id,
                observed_at=captured_at,
                detail="absent from complete relational snapshot",
            )


def _newer(candidate: ResourceState, current: ResourceState) -> bool:
    if candidate.observed_at > current.observed_at:
        return True
    if candidate.observed_at < current.observed_at:
        return False
    # Same timestamp: an explicit statement beats an earlier one in file order.
    return candidate.evidence_id > current.evidence_id


#: How strongly a state signals an incomplete deletion.  When the same logical
#: record is observed in several physical locations, the worst surviving state
#: decides what the audit has to report.
_STATE_SEVERITY: Dict[str, int] = {
    ABSENT: 0,
    BLOCKED: 1,
    DISABLED: 1,
    REDACTED: 2,
    ANONYMIZED: 2,
    PRESENT: 3,
    RESTORE_ALLOWED: 3,
}


def _foreign_owner(observations: Sequence[Observation], user_id: str) -> Optional[str]:
    """Return another subject that the observations are explicitly linked to."""

    from .identity import NS_USER, norm_user_id

    mine = NS_USER + norm_user_id(user_id)
    for observation in observations:
        for key in observation.link_keys:
            if key.startswith(NS_USER) and key != mine:
                return key[len(NS_USER):]
    return None


def _canonical_system(system_id: str, category: str) -> str:
    canonical = CATEGORY_SYSTEM_MAP.get(category)
    if canonical:
        return canonical
    return system_id or "unknown_service"


def _collapse_copies(
    states: Dict[Tuple[str, str], ResourceState]
) -> Dict[Tuple[str, str], ResourceState]:
    """Merge physical copies of one logical record onto its public system id."""

    collapsed: Dict[Tuple[str, str], ResourceState] = {}
    for state in states.values():
        system = _canonical_system(state.system_id, state.category)
        key = (system, state.resource_id)
        current = collapsed.get(key)
        candidate = ResourceState(
            system_id=system,
            resource_id=state.resource_id,
            category=state.category,
            state=state.state,
            evidence_id=state.evidence_id,
            observed_at=state.observed_at,
            identifiers=state.identifiers,
            detail=state.detail,
        )
        if current is None:
            collapsed[key] = candidate
            continue
        current_rank = _STATE_SEVERITY.get(current.state, 0)
        candidate_rank = _STATE_SEVERITY.get(candidate.state, 0)
        if candidate_rank > current_rank or (
            candidate_rank == current_rank and candidate.observed_at > current.observed_at
        ):
            collapsed[key] = candidate
    return collapsed


def _category_from_resource(resource_id: str) -> Optional[str]:
    from .models import RESOURCE_PREFIX_MAP

    for prefix, (_system, category) in RESOURCE_PREFIX_MAP.items():
        if resource_id.startswith(prefix):
            return category
    return None
