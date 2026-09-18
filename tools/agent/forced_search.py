"""Isolated dangerous two-send forced-search transaction (wave 5).

The plan (``doc/agent-reflex-upgrade-plan.md`` sections 5.3-5.4) keeps
``risky-emergency-forced-search`` as the *single* caller-approved dangerous
exception.  It is deliberately isolated and it is only available at all once
the native engine contract has been proven (see
``test/agent/native_prefix_probe.py``): the ``m`` reqmenu prefix consumes no
game time, the exact immediately following command need carries the suffix,
and native double-``m`` cancels the prefix.

This module is pure and stdlib-only, so any layer may depend on it.  It owns
two things and nothing else:

* :func:`evaluate_activation_gates` / :func:`evaluate_success_gates` /
  :func:`evaluate_reassess` -- the ten mandatory gates of plan 5.3, each an
  independently falsifiable predicate over public evidence;
* :class:`ForcedSearchTransaction` -- the controller-owned
  ``PROPOSED -> PREFIX_SENT -> SUFFIX_SENT -> SUCCEEDED | FAILED`` automaton
  of plan 5.4, with cancellation from any live state and an episode activation
  cap consumed at the first successfully sent prefix and never refunded.

Nothing here sends anything, reads a wire or mutates gameplay memory: the
controller performs the sends and feeds the observed outcomes back in.

Condition vocabulary and the hunger ladder are taken from the engine
(``src/botl.c`` conditions[]; ``src/botl.c:2831`` hunger names) and reused
from :mod:`tools.agent.events`.
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

from .events import HUNGER_STAGES

# -- the dangerous contract -------------------------------------------------

#: The prefix command (``m``, do_reqmenu) and the single suffix search.
FORCED_SEARCH_PREFIX = "m"
FORCED_SEARCH_SUFFIX = "s"

#: Their wire key codes (the ``key`` action payload).
FORCED_SEARCH_PREFIX_CODE = ord("m")
FORCED_SEARCH_SUFFIX_CODE = ord("s")

#: The proposed-effect label the reflex attaches to a forced-search prefix
#: candidate so the controller can recognise it without a mapped action.
FORCED_SEARCH_EFFECT = "forced-search"

#: True once the native prefix/cancellation contract has been proven through
#: the real adapter by ``test/agent/native_prefix_probe.py`` (gate 6).  It is
#: a module constant, not a per-run guess: the fixture established that the
#: ``m`` prefix consumes no game time, the exact following need is a command
#: need, and native double-``m`` cancels the prefix with no time.
PREFIX_CONTRACT_VERIFIED = True

#: The episode activation cap: three successful prefixes, then trapped.
ACTIVATION_CAP = 3

#: The risk label the exception carries everywhere it is recorded.
RISK_LABEL = "risky-emergency-forced-search"

#: The graceful-quit reason once the exception can no longer run.
TRAPPED_QUIT_REASON = "policy-exhausted/trapped"

# -- condition vocabulary (src/botl.c conditions[]) -------------------------

#: Every condition the engine can display, by its short text (txt1).  An
#: unrecognised condition text is *unknown* and fails closed (gate 3).
RECOGNIZED_CONDITIONS = frozenset((
    "Bare", "Blind", "Busy", "Conf", "Deaf", "Iron", "Fly", "FoodPois",
    "Glow", "Grab", "Hallu", "Held", "Icy", "InLava", "Lev", "Parlyz",
    "Ride", "Zzz", "Slime", "Slip", "Stone", "Strngl", "Stun", "Submrg",
    "TermIll", "Teth", "Trap", "Out", "WLegs", "UHold",
))

#: Conditions that do *not* by themselves deny a forced search.  These are
#: cosmetic or non-incapacitating (glowing hands, wearing iron, bare hands,
#: flight/levitation).  Everything else recognised is treated as dangerous,
#: and anything unrecognised fails closed -- the conservative direction.
BENIGN_CONDITIONS = frozenset(("Glow", "Iron", "Bare", "Fly", "Lev"))

DANGEROUS_CONDITIONS = frozenset(RECOGNIZED_CONDITIONS - BENIGN_CONDITIONS)

#: The hunger ladder at "Hungry or worse" (plan 5.3 gate 3).
DENY_HUNGER_STAGES = frozenset(HUNGER_STAGES)


# -- the ten gates ----------------------------------------------------------

@dataclass(frozen=True)
class GateResult(object):
    """One evaluated gate: its id, pass/fail and a human reason."""

    gate: str
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class GateReport(object):
    """The full ten-gate evidence for one activation (plan 5.3)."""

    gates: Tuple[GateResult, ...] = ()

    def ok(self) -> bool:
        return all(g.ok for g in self.gates)

    def failed(self) -> Tuple[GateResult, ...]:
        return tuple(g for g in self.gates if not g.ok)

    def get(self, gate: str) -> Optional[GateResult]:
        for g in self.gates:
            if g.gate == gate:
                return g
        return None

    def as_dict(self) -> dict:
        return {g.gate: {"ok": g.ok, "reason": g.reason}
                for g in self.gates}


#: Stable gate ids, in plan order.
GATE_HERO = "g1-hero"
GATE_HP = "g2-hp"
GATE_CONDITIONS = "g3-conditions"
GATE_REFUSAL = "g4-refusal"
GATE_EXHAUSTION = "g5-exhaustion"
GATE_READY = "g6-intent-transport-contract"
GATE_CAP = "g7-cap"
GATE_BINDING = "g8-binding"
GATE_OUTCOME = "g9-outcome"
GATE_REASSESS = "g10-reassess"

ACTIVATION_GATES = (GATE_HERO, GATE_HP, GATE_CONDITIONS, GATE_REFUSAL,
                    GATE_EXHAUSTION, GATE_READY, GATE_CAP, GATE_BINDING)

#: The gates the reflex alone can judge (plan 5.3 gates 1-5): identity,
#: HP, conditions, exact refusal and exhaustion.  The controller owns the
#: remaining activation gates (transport, cap, binding).
LOCAL_GATES = (GATE_HERO, GATE_HP, GATE_CONDITIONS, GATE_REFUSAL,
               GATE_EXHAUSTION)


@dataclass(frozen=True)
class ForcedSearchContext(object):
    """The public evidence every gate reads.  All fields are public state.

    The first eight fields feed the activation gates (evaluated before the
    prefix is sent); ``suffix_*`` feed gate 9 (evaluated at the suffix
    outcome); ``reassessed``/``unchanged_failed_retry`` feed gate 10.
    """

    # gate 1 -- identity and need coherence
    hero_confirmed: bool = False
    command_need_coherent: bool = False
    instance_resolved: bool = False
    transition_pending: bool = False
    # gate 2 -- HP
    hp: Optional[int] = None
    hp_max: Optional[int] = None
    # gate 3 -- conditions
    hunger: str = ""
    conditions: Tuple[str, ...] = ()
    conditions_complete: bool = True
    # gate 4 -- refusal evidence
    refusal_kind: str = ""
    # gate 5 -- exhaustion
    alternatives_exhausted: bool = False
    # gate 6 -- ready
    no_pending_intent: bool = False
    transport_healthy: bool = False
    prefix_contract_verified: bool = False
    # gate 7 -- cap
    activations_used: int = 0
    # gate 8 -- binding
    bound_suffix_need: tuple = ()
    following_need: tuple = ()
    planned_suffix: str = ""
    # gate 9 -- outcome
    suffix_single: bool = True
    suffix_observed: bool = False
    suffix_time_advanced: bool = False
    # gate 10 -- reassessment
    reassessed: bool = True
    unchanged_failed_retry: bool = False


def conditions_ok(conditions: Sequence[str], hunger: str,
                  complete: bool = True) -> Tuple[bool, str]:
    """Gate 3's pure core (plan 5.3): safe, fully interpreted conditions."""
    for stage in DENY_HUNGER_STAGES:
        if (hunger or "").startswith(stage):
            return False, "hungry-or-worse (%s)" % hunger
    for name in conditions:
        text = (name or "").strip()
        if not text:
            continue
        if text in DANGEROUS_CONDITIONS:
            return False, "dangerous condition %s" % text
        if text not in RECOGNIZED_CONDITIONS:
            if not complete:
                return False, "unknown condition %r" % text
            return False, "unknown condition %r" % text
    if not complete:
        return False, "condition interpretation incomplete"
    return True, ""


