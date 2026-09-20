"""Deterministic boundary detection and (bounded) coalescing.

The detector answers one question: *has something happened that a strategy
tier might need to know about?*  It is pure and episode-local, so it can be
unit-tested without a wire, a clock or a network.  Every event carries a
stable episode-local id, and an id is emitted **once** -- re-presenting the
same full snapshot, or replaying history, produces zero new events.

The queue is the second half: detected events are coalesced into at most one
pending request, rate-limited by a normal cooldown (50 displayed ticks **and**
5 wall seconds by default), and dispatched one at a time.  Severe crossings
(an HP crisis, a hunger stage at Fainting or worse) may bypass the normal
cooldown but stay cap-limited and are still subject to a short emergency wall
cooldown.

States an event can be in -- all observable in tests:

  ``detected``    the detector emitted it this observation
  ``queued``      merged into the one pending coalesced request
  ``dispatched``  handed to the strategy tier (a call is in flight)
  ``suppressed``  dropped by cooldown/cap/policy before it was dispatched
  ``expired``     stale before it could be applied (level changed, etc.)
  ``applied``     its directives were activated by the reflex tier

:class:`EventLedger` persists that lifecycle per event id -- one record per
detected boundary, with the deterministic tick/level of each transition, the
coalesced members of its pending set and every reason it carried.  Wall-clock
timing lives under a separate ``wall`` map so a replay comparison can drop it.
"""

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Schema version of a persisted event-lifecycle record (``EP-EVENTS``).
EVENT_SCHEMA = 1

# The engine's hunger ladder, weakest to worst.  "Worsening" is an increase
# in this index, so a snapshot that merely repeats the same word is not an
# event.
HUNGER_STAGES = ("Hungry", "Weak", "Fainting", "Fainted", "Starved")
# Hunger stages severe enough to bypass the normal dispatch cooldown.
SEVERE_HUNGER = frozenset(("Fainting", "Fainted", "Starved"))

# Explicit engine descriptions of an item or trap the hero just noticed.
# These are *explicit* ("You see here a ..."), which is exactly why they are
# worth an event; the detector still notes that identity stays ambiguous.
_DESC_RES = (
    re.compile(r"\byou see here (?:a|an|the|some) (.+)", re.I),
    re.compile(r"\byou find (?:a|an|the|some) (.+)", re.I),
    re.compile(r"\bthere is (?:a|an|the|some) (.+)", re.I),
    re.compile(r"\byou (?:discover|notice) (?:a|an|the|some) (.+)", re.I),
)
_DESC_TRIM = re.compile(r"[\s.,;:!?]+")
_MAX_DESC = 48


@dataclass(frozen=True)
class Boundary(object):
    reason: str
    eid: str
    severe: bool = False
    detail: str = ""


def _hunger_stage(hunger: str) -> str:
    for stage in HUNGER_STAGES:
        if hunger.startswith(stage):
            return stage
    return ""


def hunger_index(hunger: str) -> int:
    stage = _hunger_stage(hunger)
    if not stage:
        return -1
    return HUNGER_STAGES.index(stage)


def novel_descriptions(messages: Iterable[str]) -> List[Tuple[str, str]]:
    """Normalised ``(text, eid)`` for each explicit new-description match."""
    out = []
    seen = set()
    for msg in messages:
        for rx in _DESC_RES:
            m = rx.search(msg or "")
            if not m:
                continue
            text = _DESC_TRIM.sub(" ", m.group(1)).strip().lower()
            text = text[:_MAX_DESC].strip()
            if not text:
                continue
            eid = "novel:" + re.sub(r"[^a-z0-9]+", "-", text).strip("-")
            if eid and eid not in seen:
                seen.add(eid)
                out.append((text, eid))
    return out


