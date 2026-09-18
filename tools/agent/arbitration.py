"""Shared pure arbitration helpers (no policy/providers dependency).

These functions are the single copy of the selection, rejection and
reconciliation semantics shared by live control and offline evaluation
(``doc/agent-reflex-upgrade-plan.md`` sections 3.4-3.5 and 6.1).  They are
pure: nothing here writes a wire, mutates gameplay memory or knows about a
provider.  The live controller and ``evaluate.py`` both call them so the two
cannot drift (reviewer gate 6).

This module imports only ``candidates`` (itself a neutral leaf) and the
standard library, so it can never create a ``policy``/``providers`` cycle.
"""

import math
from dataclasses import dataclass, replace
from typing import Optional, Sequence, Tuple

from . import candidates
from .candidates import ActionCandidate, CandidateTable, ImmutableAction

# Raw-choice rejection reasons.  ``stale`` is a whole-context mismatch: the
# caller must discard the choice and prepare for the actual new need rather
# than send an old fallback (6.1).
REJECTION_CODES = (
    "stale",            # request/need-key/table identity does not match
    "index-type",       # index is not a non-bool integer
    "index-range",      # index outside the retained table
    "confidence",       # non-finite or below the acceptance threshold
    "rejected-member",  # the chosen member is already excluded
    "unsafe",           # membership fails the current safety contract
    "parse",            # the provider payload could not be parsed
    "abstain",          # the provider deliberately abstained
)

DEFAULT_CONFIDENCE_THRESHOLD = 0.8


class RejectionSet(object):
    """Controller-owned, versioned rejection set for one NeedKey.

    Retains both candidate IDs and canonical action signatures, so an
    equivalent action renamed or relabelled -- or re-emitted in a later,
    rebuilt table version -- is still excluded (3.5).  ``incomplete`` is the
    sole delivery exception: it terminates that wire send's lifecycle as a
    delivery repair without excluding the candidate from gameplay.
    """

    def __init__(self) -> None:
        self.ids = set()
        self.signatures = set()
        self.version = 0

    def exclude(self, candidate: ActionCandidate) -> None:
        """Terminally exclude one candidate's ID and canonical action."""
        self.ids.add(candidate.candidate_id)
        self.signatures.add(candidate.action_signature)
        self.version += 1

    def excludes_signature(self, signature: str) -> bool:
        return signature in self.signatures

    def excludes(self, candidate: ActionCandidate) -> bool:
        """True when the ID *or* the canonical action is already excluded."""
        return (candidate.candidate_id in self.ids
                or candidate.action_signature in self.signatures)

    def __len__(self) -> int:
        return len(self.ids)


@dataclass(frozen=True)
class RejectionDecision(object):
    """The lifecycle outcome of one wire ``invalid`` record."""

    code: str
    gameplay: bool        # True: terminally exclude; False: delivery repair
    repair: bool          # True only for the ``incomplete`` repair path


def classify_invalid(code: str,
                     candidate: Optional[ActionCandidate]
                     ) -> RejectionDecision:
    """Classify a wire ``invalid`` for an in-flight candidate (3.5).

    Every ordinary code terminally excludes the candidate; ``incomplete`` is
    transport repair only and does **not** mark it gameplay-rejected, so a
    new sent ordinal is allowed after the page obligation is repaired.
    """
    if code == "incomplete":
        return RejectionDecision(code=code, gameplay=False, repair=True)
    return RejectionDecision(code=code, gameplay=True, repair=False)


def select_retained(table: CandidateTable,
                    rejected: RejectionSet) -> Optional[ActionCandidate]:
    """The retained argmax among eligible, unrejected members (3.5).

    The table is already ordered, so this is the first member whose ID and
    canonical action are both unrejected -- the same winner the retry would
    send, never a recomputation that could pick a different member.
    """
    for cand in table.ordered_candidates:
        if not rejected.excludes(cand):
            return cand
    return None


@dataclass(frozen=True)
class RawChoice(object):
    """A provider's *raw* answer, before any mapping (6.1).

    The provider never returns a mapped action: it returns a raw index or an
    abstention, with the identity and usage the controller must validate.
    """

    table_id: str = ""
    need_key: tuple = ()
    table_version: int = -1
    index: Optional[int] = None
    confidence: Optional[float] = None
    abstain: bool = False
    parse_error: str = ""
    usage: tuple = ()
    latency: float = 0.0
    dispatched: bool = False


@dataclass(frozen=True)
class ChoiceOutcome(object):
    """The validated result of one raw choice.

    ``accepted`` is False for every rejection; ``candidate`` is set only on
    acceptance.  ``billable`` and ``stale`` are advisory so the caller bills
    returned usage exactly once even for a rejected choice, and treats a
    stale whole context as discard-and-reprepare (6.1).
    """

    accepted: bool
    candidate: Optional[ActionCandidate] = None
    reason: str = ""
    code: str = ""