def evaluate_activation_gates(ctx: ForcedSearchContext) -> GateReport:
    """Evaluate gates 1-8 (plan 5.3), each independently."""
    out = []

    ok = (ctx.hero_confirmed and ctx.command_need_coherent
          and ctx.instance_resolved and not ctx.transition_pending)
    out.append(GateResult(GATE_HERO, ok,
                          "" if ok else "hero/need/instance not resolved"))

    ok = (ctx.hp is not None and ctx.hp_max is not None and ctx.hp_max > 0
          and 2 * ctx.hp > ctx.hp_max)
    out.append(GateResult(GATE_HP, ok, "" if ok else
                          "HP not known-strictly-above-50%% (%r/%r)"
                          % (ctx.hp, ctx.hp_max)))

    ok, why = conditions_ok(ctx.conditions, ctx.hunger,
                            ctx.conditions_complete)
    out.append(GateResult(GATE_CONDITIONS, ok, why))

    ok = ctx.refusal_kind in ("monster", "danger")
    out.append(GateResult(GATE_REFUSAL, ok, "" if ok else
                          "no exact correlated search refusal"))

    ok = bool(ctx.alternatives_exhausted)
    out.append(GateResult(GATE_EXHAUSTION, ok, "" if ok else
                          "a legal movement/door/stair/food alternative "
                          "remains"))

    ok = (ctx.no_pending_intent and ctx.transport_healthy
          and ctx.prefix_contract_verified)
    out.append(GateResult(GATE_READY, ok, "" if ok else
                          "pending intent / unhealthy transport / "
                          "unverified prefix contract"))

    ok = int(ctx.activations_used) < ACTIVATION_CAP
    out.append(GateResult(GATE_CAP, ok, "" if ok else
                          "episode activation cap reached"))

    # The planned suffix is exactly a single search bound to the immediately
    # following command need -- never a generic key/direction need, never a
    # different future command.
    ok = (ctx.planned_suffix == FORCED_SEARCH_SUFFIX
          and (not ctx.bound_suffix_need
               or tuple(ctx.following_need) == tuple(ctx.bound_suffix_need)))
    out.append(GateResult(GATE_BINDING, ok, "" if ok else
                          "suffix is not the single s bound to the following "
                          "command need"))
    return GateReport(tuple(out))


