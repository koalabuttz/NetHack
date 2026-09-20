"""Schema-versioned destination/pickup lifecycle metrics (plan section 5).

The live controller and the offline evaluator persist additive, schema-versioned
lifecycle *events*; this module derives the plan's measurement set from that
event stream.  Two properties are deliberate:

* everything is additive and versioned, so an older artifact simply lacks the
  fields; and
* a metric the stream does **not** evidence is reported as ``None``
  ("unavailable"), never ``0`` -- a legacy recording missing the new fields
  must not masquerade as a measured zero.
"""

import json
import os
from typing import Any, Dict, Iterable, List, Optional

#: The additive lifecycle schema version (plan §3).  Bumped to 2 so a record
#: carries ``schema_version`` inside its ``kind``-tagged payload; readers treat
#: a stream with no ``schema_version`` as legacy (metrics unavailable).
SCHEMA_VERSION = 2

# Destination lifecycle outcomes (plan section 5).
DEST_ACQUIRED = "acquired"
DEST_REPLACED = "replaced"
DEST_SUSPENDED = "suspended"
DEST_RESUMED = "resumed"
DEST_REACHED = "reached"
DEST_FAILED = "failed"
DEST_EXPIRED = "expired"
DEST_ACTION = "action"

# Pickup lifecycle outcomes.
PICKUP_OFFERED = "offered"
PICKUP_DECLINED = "declined"
PICKUP_ATTEMPTED = "attempted"
PICKUP_SUCCEEDED = "succeeded"
PICKUP_NO_ITEMS = "no-items"
PICKUP_CANCELED = "canceled"
PICKUP_REFUSED = "refused"
PICKUP_UNKNOWN = "unknown"

# Directive lifecycle outcomes.
DIR_ELIGIBLE = "eligible"
DIR_RESOLVED = "resolved"
DIR_FIRST_ACTION = "first-action"
DIR_TERMINAL = "terminal"

KIND_DESTINATION = "destination"
KIND_PICKUP = "pickup"
KIND_DIRECTIVE = "directive"

#: ``replaced`` is terminal for completeness accounting (plan §3) and is the
#: sole input to replacement switch pairing (``serial`` -> ``replacement_serial``).
_TERMINAL_DEST = (DEST_REACHED, DEST_FAILED, DEST_EXPIRED, DEST_REPLACED)


def _hashable(value):
    """A hashable view of a JSON-round-tripped (nested list) value."""
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    return value