class BoundaryDetector(object):
    """Episode-local, deterministic detection with stable event ids."""

    HP_CRISIS_LOW = 0.30
    HP_CRISIS_HIGH = 0.50

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._armed_hp = True
        self._hp_gen = 0
        self._last_level: Optional[str] = None
        self._level_gen = 0
        self._hunger_max = -1
        self._seen_classes: Set[str] = set()
        self._seen_novel: Set[str] = set()
        self._inv_sig: Optional[Tuple[str, ...]] = None
        self._inv_gen = 0
        self._food_failed_bucket = 0
        self._low_conf_bucket = 0
        self._closed = False

    def check(self, st, classes: Iterable[str] = (),
              messages: Iterable[str] = (), inventory_sig=None,
              failed_food: int = 0, low_conf_streak: int = 0,
              low_conf_threshold: int = 3,
              closed: bool = False) -> List[Boundary]:
        """Return the boundaries newly detected by one applied snapshot.

        ``st`` is any object exposing ``hp``, ``hp_max``, ``hunger`` and
        ``dlvl`` (a :class:`tools.agent.state.Status`).  Every other argument
        is public state the caller already extracted.
        """
        out: List[Boundary] = []
        out += self._check_level(st)
        out += self._check_hp(st)
        out += self._check_hunger(st)
        out += self._check_classes(classes)
        out += self._check_novel(messages)
        out += self._check_inventory(inventory_sig)
        out += self._check_food(failed_food)
        out += self._check_low_conf(low_conf_streak, low_conf_threshold)
        if closed and not self._closed:
            self._closed = True
            out.append(Boundary("closed", "closed", False,
                                "episode closed"))
        return out

    # -- individual detectors -------------------------------------------
    def _check_level(self, st) -> List[Boundary]:
        dlvl = getattr(st, "dlvl", "") or ""
        if not dlvl or dlvl == self._last_level:
            return []
        reason = "initial-level" if self._last_level is None \
            else "level-change"
        self._last_level = dlvl
        self._level_gen += 1
        return [Boundary(reason, "level:%s:%d" % (dlvl, self._level_gen))]

    def _check_hp(self, st) -> List[Boundary]:
        hp = getattr(st, "hp", None)
        hp_max = getattr(st, "hp_max", None)
        if hp is None or not hp_max:
            return []
        frac = hp / float(hp_max)
        if self._armed_hp and frac <= self.HP_CRISIS_LOW:
            self._armed_hp = False
            self._hp_gen += 1
            return [Boundary("hp-crisis", "hp-crisis:%d" % self._hp_gen,
                             True, "hp<=%.0f%%" % (frac * 100))]
        if not self._armed_hp and frac > self.HP_CRISIS_HIGH:
            self._armed_hp = True
        return []

    def _check_hunger(self, st) -> List[Boundary]:
        idx = hunger_index(getattr(st, "hunger", "") or "")
        if idx < 0 or idx <= self._hunger_max:
            return []
        self._hunger_max = idx
        stage = HUNGER_STAGES[idx]
        return [Boundary("hunger-%s" % stage.lower(),
                         "hunger:%s" % stage, stage in SEVERE_HUNGER,
                         "hunger stage %s" % stage)]

    def _check_classes(self, classes: Iterable[str]) -> List[Boundary]:
        out = []
        for ch in sorted(set(classes or ())):
            if not ch or ch == "@" or ch in self._seen_classes:
                continue
            self._seen_classes.add(ch)
            out.append(Boundary("novelty-class", "class:%s" % ch, False,
                                "appearance only; identity ambiguous"))
        return out

    def _check_novel(self, messages: Iterable[str]) -> List[Boundary]:
        out = []
        for _text, eid in novel_descriptions(messages):
            if eid in self._seen_novel:
                continue
            self._seen_novel.add(eid)
            out.append(Boundary("novelty-item", eid, False,
                                "explicitly described; identity ambiguous"))
        return out

    def _check_inventory(self, inventory_sig) -> List[Boundary]:
        if inventory_sig is None:
            return []
        sig = tuple(inventory_sig)
        if self._inv_sig is None:
            self._inv_sig = sig       # first read is the baseline
            return []
        if sig == self._inv_sig:
            return []
        self._inv_sig = sig
        self._inv_gen += 1
        return [Boundary("inventory-change", "inventory:%d" % self._inv_gen,
                         False, "inventory list changed")]

    def _check_food(self, failed_food: int) -> List[Boundary]:
        if failed_food < 2:
            return []
        bucket = failed_food // 2
        if bucket <= self._food_failed_bucket:
            return []
        self._food_failed_bucket = bucket
        return [Boundary("food-intent-failed",
                         "food-failed:%d" % bucket, False,
                         "two food intents were rejected")]

    def _check_low_conf(self, streak: int, threshold: int) -> List[Boundary]:
        if threshold <= 0 or streak < threshold:
            return []
        bucket = streak // threshold
        if bucket <= self._low_conf_bucket:
            return []
        self._low_conf_bucket = bucket
        return [Boundary("low-confidence", "low-confidence:%d" % bucket,
                         False, "sustained low reflex confidence")]