def evaluate_success_gates(ctx: ForcedSearchContext) -> GateReport:
    """Evaluate gate 9 (plan 5.3): one search, observed, time advanced."""
    ok = (ctx.suffix_single and ctx.suffix_observed
          and ctx.suffix_time_advanced)
    return GateReport((GateResult(GATE_OUTCOME, ok, "" if ok else
                                  "suffix not a single observed "
                                  "time-advancing search"),))


def evaluate_reassess(ctx: ForcedSearchContext) -> GateReport:
    """Evaluate gate 10 (plan 5.3): reassess; never retry unchanged."""
    ok = bool(ctx.reassessed) and not ctx.unchanged_failed_retry
    return GateReport((GateResult(
        GATE_REASSESS, ok,
        "" if ok else "unchanged failed activation would repeat"),))


#: The gates rechecked immediately before a suffix send (plan 5.4).  This is
#: deliberately *not* the full activation set: exhaustion is not re-evaluated
#: because the prefix is already armed and the only legal continuations are
#: the bound suffix or a native cancellation, so the relevant question is
#: whether identity/HP/condition/transport/binding facts still hold.
BINDING_GATES = (GATE_HERO, GATE_HP, GATE_CONDITIONS, GATE_READY,
                 GATE_BINDING)


def evaluate_binding_gates(ctx: ForcedSearchContext) -> GateReport:
    """The subset of the activation gates rechecked before a suffix send."""
    full = evaluate_activation_gates(ctx)
    keep = {g.gate: g for g in full.gates}
    return GateReport(tuple(keep[name] for name in BINDING_GATES))


#: The gates checked before a *new* activation is proposed: the eight
#: activation gates plus the reassessment gate 10 (never repeat an unchanged
#: failed activation).  Gate 9 (outcome) is excluded -- it is only meaningful
#: after the suffix is sent.
PROPOSAL_GATES = ACTIVATION_GATES + (GATE_REASSESS,)