def _percentile(values: List[int], q: float) -> Optional[int]:
    """The nearest-rank percentile of *values* (deterministic, no float noise)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = int(q * (len(ordered) - 1) + 0.5)
    return ordered[max(0, min(idx, len(ordered) - 1))]


class LifecycleRecorder(object):
    """An additive, schema-versioned lifecycle event stream.

    ``sink`` receives a copy of every event as it is recorded (the recording
    sidecar's writer); the in-memory list is a bounded replay-visible window,
    capped exactly like the boundary/directive ledgers.
    """

    EVENT_CAP = 4096

    def __init__(self, sink=None, cap: int = EVENT_CAP) -> None:
        self.sink = sink
        self.cap = int(cap)
        self.events: List[Dict[str, Any]] = []
        # Totals for the emitted cursor: ``recorded`` counts every event ever
        # recorded, ``emitted`` those already handed to a sink.  A sink that is
        # active during the episode emits incrementally, so the end-of-episode
        # drain (:meth:`drain_pending`) has nothing left to re-emit and a
        # >cap event stream is never truncated in the artifact.
        self.recorded = 0
        self.emitted = 0

    def record(self, kind: str, outcome: str, **fields: Any) -> None:
        # ``schema`` is the outer envelope version (retained); ``schema_version``
        # is the additive inside-payload version (plan §3).  No field is renamed.
        ev = {"schema": SCHEMA_VERSION, "schema_version": SCHEMA_VERSION,
              "kind": kind, "outcome": outcome}
        ev.update(fields)
        self.events.append(ev)
        self.recorded += 1
        if len(self.events) > self.cap:
            del self.events[:len(self.events) - self.cap]
        if self.sink is not None:
            self.sink(dict(ev))
            self.emitted = self.recorded

    def drain_pending(self) -> List[Dict[str, Any]]:
        """The events not yet handed to a sink, marking them emitted.

        Used by an end-of-episode drain when no incremental sink was active;
        with an active sink there is nothing pending and `[]` is returned, so
        the retained window is never replayed into the artifact (no
        duplicates).
        """
        pending = self.recorded - self.emitted
        if pending <= 0:
            return []
        out = [dict(e) for e in self.events[-pending:]]
        self.emitted = self.recorded
        return out

    def summarize(self) -> Dict[str, Any]:
        return summarize(self.events)


def load_events(records: Optional[Iterable[dict]]) -> List[dict]:
    """The lifecycle events carried by a produced artifact's event records.

    A record is a lifecycle event iff it carries the ``record:
    "lifecycle"`` envelope; every other record type is ignored.  An artifact
    produced before this field existed therefore yields an *empty* stream --
    every metric stays unavailable -- rather than a manufactured zero.
    """
    out = []
    for r in records or ():
        if isinstance(r, dict) and r.get("record") == "lifecycle":
            out.append({k: v for k, v in r.items() if k != "record"})
    return out


def summarize_artifact(path: str) -> Dict[str, Any]:
    """Derive the metrics from a produced ``ep-N.events.jsonl`` artifact.

    Reads the persisted event sidecar line by line; a missing file, an
    unreadable line or an artifact without lifecycle records all yield the
    "unavailable" summary (``None`` per metric), never a zero.
    """
    records: List[dict] = []
    if path and os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue                # a torn line is not a metric zero
                if isinstance(obj, dict):
                    records.append(obj)
    return summarize(load_events(records))


def summarize(events: Optional[Iterable[dict]]) -> Dict[str, Any]:
    """Derive the plan's lifecycle metrics, or ``None`` when unevidenced."""
    if events is None:
        events = []
    events = list(events)
    # A stream in which no record carries the additive ``schema_version`` is a
    # legacy (schema-only) stream: its lifecycle metrics are reported
    # *unavailable rather than zero*, and the flag makes that explicit.
    legacy_stream = bool(events) and not any(
        e.get("schema_version") is not None for e in events)
    if legacy_stream:
        events = []
    dest = [e for e in events if e.get("kind") == KIND_DESTINATION]
    pick = [e for e in events if e.get("kind") == KIND_PICKUP]
    direc = [e for e in events if e.get("kind") == KIND_DIRECTIVE]

    acquired = [e for e in dest if e.get("outcome") == DEST_ACQUIRED]
    terminal = [e for e in dest if e.get("outcome") in _TERMINAL_DEST]
    actions = [e for e in dest if e.get("outcome") == DEST_ACTION]
    switches = [e for e in dest if e.get("outcome") == DEST_REPLACED]

    # commitment length: actions per acquisition serial, in order
    lengths: List[int] = []
    if acquired and actions:
        counts: Dict[Any, int] = {}
        for e in acquired:
            counts.setdefault(e.get("serial"), 0)
        for e in actions:
            counts[e.get("serial")] = counts.get(e.get("serial"), 0) + 1
        lengths = [v for v in counts.values()]

    reasons: Dict[str, int] = {}
    for e in terminal:
        key = str(e.get("reason") or DEST_EXPIRED)
        reasons[key] = reasons.get(key, 0) + 1

    activated = {e.get("generation") for e in direc
                 if e.get("outcome") == DIR_ELIGIBLE}
    executed = {e.get("generation") for e in direc
                if e.get("outcome") == DIR_FIRST_ACTION}
    resolved = {e.get("generation") for e in direc
                if e.get("outcome") == DIR_RESOLVED}
    # The plan defines override execution over **eligible resolved**
    # generations: a generation that was eligible but never resolved must not
    # inflate the denominator, and eligible generations that never executed
    # (failed resolution or expired before action) are listed separately.
    eligible_resolved = activated & resolved
    executed_eligible = executed & eligible_resolved
    unresolved_or_expired = sorted(activated - executed)
    override_rate = (len(executed_eligible) / float(len(eligible_resolved))
                     if eligible_resolved else None)

    picked: Dict[str, int] = {}
    for e in pick:
        key = str(e.get("outcome"))
        picked[key] = picked.get(key, 0) + 1
    attempts_by_token: Dict[Any, int] = {}
    for e in pick:
        if e.get("outcome") == PICKUP_ATTEMPTED:
            tok = e.get("token")
            tok = _hashable(tok)              # JSON round-trip: lists -> tuples
            attempts_by_token[tok] = attempts_by_token.get(tok, 0) + 1

    # Terminal completeness (AC9): the share of acquired serials that carry at
    # least one terminal disposition -- a serial with no terminal is an
    # incomplete lifecycle, reported as such rather than silently dropped.
    acquired_serials = {e.get("serial") for e in acquired}
    terminated_serials = {e.get("serial") for e in terminal}
    completeness = (len(acquired_serials & terminated_serials)
                    / float(len(acquired_serials)) if acquired_serials else None)

    return {
        "schema_version": SCHEMA_VERSION,
        "legacy_stream": legacy_stream,
        "available": bool(dest or pick or direc),
        "terminal_completeness": completeness,
        # serviced-site reopens: a superseded serial's replacement is the one
        # destination record that evidences a re-acquisition of a serviced or
        # parked site (the site-level reopen rule lives in navigation).
        "serviced_reopens": len(switches) if dest else None,
        "commitment_length_median": (_percentile(lengths, 0.5)
                                     if lengths else None),
        "commitment_length_p90": (_percentile(lengths, 0.9)
                                  if lengths else None),
        "terminal_reasons": reasons if terminal else None,
        "destination_switch_rate": (len(switches) / float(len(actions))
                                    if actions else None),
        "directive_activations": len(activated) if direc else None,
        "directive_executions": len(executed_eligible) if direc else None,
        "directive_override_execution_rate": override_rate,
        "directive_unresolved_or_expired_before_action": (
            unresolved_or_expired if direc else None),
        "target_reach_rate": (len([e for e in terminal
                                   if e.get("outcome") == DEST_REACHED])
                              / float(len(acquired)) if acquired else None),
        "pickup_outcomes": picked if pick else None,
        "pickup_attempts": (sum(attempts_by_token.values())
                            if attempts_by_token else None),
        "pickup_repeated_sites": (len([t for t, n in attempts_by_token.items()
                                       if n > 1])
                                  if attempts_by_token else None),
        "pickup_unresolved_inspections": (picked.get(PICKUP_UNKNOWN)
                                          if pick else None),
    }


__all__ = [
    "SCHEMA_VERSION", "KIND_DESTINATION", "KIND_PICKUP", "KIND_DIRECTIVE",
    "DEST_ACQUIRED", "DEST_REPLACED", "DEST_SUSPENDED", "DEST_RESUMED",
    "DEST_REACHED", "DEST_FAILED", "DEST_EXPIRED", "DEST_ACTION",
    "PICKUP_OFFERED", "PICKUP_DECLINED", "PICKUP_ATTEMPTED", "PICKUP_SUCCEEDED",
    "PICKUP_NO_ITEMS", "PICKUP_CANCELED", "PICKUP_REFUSED", "PICKUP_UNKNOWN",
    "DIR_ELIGIBLE", "DIR_RESOLVED", "DIR_FIRST_ACTION", "DIR_TERMINAL",
    "LifecycleRecorder", "summarize", "load_events", "summarize_artifact",
]