@dataclass
class PendingBoundary(object):
    """The one coalesced request a strategy call would answer."""

    reasons: List[str] = field(default_factory=list)
    eids: List[str] = field(default_factory=list)
    severe: bool = False
    tick: int = 0
    level: str = ""

    def merge(self, boundary: Boundary, tick: int) -> None:
        if boundary.reason not in self.reasons:
            self.reasons.append(boundary.reason)
        if boundary.eid not in self.eids:
            self.eids.append(boundary.eid)
        self.severe = self.severe or boundary.severe
        self.tick = tick


# How many finalised lifecycle records are kept in memory for inspection.  A
# longer episode still *emits* every record (to the recording writer) as it
# is finalised; only the in-memory detail window is capped, so a burst cannot
# grow the ledger without bound.
_RETAIN_CAP = 4096


class EventLedger(object):
    """Per-EID boundary lifecycle, schema-versioned and replay-stable.

    Every detected boundary gets exactly one record tracing its transitions.
    ``detected`` -> ``queued`` -> ``dispatched`` -> one *terminal* state
    (``applied``/``expired``/``suppressed``).  Each transition carries the
    deterministic displayed ``tick`` and dungeon ``level``; the raw wall
    clock is confined to a separate ``wall`` map so a replay comparison over
    the deterministic fields stays exact.

    A record is *finalised* -- and handed to ``sink`` -- the moment it can no
    longer change: on a terminal transition, or when the detection round that
    produced it queued nothing (:meth:`flush_unqueued`).  Emitting
    incrementally keeps the episode's memory bounded and avoids the
    end-of-episode burst that could overflow the recording writer queue.  A
    bounded window of finalised records is retained for inspection
    (:meth:`as_list`); once ``cap`` is exceeded the oldest detail is dropped
    and :attr:`collapsed` counts it, but emission continues unaffected.
    """

    def __init__(self, sink=None, cap: int = _RETAIN_CAP) -> None:
        self.sink = sink
        self.cap = int(cap)
        self._open: Dict[str, Dict[str, Any]] = {}
        self._retained: List[Dict[str, Any]] = []
        self.emitted = 0
        self.collapsed = 0

    def _rec(self, eid: str) -> Dict[str, Any]:
        rec = self._open.get(eid)
        if rec is None:
            rec = {"schema": EVENT_SCHEMA, "record": "boundary", "eid": eid,
                   "kind": "", "detail": "", "severe": False, "reasons": [],
                   "detected": None, "queued": None, "dispatched": None,
                   "terminal": None, "coalesced_with": [], "wall": {}}
            self._open[eid] = rec
        return rec

    def _finalize(self, eid: str) -> None:
        """Emit a record that can no longer change and bound the window."""
        rec = self._open.pop(eid, None)
        if rec is None:
            return
        self.emitted += 1
        if self.sink is not None:
            self.sink(dict(rec))
        self._retained.append(rec)
        while len(self._retained) > self.cap:
            self._retained.pop(0)
            self.collapsed += 1

    def detect(self, boundary: "Boundary", tick: int,
               level: str = "") -> None:
        """Record one boundary the detector emitted this observation."""
        rec = self._rec(boundary.eid)
        if not rec["kind"]:
            rec["kind"] = boundary.reason
        rec["detail"] = boundary.detail
        rec["severe"] = bool(boundary.severe)
        rec["detected"] = {"tick": tick, "level": level}
        rec["reasons"].append(boundary.reason)

    def transition(self, state: str, eid: str, reason: str, tick: int,
                   level: str = "", wall: float = 0.0) -> None:
        """Record one lifecycle step for *eid* (creating it if unseen)."""
        rec = self._rec(eid)
        if state == "queued" and not rec["kind"]:
            rec["kind"] = reason
        rec["reasons"].append(reason)
        rec["wall"][state] = round(wall, 6)
        if state in ("queued", "dispatched"):
            rec[state] = {"tick": tick, "level": level}
        elif state in ("applied", "expired", "suppressed"):
            rec["terminal"] = {"state": state, "tick": tick, "level": level,
                               "reason": reason}
            self._finalize(eid)

    def coalesce(self, eids: Sequence[str]) -> None:
        """Record that *eids* were merged into one pending request.

        Called with the *complete* pending set, not just the newest arrivals:
        a member merged into an already-pending request is restamped too, so
        every member names the same coalesced set.
        """
        group = list(eids)
        for eid in group:
            rec = self._open.get(eid)
            if rec is None:
                continue
            rec["coalesced_with"] = [e for e in group if e != eid]

    def flush_unqueued(self) -> None:
        """Finalise every open record that was detected but not queued.

        The queue only ever receives the boundaries detected in the same
        observation, so once that round has run an EID left unqueued can
        never be queued later: its "detected only" record is final.
        """
        for eid in [e for e, r in self._open.items() if r["queued"] is None]:
            self._finalize(eid)

    def flush(self) -> None:
        """Finalise every remaining open record (episode end)."""
        for eid in list(self._open):
            self._finalize(eid)

    def as_list(self) -> List[Dict[str, Any]]:
        """The retained window of finalised records, oldest first."""
        return list(self._retained)


