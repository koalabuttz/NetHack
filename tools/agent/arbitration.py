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

#: The relative-acceptance rule: a Jev choice is accepted on concentration
#: alone when the *selected option's own validated probability* strictly
#: exceeds ``factor / N`` for the retained offered count ``N``.  The factor
#: must be greater than 1 so acceptance is above a uniform distribution, and
#: below 2 so a two-option choice remains attainable.
CONFIDENCE_RELATIVE = "relative"
CONFIDENCE_ABSOLUTE = "absolute"
CONFIDENCE_MODES = (CONFIDENCE_RELATIVE, CONFIDENCE_ABSOLUTE)
DEFAULT_RELATIVE_FACTOR = 1.5


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
    #: The selected option's own validated probability (the concentration the
    #: relative gate reads).  Populated by the parser only after the full
    #: response vector and its maximum have been validated; ``None`` means no
    #: trustworthy selected probability was supplied.
    selected_probability: Optional[float] = None
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
                        eligible=None,
                        mode: str = CONFIDENCE_RELATIVE,
                        factor: float = DEFAULT_RELATIVE_FACTOR
                        ) -> ChoiceOutcome:
    """Validate one raw choice against the retained table (3.4/6.1).

    Checks, in order: exact request/table identity, parse, deliberate
    abstention, non-bool integer index in bounds, the confidence/concentration
    gate, and unrejected membership.  *eligible* is an optional predicate
    ``candidate -> bool`` for the current safety contract; a member that fails
    it is rejected as ``unsafe``.  Nothing is mutated here.

    Confidence gate (plan section 2):

    * ``mode="relative"`` (the default) accepts on *concentration* only --
      the selected option's own validated ``selected_probability`` must be a
      finite probability in ``[0, 1]`` that strictly exceeds ``factor / N``
      for the retained offered count ``N``.  Missing/malformed selected
      probability fails closed; the unrelated service ``confidence`` scalar is
      never consulted.  A singleton table (``N < 2``) bypasses the comparison,
      but still requires a valid selected probability.
    * ``mode="absolute"`` is the explicit rollback: the legacy scalar
      ``confidence`` in ``[0, 1]`` must be at least *threshold* (inclusive).

    The rejection code is ``confidence`` in both modes.  A passing
    concentration test never bypasses the identity, membership, rejection or
    eligibility gates: safety stays independent of the acceptance rule.
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
    if mode == CONFIDENCE_ABSOLUTE:
        conf = raw.confidence
        if (isinstance(conf, bool) or not isinstance(conf, (int, float))
                or not math.isfinite(conf) or not (0.0 <= conf <= 1.0)):
            return ChoiceOutcome(False, reason="confidence is not a finite "
                                 "probability", code="confidence")
        if conf < threshold:
            return ChoiceOutcome(False, reason="confidence %.3f below %.3f"
                                 % (conf, threshold), code="confidence")
        ratio_note = ""
    else:
        prob = raw.selected_probability
        if (isinstance(prob, bool) or not isinstance(prob, (int, float))
                or not math.isfinite(prob) or not (0.0 <= prob <= 1.0)):
            return ChoiceOutcome(
                False, reason="confidence: selected probability is missing "
                "or not a finite probability", code="confidence")
        prob = float(prob)
        count = len(table.ordered_candidates)
        required = (float(factor) / count) if count else float("inf")
        ratio_note = ("relative concentration p=%.3f N=%d k=%.3f"
                      % (prob, count, factor))
        if count >= 2 and not prob > required:
            return ChoiceOutcome(
                False, reason="%s requires >%.3f" % (ratio_note, required),
                code="confidence")
    cand = table.ordered_candidates[index]
    if rejected.excludes(cand):
        return ChoiceOutcome(False, reason="member already rejected",
                             code="rejected-member")
    if eligible is not None and not eligible(cand):
        return ChoiceOutcome(False, reason="member is not currently safe",
                             code="unsafe")
    return ChoiceOutcome(True, candidate=cand, reason=ratio_note)


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


# -- matched movement accounting and prompt-edge evidence (prompt-edge plan) --
#
# These helpers are the single, pure transport seam shared by live control and
# offline evaluation (plan §A/§B): the movement identity of a successfully sent
# step, the recognized blocking-confirmation recognizer, and the confirmed
# decline test.  Nothing here mutates state or reads policy/engine memory.

#: The door-open destination purpose.  Mirrored as a literal (this module is a
#: dependency-neutral leaf and must never import ``navigation``); a
#: direction-shaped *door interaction* is not movement and must fail closed.
DOOR_PURPOSE = "open-door"

#: Accepted operation classes: ``label -> (operation, action_class)``.  Every
#: other label fails closed (plan §A rejected rows).
_ACCEPTED_MOVEMENT_OPS = {
    "navigate": ("destination", "normal"),
    "recovery-step": ("recovery", "recovery"),
    "escape": ("emergency", "emergency"),
}

#: Labels that are *never* movement even though they may carry a direction.
_REJECTED_MOVEMENT_LABELS = frozenset(
    ("search", "site-search", "secret-search", "descend", "forced-search",
     "trapped", "unblock", "random-move", "recovery"))


def _dest_purpose(candidate) -> str:
    """The destination purpose of a candidate's frozen payload, or ``""``."""
    payload = getattr(candidate, "effect_payload", ()) or ()
    if len(payload) >= 4 and payload[0] == "dest":
        return str(payload[3])
    return ""