def evaluate_proposal_gates(ctx: ForcedSearchContext) -> GateReport:
    """The gates that must all hold before a prefix is proposed (5.3)."""
    a = evaluate_activation_gates(ctx)
    r = evaluate_reassess(ctx)
    return GateReport(tuple(a.gates) + tuple(r.gates))


def evaluate_all(ctx: ForcedSearchContext) -> GateReport:
    """All ten gates, for telemetry."""
    a = evaluate_activation_gates(ctx)
    s = evaluate_success_gates(ctx)
    r = evaluate_reassess(ctx)
    return GateReport(tuple(a.gates) + tuple(s.gates) + tuple(r.gates))


#: The gate inputs the controller supplies authoritatively.  The reflex's
#: nomination only contributes its own exhaustion/refusal judgement; every
#: other public fact (identity, HP, conditions, transport, cap, binding) is
#: re-derived by the controller from the live observation, so a stale reflex
#: view cannot activate the dangerous exception.  ``merge_controller_fields``
#: refuses a name outside this set so a typo cannot silently leave a gate
#: fail-closed or, worse, silently pass it.
CONTROLLER_GATE_FIELDS = (
    "hero_confirmed", "command_need_coherent", "instance_resolved",
    "transition_pending", "hp", "hp_max", "hunger", "conditions",
    "conditions_complete", "no_pending_intent", "transport_healthy",
    "prefix_contract_verified", "activations_used", "bound_suffix_need",
    "following_need", "planned_suffix", "reassessed",
    "unchanged_failed_retry",
)


def merge_controller_fields(ctx: ForcedSearchContext, **fields
                            ) -> ForcedSearchContext:
    """Overlay the controller-owned gate fields on a reflex-local template.

    Unknown field names are refused so a misspelling cannot quietly leave a
    gate fail-closed (or silently gate an activation).
    """
    bad = [k for k in fields if k not in CONTROLLER_GATE_FIELDS]
    if bad:
        raise ValueError("not controller gate fields: %r" % (bad,))
    data = dict(ctx.__dict__)
    data.update(fields)
    return ForcedSearchContext(**data)


def local_ok(report: GateReport) -> bool:
    """True when every reflex-local gate (1-5) passed.

    The controller-only gates are ignored, so a reflex-local template whose
    controller fields are still fail-closed is not misread as a denial.
    """
    seen = {g.gate: g.ok for g in report.gates}
    return all(seen.get(name, False) for name in LOCAL_GATES)


# -- the episode activation cap --------------------------------------------

class ForcedSearchBudget(object):
    """Episode-scoped activation accounting (plan 5.3 gate 7).

    The count persists across level instances, recovery changes and target
    changes.  It is consumed when the first dangerous prefix is successfully
    sent and is **never refunded** -- not for cancellation, invalid, a failed
    suffix write, a no-time suffix, shutdown or death (plan 5.4).
    """

    def __init__(self, cap: int = ACTIVATION_CAP) -> None:
        self.cap = int(cap)
        self.activations = 0

    def consume(self) -> None:
        self.activations += 1

    def remaining(self) -> int:
        return max(0, self.cap - self.activations)

    def allows(self) -> bool:
        return self.activations < self.cap

    def exhausted(self) -> bool:
        return not self.allows()


# -- the transaction --------------------------------------------------------

STATE_PROPOSED = "proposed"
STATE_PREFIX_SENT = "prefix_sent"
STATE_SUFFIX_SENT = "suffix_sent"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

LIVE_STATES = (STATE_PROPOSED, STATE_PREFIX_SENT, STATE_SUFFIX_SENT)
TERMINAL_STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED)