def validate_raw_choice(table: CandidateTable, raw: RawChoice,
                        rejected: RejectionSet,
                        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                        eligible=None) -> ChoiceOutcome:
    """Validate one raw choice against the retained table (3.4/6.1).

    Checks, in order: exact request/table identity, parse, deliberate
    abstention, non-bool integer index in bounds, finite confidence in
    ``[0, 1]`` meeting *threshold*, and unrejected membership.  *eligible* is
    an optional predicate ``candidate -> bool`` for the current safety
    contract; a member that fails it is rejected as ``unsafe``.  Nothing is
    mutated here.
    """
    if raw.parse_error:
        return ChoiceOutcome(False, reason="parse: %s" % raw.parse_error,
                             code="parse")
    if raw.abstain:
        return ChoiceOutcome(False, reason="provider abstained",
                             code="abstain")
    # identity must match exactly: a stale table/need/version is never sent
    if (raw.table_id != table.table_id
            or tuple(raw.need_key) != tuple(table.need_key)
            or raw.table_version != table.table_version):
        return ChoiceOutcome(
            False, reason="stale request/table identity", code="stale")
    index = raw.index
    if isinstance(index, bool) or not isinstance(index, int):
        return ChoiceOutcome(False, reason="index is not an integer",
                             code="index-type")
    if not (0 <= index < len(table.ordered_candidates)):
        return ChoiceOutcome(False, reason="index %r out of range" % (index,),
                             code="index-range")
    conf = raw.confidence
    if (isinstance(conf, bool) or not isinstance(conf, (int, float))
            or not math.isfinite(conf) or not (0.0 <= conf <= 1.0)):
        return ChoiceOutcome(False, reason="confidence is not a finite "
                             "probability", code="confidence")
    if conf < threshold:
        return ChoiceOutcome(False, reason="confidence %.3f below %.3f"
                             % (conf, threshold), code="confidence")
    cand = table.ordered_candidates[index]
    if rejected.excludes(cand):
        return ChoiceOutcome(False, reason="member already rejected",
                             code="rejected-member")
    if eligible is not None and not eligible(cand):
        return ChoiceOutcome(False, reason="member is not currently safe",
                             code="unsafe")
    return ChoiceOutcome(True, candidate=cand)


# -- reconciliation -------------------------------------------------------

@dataclass(frozen=True)
class Reconciliation(object):
    """The transition/hero/effect evidence produced by one attempt (3.4).

    A reconciliation is *evidence*, not a commit: the controller decides when
    to commit it exactly once.  ``outcome`` is one of :data:`TERMINAL_STATES`,
    and when it is ``observed`` the ``observed_kind`` names the sub-category.
    """

    attempt_key: tuple
    outcome: str
    observed_kind: str = ""
    moved: bool = False
    time_advanced: bool = False
    transition_token: str = ""
    hero_possible: tuple = ()
    hero_confirmed: tuple = ()
    effect_deltas: tuple = ()
    notes: str = ""


def classify_outcome(same_position: bool, hero_resolved: bool,
                     time_delta: Optional[int]) -> Tuple[str, str]:
    """Pure outcome classification from public evidence (3.4).

    Returns ``(terminal_state, observed_kind)``.  An unresolved hero or a
    negative time delta is ``unknown`` -- an outcome classification, never an
    excuse to leave an attempt live.  Movement requires a resolved hero
    *and* a changed position.
    """
    if not hero_resolved:
        return ("observed", "unknown")
    if time_delta is None:
        return ("observed", "unknown")
    if not same_position:
        return ("observed", "moved")
    if time_delta > 0:
        return ("observed", "stationary-time-advanced")
    if time_delta == 0:
        return ("observed", "no-time")
    return ("observed", "unknown")


# -- shared detectors (live control and evaluation use one copy) ------------

#: The allowlisted, source-derived arrival outcomes (plan 4.1 ``O``).  Only a
#: current public arrival outcome counts; quoted/look/history text never
#: becomes authoritative.
ARRIVAL_PHRASES = ("you materialize", "you fall", "you are now on level",
                   "you climb down", "you descend")


def arrival_outcome(messages) -> bool:
    """True for a public arrival outcome among *messages* (4.1 ``O``)."""
    for entry in messages or ():
        if isinstance(entry, (tuple, list)) and len(entry) > 1:
            text = entry[1]
        else:
            text = entry
        low = (text or "").lower()
        if any(phrase in low for phrase in ARRIVAL_PHRASES):
            return True
    return False


def direction_delta(action, dir_keys) -> Optional[Tuple[int, int]]:
    """The grid delta of a plain movement-direction key action, else None.

    *dir_keys* is the ``{(dx, dy): wire_key}`` table (``protocol.DIR_KEYS``);
    it is a parameter so this module stays a dependency-neutral leaf.
    """
    if action is None or getattr(action, "tag", None) != "key":
        return None
    code = action.payload[0]
    for delta, key in dir_keys.items():
        if key == code:
            return delta
    return None


__all__ = [
    "REJECTION_CODES", "DEFAULT_CONFIDENCE_THRESHOLD", "RejectionSet",
    "RejectionDecision", "classify_invalid", "select_retained", "RawChoice",
    "ChoiceOutcome", "validate_raw_choice", "Reconciliation",
    "classify_outcome", "arrival_outcome", "direction_delta",
    "ARRIVAL_PHRASES",
]