def movement_origin_from_selected(candidate, need_kind, source_instance,
                                  pre_hero, attempt_key):
    """The frozen :class:`MovementOrigin` of a selected step, or ``None`` (§A).

    Total, side-effect-free, and dependent only on its arguments plus the
    candidate's immutable identity.  Accepts the four operation classes
    (destination acquire/continue, recovery step, directional emergency
    escape); rejects every other row -- door interaction, movement prefix,
    stair traversal, wait/search/forced-search, classless direction
    continuations, and synthesized/effect-less candidates -- so unknown or
    synthetic intent fails closed for edge learning.
    """
    if candidate is None or pre_hero is None:
        return None
    label = getattr(candidate, "semantic_label", "")
    if not label or label in _REJECTED_MOVEMENT_LABELS:
        return None
    op = _ACCEPTED_MOVEMENT_OPS.get(label)
    if op is None:
        return None
    # the action must actually be a plain movement-direction key
    delta = direction_delta(candidate.action, _DIR_KEYS)
    if delta is None:
        return None
    # a destination operation must carry its frozen destination payload: a
    # synthesized/effect-less navigate candidate fails closed, and a
    # direction-shaped door *interaction* is not movement.
    if label == "navigate":
        payload = getattr(candidate, "effect_payload", ()) or ()
        if not (len(payload) >= 4 and payload[0] == "dest"):
            return None
        if str(payload[3]) == DOOR_PURPOSE:
            return None
    src = (int(pre_hero[0]), int(pre_hero[1]))
    dst = (src[0] + delta[0], src[1] + delta[1])
    operation, action_class = op
    return candidates.MovementOrigin(
        attempt_key=tuple(attempt_key) if attempt_key else (),
        origin_need_kind=str(need_kind or ""),
        instance=int(source_instance) if source_instance is not None else 0,
        src=src, delta=tuple(delta), dst=dst,
        candidate_id=getattr(candidate, "candidate_id", ""),
        proposed_effect=getattr(candidate, "proposed_effect", ""),
        operation=operation, action_class=action_class)


def is_movement_entry_confirmation(prompt_text) -> bool:
    """True for a source-verified blocking movement-entry confirmation (§A).

    Starts with the exact engine confirmations (``src/hack.c:2542``:
    ``%s into that %s cloud?`` -- vapor or poison gas) and nothing else; the
    recognizer is intentionally narrow so an unrelated ``yn`` prompt can never
    be treated as a movement confirmation.
    """
    if not prompt_text:
        return False
    low = " ".join(str(prompt_text).lower().split())
    return ("into that " in low and low.rstrip().endswith("cloud?")
            and ("vapor cloud" in low or "poison gas cloud" in low))


def matched_movement_prompt(origin, response_need, instance, confirmed_hero,
                            need_key=()):
    """The pending prompt context for a matched blocking confirmation, or None.

    Created only when the response is a ``yn`` need carrying a recognized
    movement-entry confirmation, the instance is the origin's instance, and the
    *confirmed* hero is still the frozen source square (a moved hero is not a
    stationary confirmation).  ``None`` for every other frame.
    """
    if origin is None or not isinstance(response_need, dict):
        return None
    if response_need.get("kind") != "yn":
        return None
    prompt = response_need.get("prompt") or ""
    if not is_movement_entry_confirmation(prompt):
        return None
    if instance is None or int(instance) != int(origin.instance):
        return None
    if confirmed_hero is None or tuple(confirmed_hero) != tuple(origin.src):
        return None
    return candidates.MatchedMovementPrompt(
        attempt_key=tuple(origin.attempt_key),
        instance=int(origin.instance),
        src=tuple(origin.src), dst=tuple(origin.dst),
        action_class=origin.action_class,
        prompt_text=" ".join(str(prompt).split()),
        need_key=tuple(need_key))


def prompt_decline_confirmed(pending, instance, hero, dismissed) -> bool:
    """True only when a decline is proven by the post-answer observation (§C).

    Requires the answer to have been *sent*, the observation to be in the same
    instance, the confirmed hero to be unchanged at the frozen source square,
    and the engine to have left the confirmation (``dismissed``).  A stale
    ``n``, a failed write, a replacement prompt or a re-presented confirmation
    therefore never writes evidence.
    """
    if pending is None or not pending.answer_sent:
        return False
    if instance is None or int(instance) != int(pending.instance):
        return False
    if not dismissed:
        return False
    if hero is None or tuple(hero) != tuple(pending.src):
        return False
    return True


# ``protocol.DIR_KEYS`` mirrored as a literal so this leaf never imports
# ``protocol`` (a circular-safe, dependency-neutral seam).  The order and the
# values match ``protocol.DIR_KEYS`` exactly.
_DIR_KEYS = {(-1, -1): ord("y"), (0, -1): ord("k"), (1, -1): ord("u"),
             (-1, 0): ord("h"), (1, 0): ord("l"), (-1, 1): ord("b"),
             (0, 1): ord("j"), (1, 1): ord("n")}


def set_dir_keys(dir_keys) -> None:
    """Bind the canonical ``{(dx, dy): wire_key}`` table (test/DI seam)."""
    global _DIR_KEYS
    _DIR_KEYS = dict(dir_keys)


__all__ = [
    "REJECTION_CODES", "DEFAULT_CONFIDENCE_THRESHOLD", "RejectionSet",
    "CONFIDENCE_RELATIVE", "CONFIDENCE_ABSOLUTE", "CONFIDENCE_MODES",
    "DEFAULT_RELATIVE_FACTOR",
    "RejectionDecision", "classify_invalid", "select_retained", "RawChoice",
    "ChoiceOutcome", "validate_raw_choice", "Reconciliation",
    "classify_outcome", "arrival_outcome", "direction_delta",
    "ARRIVAL_PHRASES",
    "DOOR_PURPOSE", "movement_origin_from_selected",
    "is_movement_entry_confirmation", "matched_movement_prompt",
    "prompt_decline_confirmed", "set_dir_keys",
]