@dataclass(frozen=True)
class ForcedSearchTelemetry(object):
    """One activation's before/after evidence (plan 5.4)."""

    risk_label: str = RISK_LABEL
    activation_ordinal: int = 0
    instance: int = 0
    origin_need_key: tuple = ()
    before_hp: Optional[int] = None
    before_hp_max: Optional[int] = None
    before_time: Optional[int] = None
    after_hp: Optional[int] = None
    after_time: Optional[int] = None
    prefix_send_id: str = ""
    suffix_send_id: str = ""
    cancelled: bool = False
    cancel_reason: str = ""
    failure_reason: str = ""

    def time_delta(self) -> Optional[int]:
        if self.before_time is None or self.after_time is None:
            return None
        return self.after_time - self.before_time

    def hp_delta(self) -> Optional[int]:
        if self.before_hp is None or self.after_hp is None:
            return None
        return self.after_hp - self.before_hp


class ForcedSearchTransactionError(RuntimeError):
    """An illegal transition was attempted on the transaction."""


class ForcedSearchTransaction(object):
    """The controller-owned two-send transaction (plan 5.4).

    States: ``PROPOSED -> PREFIX_SENT -> SUFFIX_SENT -> SUCCEEDED | FAILED``,
    with cancellation from any live state.  A successful ``m`` is never
    refunded; a local-invalid or failed-write of the prefix consumes nothing.
    """

    def __init__(self, report: GateReport, need_key: tuple, instance: int,
                 hero: tuple, fingerprint: str,
                 budget: ForcedSearchBudget,
                 planned_suffix: str = FORCED_SEARCH_SUFFIX) -> None:
        if not report.ok():
            raise ForcedSearchTransactionError(
                "cannot propose: gates failed: %s"
                % [g.gate for g in report.failed()])
        self.report = report
        self.need_key = tuple(need_key)
        self.instance = int(instance)
        self.hero = tuple(hero)
        self.fingerprint = fingerprint
        self.budget = budget
        self.planned_suffix = planned_suffix
        self.state = STATE_PROPOSED
        self.bound_suffix_need: tuple = ()
        self.bound = False
        self.prefix_send_id = ""
        self.suffix_send_id = ""
        self.telemetry = ForcedSearchTelemetry(
            activation_ordinal=budget.activations, instance=self.instance,
            origin_need_key=self.need_key)
        self._terminal_emitted = False

    # -- queries ---------------------------------------------------------
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def prefix_armed(self) -> bool:
        """True while the engine prefix may still be active (no leak)."""
        return self.state in (STATE_PREFIX_SENT,)

    # -- lifecycle -------------------------------------------------------
    def on_prefix_sent(self, send_id: str) -> None:
        """The prefix ``m`` completed a full send: consume the cap once."""
        if self.state != STATE_PROPOSED:
            raise ForcedSearchTransactionError(
                "prefix sent from %s" % self.state)
        self.budget.consume()
        self.state = STATE_PREFIX_SENT
        self.prefix_send_id = send_id
        self.telemetry = _replace(self.telemetry,
                                  prefix_send_id=send_id,
                                  activation_ordinal=self.budget.activations)

    def on_prefix_failed(self, reason: str) -> None:
        """A local-invalid or failed prefix write consumed nothing."""
        if self.state not in (STATE_PROPOSED,):
            raise ForcedSearchTransactionError(
                "prefix failed from %s" % self.state)
        self.telemetry = _replace(self.telemetry, failure_reason=reason)
        self._fail()

    def bind_suffix(self, following_need: tuple, same_instance: bool,
                    evidence_unchanged: bool,
                    gates: Optional[GateReport] = None) -> bool:
        """Bind the suffix to the exact immediately following command need.

        Returns True only when the following need is a real command need in
        the same instance, the required evidence is unchanged and the gates
        still hold.  Any other case refuses the binding (the controller then
        cancels); the armed prefix is never handed to a later command.
        """
        if self.state != STATE_PREFIX_SENT:
            raise ForcedSearchTransactionError(
                "bind from %s" % self.state)
        ok = (tuple(following_need) != ()
              and same_instance and evidence_unchanged
              and (gates is None or gates.ok()))
        if ok:
            self.bound_suffix_need = tuple(following_need)
            self.bound = True
        return ok

    def on_suffix_sent(self, send_id: str) -> None:
        """The suffix ``s`` completed a full send to the bound need."""
        if self.state != STATE_PREFIX_SENT:
            raise ForcedSearchTransactionError(
                "suffix sent from %s" % self.state)
        if not self.bound:
            raise ForcedSearchTransactionError(
                "suffix sent without a bound following need")
        self.state = STATE_SUFFIX_SENT
        self.suffix_send_id = send_id
        self.telemetry = _replace(self.telemetry, suffix_send_id=send_id)

    def on_suffix_outcome(self, observed: bool, time_advanced: bool,
                          after_time: Optional[int] = None,
                          after_hp: Optional[int] = None) -> str:
        """Conclude the transaction from the suffix's observed outcome.

        Success requires an observed outcome *and* an increased displayed
        time (plan 5.4); a no-time / rejected / unknown suffix fails.  A
        success may still include HP loss and remains dangerous.
        """
        if self.state != STATE_SUFFIX_SENT:
            raise ForcedSearchTransactionError(
                "outcome from %s" % self.state)
        self.telemetry = _replace(self.telemetry, after_time=after_time,
                                  after_hp=after_hp)
        if observed and time_advanced:
            self.state = STATE_SUCCEEDED
        else:
            self._fail("suffix not an observed time-advancing search")
            return self.state
        return self.state

    def note_before(self, hp: Optional[int], hp_max: Optional[int],
                    time_value: Optional[int]) -> None:
        self.telemetry = _replace(self.telemetry, before_hp=hp,
                                  before_hp_max=hp_max,
                                  before_time=time_value)

    def cancel(self, reason: str) -> None:
        """Cancel from any live state.  A sent prefix is never refunded."""
        if self.is_terminal():
            return
        self.state = STATE_CANCELLED
        self.telemetry = _replace(self.telemetry, cancelled=True,
                                  cancel_reason=reason)

    def terminal_event(self) -> Tuple[str, ForcedSearchTelemetry]:
        """Return ``(state, telemetry)`` exactly once (plan 3.4)."""
        if not self.is_terminal():
            raise ForcedSearchTransactionError(
                "no terminal event while %s" % self.state)
        if self._terminal_emitted:
            raise ForcedSearchTransactionError("terminal event already sent")
        self._terminal_emitted = True
        return self.state, self.telemetry

    def _fail(self, reason: str = "") -> None:
        self.state = STATE_FAILED
        if reason:
            self.telemetry = _replace(self.telemetry, failure_reason=reason)


