"""Validated strategy directives and their episode-local lifecycle.

A strategy call returns *goals*, never wire actions (see
``doc/agent-autoplay-plan.md`` section "Contracts and ownership").  This
module is the only place a provider's JSON becomes something the reflex may
read:

  * :func:`validate_directive_set` is a strict, closed-world validator.  Any
    unknown key, any goal outside the fixed set, any key-like or text-like
    field, any out-of-range coordinate or risk, and any over-long explanation
    are rejected, and a rejected set is discarded rather than partially
    applied.
  * :class:`DirectiveBook` owns activation, TTL, level and precondition
    checks.  Advice is activated at the next command boundary, discarded when
    the displayed level changes, expired when its tick TTL runs out, and
    dropped when its preconditions no longer hold.
  * :class:`DirectiveView` is the read-only surface the reflex consults.  It
    can only bias an action the reflex already knows how to build; it can
    never contribute an action of its own.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Goals a strategy may name.  The reflex knows how each one biases its own
# action; an unknown goal is a validation failure, never a silent ignore.
GOALS = ("survive", "acquire_food", "eat_known_safe_food", "recover",
         "explore_frontier", "search_dead_ends", "descend_known_stairs",
         "inspect_inventory", "disengage")

# State predicates a directive may require.  Each is evaluated locally from
# public state only; an unknown predicate is rejected.
PRECONDITIONS = ("hero_known", "hp_known", "hungry", "not_hungry",
                 "hp_below_half", "hp_above_half", "inventory_fresh")

SCHEMA_VERSION = 1
MAX_GOALS = 8
MAX_PRECONDITIONS = 4
MAX_TTL = 500
MAX_EXPLANATION = 240
_ALLOWED_KEYS = frozenset(("schema_version", "goals", "target", "risk",
                           "ttl", "preconditions", "explanation"))
# Field-name fragments that would let a provider smuggle wire content in.
_FORBIDDEN_HINTS = ("key", "action", "menu", "exec", "command", "shell",
                    "script", "text", "commit", "row")

MAP_MIN_X, MAP_MAX_X = 1, 79
MAP_MIN_Y, MAP_MAX_Y = 0, 20


@dataclass(frozen=True)
class DirectiveSet(object):
    goals: Tuple[str, ...]
    target: Optional[Tuple[int, int]] = None
    risk: float = 0.0
    ttl: int = 1
    preconditions: Tuple[str, ...] = ()
    explanation: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version,
                "goals": list(self.goals),
                "target": list(self.target) if self.target else None,
                "risk": self.risk, "ttl": self.ttl,
                "preconditions": list(self.preconditions),
                "explanation": self.explanation}


def _reject(msg: str):
    return (None, msg)


def validate_directive_set(obj: Any) -> Tuple[Optional[DirectiveSet], str]:
    """Return ``(DirectiveSet, "")`` or ``(None, reason)``.

    Deliberately strict: the provider's JSON is untrusted input, so an
    unexpected field is a rejection, not something to tolerate.  A missing
    optional field is fine; a malformed one never is.
    """
    if not isinstance(obj, dict):
        return _reject("directive set is not an object")
    for name in obj:
        if not isinstance(name, str):
            return _reject("directive set has a non-string key")
        low = name.lower()
        if any(h in low for h in _FORBIDDEN_HINTS):
            return _reject("field %r looks like wire content" % (name,))
        if name not in _ALLOWED_KEYS:
            return _reject("unexpected field %r" % (name,))
    ver = obj.get("schema_version", SCHEMA_VERSION)
    if ver != SCHEMA_VERSION:
        return _reject("schema_version is not %d" % SCHEMA_VERSION)
    goals = obj.get("goals")
    if not isinstance(goals, list) or not goals:
        return _reject("goals must be a non-empty list")
    if len(goals) > MAX_GOALS:
        return _reject("too many goals (%d)" % len(goals))
    seen = set()
    for g in goals:
        if not isinstance(g, str) or g not in GOALS:
            return _reject("unknown goal %r" % (g,))
        if g in seen:
            return _reject("duplicate goal %r" % (g,))
        seen.add(g)
    target = obj.get("target")
    if target is not None:
        if (not isinstance(target, (list, tuple)) or len(target) != 2
                or not _is_int(target[0]) or not _is_int(target[1])):
            return _reject("target must be a [x,y] pair")
        if not (MAP_MIN_X <= target[0] <= MAP_MAX_X
                and MAP_MIN_Y <= target[1] <= MAP_MAX_Y):
            return _reject("target is outside the map rectangle")
        target = (target[0], target[1])
    risk = obj.get("risk", 0.0)
    if not _finite(risk) or not (0.0 <= float(risk) <= 1.0):
        return _reject("risk must be a finite number in [0,1]")
    ttl = obj.get("ttl", 1)
    if not _is_int(ttl) or not (1 <= ttl <= MAX_TTL):
        return _reject("ttl must be an integer in 1..%d" % MAX_TTL)
    pre = obj.get("preconditions", [])
    if pre is None:
        pre = []
    if not isinstance(pre, list):
        return _reject("preconditions must be a list")
    if len(pre) > MAX_PRECONDITIONS:
        return _reject("too many preconditions")
    for p in pre:
        if not isinstance(p, str) or p not in PRECONDITIONS:
            return _reject("unknown precondition %r" % (p,))
    expl = obj.get("explanation", "")
    if expl is None:
        expl = ""
    if not isinstance(expl, str):
        return _reject("explanation must be a string")
    if len(expl) > MAX_EXPLANATION:
        return _reject("explanation is too long")
    return (DirectiveSet(goals=tuple(goals), target=target,
                         risk=float(risk), ttl=int(ttl),
                         preconditions=tuple(pre), explanation=expl,
                         schema_version=SCHEMA_VERSION), "")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _finite(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    f = float(v)
    return f == f and f not in (float("inf"), float("-inf"))


@dataclass
class PreconditionState(object):
    """The public facts a precondition is evaluated against."""

    hero_known: bool = False
    hp_known: bool = False
    hp_frac: Optional[float] = None
    hungry: bool = False
    inventory_fresh: bool = False


def preconditions_met(dset: DirectiveSet,
                      st: PreconditionState) -> bool:
    for p in dset.preconditions:
        if p == "hero_known" and not st.hero_known:
            return False
        if p == "hp_known" and not st.hp_known:
            return False
        if p == "hungry" and not st.hungry:
            return False
        if p == "not_hungry" and st.hungry:
            return False
        if p == "hp_below_half" and not (st.hp_frac is not None
                                         and st.hp_frac <= 0.5):
            return False
        if p == "hp_above_half" and not (st.hp_frac is not None
                                         and st.hp_frac > 0.5):
            return False
        if p == "inventory_fresh" and not st.inventory_fresh:
            return False
    return True


def _ineligibility_reason(dset: Optional[DirectiveSet],
                          activated_tick: Optional[int],
                          activated_level: Optional[str], *,
                          tick: int, level: Optional[str],
                          st: PreconditionState,
                          activated_instance: Optional[int] = None,
                          instance: Optional[int] = None) -> Optional[str]:
    """Why the active set is *not* in force, or None when it is.

    The single eligibility authority: :meth:`DirectiveBook.active` (for the
    reflex, which expires and logs on a failure) and
    :meth:`DirectiveBook.peek_view` (for display, which must never mutate)
    both call this, so their answers cannot drift.  ``None`` means either
    "no set" or "eligible"; the caller distinguishes those by whether it
    holds a set at all.  Precedence is instance, level, TTL, then
    precondition; the TTL test keeps the exact ``age > ttl`` semantics
    (equality is eligible) with the same None-activation and None-level
    behavior.  An instance mismatch is the level-instance scope of plan 4.4:
    advice produced for a different instance is stale even on the same
    displayed level, and a ``None`` on either side means "no instance check".
    """
    if dset is None:
        return None
    if activated_instance is not None and instance is not None \
            and instance != activated_instance:
        return "instance-changed"
    if activated_level is not None and level is not None \
            and level != activated_level:
        return "level-changed"
    if activated_tick is not None:
        age = tick - activated_tick
        if age > dset.ttl:
            return "ttl-expired"
    if not preconditions_met(dset, st):
        return "precondition-failed"
    return None


class DirectiveBook(object):
    """Activation, TTL/level/precondition enforcement for one episode."""

    # Bounded in-memory window of lifecycle events.  Each event is emitted to
    # ``sink`` as it is logged, so this list is only the replay-visible trace
    # for inspection and is capped rather than grown over a long episode.
    EVENT_CAP = 4096

    def __init__(self, sink=None, cap: int = EVENT_CAP) -> None:
        self.sink = sink
        self.cap = int(cap)
        self.reset()

    def reset(self) -> None:
        self._active: Optional[DirectiveSet] = None
        self.activated_tick: Optional[int] = None
        self.level: Optional[str] = None
        self.active_instance: Optional[int] = None
        self.generation = 0
        self.events: List[Dict[str, Any]] = []

    @property
    def has_active(self) -> bool:
        return self._active is not None

    def activate(self, dset: DirectiveSet, tick: int, level: Optional[str],
                 reason: str = "activated",
                 instance: Optional[int] = None) -> None:
        self.generation += 1
        self._active = dset
        self.activated_tick = tick
        self.level = level
        self.active_instance = instance
        self._log("applied", reason, tick, level, dset.to_dict())

    def expire(self, reason: str, tick: Optional[int] = None,
               level: Optional[str] = None) -> None:
        if self._active is not None:
            if tick is None:
                tick = self.activated_tick
            if level is None:
                level = self.level
            self._active = None
            self.activated_tick = None
            self.level = None
            self.active_instance = None
            self._log("expired", reason, tick, level)

    def on_instance_change(self, instance: Optional[int],
                           tick: Optional[int] = None) -> bool:
        """Expire the active set when a fresh instance was allocated (4.4).

        The controller calls this once at the reconciliation boundary where a
        fresh arrival is settled, so the *old* instance's directive generation
        is retired exactly once rather than carried into the new scope.  A set
        already belonging to *instance* (or no set at all) is untouched, and a
        set for a different instance logs exactly one expiry event.
        """
        if self._active is None:
            return False
        if self.active_instance is not None and instance is not None \
                and instance == self.active_instance:
            return False
        self.expire("instance-changed", tick)
        return True

    def active(self, tick: int, level: Optional[str],
               st: PreconditionState,
               instance: Optional[int] = None) -> Optional[DirectiveSet]:
        """The directive set in force right now, or None.

        Enforces, in order: activation exists; the instance still matches; the
        displayed level still matches (advice about another level is stale);
        the tick TTL has not run out; every precondition still holds.  Any
        failure expires the set rather than leaving it half-applied.  The
        predicate itself is :func:`_ineligibility_reason`, shared with
        :meth:`peek_view`.
        """
        if self._active is None:
            return None
        reason = _ineligibility_reason(
            self._active, self.activated_tick, self.level,
            tick=tick, level=level, st=st,
            activated_instance=self.active_instance, instance=instance)
        if reason is not None:
            self.expire(reason, tick, level)
            return None
        return self._active

    def view(self, tick: int, level: Optional[str],
             st: PreconditionState,
             instance: Optional[int] = None) -> "DirectiveView":
        dset = self.active(tick, level, st, instance)
        return DirectiveView(dset, self.generation if dset else 0)

    def peek_view(self, tick: int, level: Optional[str],
                  st: PreconditionState,
                  instance: Optional[int] = None) -> "DirectiveView":
        """The read-only view for *display*: never expires, logs or mutates.

        Same eligibility predicate as :meth:`view` (:func:`active`), but a
        failing set is reported as inactive without touching book state -- no
        ``expire``, no lifecycle event, no sink call, no generation change.
        So a frame can show the directive state a later reflex read will
        still see, and rendering a frame can never itself advance the book.
        """
        dset = self._active
        if dset is None or _ineligibility_reason(
                dset, self.activated_tick, self.level,
                tick=tick, level=level, st=st,
                activated_instance=self.active_instance,
                instance=instance) is not None:
            return DirectiveView(None, 0)
        return DirectiveView(dset, self.generation)

    def _log(self, state: str, reason: str, tick: Optional[int] = None,
             level: Optional[str] = None,
             directive: Optional[Dict[str, Any]] = None) -> None:
        ev = {"state": state, "reason": reason, "generation": self.generation,
              "tick": tick, "level": level}
        if directive is not None:
            ev["directive"] = directive
        self.events.append(ev)
        if len(self.events) > self.cap:
            del self.events[:len(self.events) - self.cap]
        if self.sink is not None:
            self.sink(dict(ev))


class DirectiveView(object):
    """The read-only, action-free surface the reflex consults."""

    def __init__(self, dset: Optional[DirectiveSet],
                 generation: int = 0) -> None:
        self.dset = dset
        self.generation = generation

    @property
    def active(self) -> bool:
        return self.dset is not None

    @property
    def goals(self) -> Tuple[str, ...]:
        return self.dset.goals if self.dset else ()

    @property
    def target(self) -> Optional[Tuple[int, int]]:
        return self.dset.target if self.dset else None

    @property
    def risk(self) -> float:
        return self.dset.risk if self.dset else 0.0

    def wants(self, goal: str) -> bool:
        return goal in self.goals

    def top_goal(self) -> str:
        return self.goals[0] if self.goals else ""

    # convenience predicates the reflex reads
    def prefers_stairs(self) -> bool:
        return self.wants("descend_known_stairs")

    def prefers_frontier(self) -> bool:
        return self.wants("explore_frontier") \
            or self.wants("search_dead_ends")

    def wants_food(self) -> bool:
        return self.wants("acquire_food") \
            or self.wants("eat_known_safe_food")

    def disengage(self) -> bool:
        return self.wants("disengage") or self.wants("survive")