def directive_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap one :class:`DirectiveBook` lifecycle event as a ledger record."""
    out = {"schema": EVENT_SCHEMA, "record": "directive"}
    out.update(ev)
    return out


def lifecycle_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap one destination/pickup lifecycle event as a ledger record (5).

    Additive: the same event sidecar gains a ``record: "lifecycle"`` line
    type; readers that do not know it ignore it, and an artifact produced
    before it existed simply has no lifecycle stream (unavailable, never
    zero).
    """
    # The lifecycle payload carries its own inner ``schema`` (the lifecycle
    # schema version); that must NOT clobber the *outer* event-envelope schema
    # (review item 7).  The envelope keeps ``schema`` at the event-envelope
    # version and the lifecycle version travels only in ``schema_version``.
    out = dict(ev)
    out.pop("schema", None)
    out["schema"] = EVENT_SCHEMA
    out["record"] = "lifecycle"
    return out


class BoundaryQueue(object):
    """Coalesce detected boundaries and rate-limit dispatch to the
    strategy."""

    # Bounded in-memory window of lifecycle steps.  The persistent record is
    # the EventLedger (emitted incrementally); this list is only the queue's
    # own replay-visible trace for :meth:`states`, so it is capped rather than
    # grown without bound over a long episode.
    EVENT_CAP = 4096

    def __init__(self, cooldown_ticks: int = 50, cooldown_wall: float = 5.0,
                 emergency_wall: float = 2.0, ledger=None, event_ledger=None,
                 clock=time.monotonic) -> None:
        self.cooldown_ticks = int(cooldown_ticks)
        self.cooldown_wall = float(cooldown_wall)
        self.emergency_wall = float(emergency_wall)
        self.ledger = ledger
        self.event_ledger = event_ledger
        self.clock = clock
        self._t0 = clock()
        self.tick = 0
        self.level = ""
        self.pending: Optional[PendingBoundary] = None
        self.in_flight: Optional[PendingBoundary] = None
        self.last_dispatch_tick: Optional[int] = None
        self.last_dispatch_wall: Optional[float] = None
        self.events: deque = deque(maxlen=self.EVENT_CAP)

    # -- intake ----------------------------------------------------------
    def submit(self, boundaries: Sequence[Boundary], tick: int,
               level: str = "") -> List[str]:
        """Coalesce *boundaries* into the pending set; return queued eids."""
        queued: List[str] = []
        self.tick = tick
        if level:
            self.level = level
        for b in boundaries:
            if self.pending is None:
                self.pending = PendingBoundary(tick=tick, level=level)
            self.pending.merge(b, tick)
            self.pending.level = level or self.pending.level
            queued.append(b.eid)
            self._log("queued", b.eid, b.reason)
        if queued and self.event_ledger is not None:
            # Stamp coalescing from the COMPLETE pending set, not just this
            # submission: a member merged into an already-pending request must
            # learn about the later arrival too, and every member must name
            # the same dispatched set.
            self.event_ledger.coalesce(list(self.pending.eids))
        return queued

    # -- dispatch --------------------------------------------------------
    def ready(self, tick: int, now: float) -> Optional[PendingBoundary]:
        """The pending set if it may be dispatched now, else None.

        Returns None while a call is in flight (one at a time) or while the
        cooldown is still running.  A severe pending set bypasses the normal
        cooldown but not the short emergency wall cooldown.
        """
        if self.in_flight is not None or self.pending is None:
            return None
        elapsed_ticks = (float("inf") if self.last_dispatch_tick is None
                         else tick - self.last_dispatch_tick)
        elapsed_wall = (float("inf") if self.last_dispatch_wall is None
                        else now - self.last_dispatch_wall)
        if self.pending.severe:
            if elapsed_wall >= self.emergency_wall:
                return self.pending
            return None
        if elapsed_ticks >= self.cooldown_ticks \
                and elapsed_wall >= self.cooldown_wall:
            return self.pending
        return None

    def mark_dispatched(self, tick: int, now: float) -> PendingBoundary:
        """Move the pending set to in-flight and start the cooldown clock."""
        self.in_flight = self.pending
        self.pending = None
        self.last_dispatch_tick = tick
        self.last_dispatch_wall = now
        self.tick = tick
        for eid in self.in_flight.eids:
            self._log("dispatched", eid, "dispatch")
        return self.in_flight

    def finish(self, applied: bool, reason: str = "strategy result") -> None:
        """Settle the in-flight set (applied) or discard it."""
        if self.in_flight is None:
            return
        state = "applied" if applied else "expired"
        for eid in self.in_flight.eids:
            self._log(state, eid, reason)
        self.in_flight = None

    # -- suppression / expiry -------------------------------------------
    def suppress(self, reason: str = "suppressed") -> List[str]:
        """Drop the pending set before dispatch; return the suppressed
        eids."""
        if self.pending is None:
            return []
        eids = list(self.pending.eids)
        for eid in eids:
            self._log("suppressed", eid, reason)
        self.pending = None
        return eids

    def expire(self, reason: str = "expired") -> None:
        """Discard pending and in-flight advice (e.g. the level changed)."""
        if self.pending is not None:
            for eid in self.pending.eids:
                self._log("expired", eid, reason)
            self.pending = None
        if self.in_flight is not None:
            for eid in self.in_flight.eids:
                self._log("expired", eid, reason)
            self.in_flight = None

    # -- bookkeeping -----------------------------------------------------
    def _log(self, state: str, eid: str, reason: str) -> None:
        self.events.append({"state": state, "eid": eid, "reason": reason,
                            "tick": self.tick, "level": self.level})
        if self.ledger is not None:
            self.ledger.note_boundary(state)
        if self.event_ledger is not None:
            self.event_ledger.transition(state, eid, reason, self.tick,
                                         self.level, self.clock() - self._t0)

    def states(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for e in self.events:
            out.setdefault(e["state"], []).append(e["eid"])
        return out