def _replace(rec: ForcedSearchTelemetry, **fields) -> ForcedSearchTelemetry:
    data = dict(rec.__dict__)
    data.update(fields)
    return ForcedSearchTelemetry(**data)


__all__ = [
    "FORCED_SEARCH_PREFIX", "FORCED_SEARCH_SUFFIX",
    "FORCED_SEARCH_PREFIX_CODE", "FORCED_SEARCH_SUFFIX_CODE",
    "FORCED_SEARCH_EFFECT", "PREFIX_CONTRACT_VERIFIED", "ACTIVATION_CAP",
    "RISK_LABEL", "TRAPPED_QUIT_REASON",
    "RECOGNIZED_CONDITIONS", "BENIGN_CONDITIONS", "DANGEROUS_CONDITIONS",
    "DENY_HUNGER_STAGES",
    "GateResult", "GateReport", "ForcedSearchContext",
    "GATE_HERO", "GATE_HP", "GATE_CONDITIONS", "GATE_REFUSAL",
    "GATE_EXHAUSTION", "GATE_READY", "GATE_CAP", "GATE_BINDING",
    "GATE_OUTCOME", "GATE_REASSESS", "ACTIVATION_GATES", "LOCAL_GATES",
    "BINDING_GATES", "evaluate_binding_gates",
    "PROPOSAL_GATES", "evaluate_proposal_gates",
    "conditions_ok", "evaluate_activation_gates", "evaluate_success_gates",
    "evaluate_reassess", "evaluate_all",
    "CONTROLLER_GATE_FIELDS", "merge_controller_fields", "local_ok",
    "ForcedSearchBudget", "ForcedSearchTelemetry",
    "ForcedSearchTransaction", "ForcedSearchTransactionError",
    "STATE_PROPOSED", "STATE_PREFIX_SENT", "STATE_SUFFIX_SENT",
    "STATE_SUCCEEDED", "STATE_FAILED", "STATE_CANCELLED",
    "LIVE_STATES", "TERMINAL_STATES",
]
