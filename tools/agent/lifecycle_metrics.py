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

from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

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

_TERMINAL_DEST = (DEST_REACHED, DEST_FAILED, DEST_EXPIRED)


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
    """An additive, schema-versioned lifecycle event stream."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def record(self, kind: str, outcome: str, **fields: Any) -> None:
        ev = {"schema": SCHEMA_VERSION, "kind": kind, "outcome": outcome}
        ev.update(fields)
        self.events.append(ev)

    def summarize(self) -> Dict[str, Any]:
        return summarize(self.events)


def summarize(events: Optional[Iterable[dict]]) -> Dict[str, Any]:
    """Derive the plan's lifecycle metrics, or ``None`` when unevidenced."""
    if events is None:
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

    activated = [e for e in direc if e.get("outcome") == DIR_ELIGIBLE]
    executed = {e.get("generation") for e in direc
                if e.get("outcome") == DIR_FIRST_ACTION}
    resolved = {e.get("generation") for e in direc
                if e.get("outcome") == DIR_RESOLVED}
    unresolved = sorted(g for g in resolved if g not in executed)

    picked: Dict[str, int] = {}
    for e in pick:
        key = str(e.get("outcome"))
        picked[key] = picked.get(key, 0) + 1
    attempts_by_token: Dict[Any, int] = {}
    for e in pick:
        if e.get("outcome") == PICKUP_ATTEMPTED:
            tok = e.get("token")
            attempts_by_token[tok] = attempts_by_token.get(tok, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "available": bool(dest or pick or direc),
        "commitment_length_median": (_percentile(lengths, 0.5)
                                     if lengths else None),
        "commitment_length_p90": (_percentile(lengths, 0.9)
                                  if lengths else None),
        "terminal_reasons": reasons if terminal else None,
        "destination_switch_rate": (len(switches) / float(len(actions))
                                    if actions else None),
        "directive_activations": len(activated) if direc else None,
        "directive_executions": (len(executed) if direc else None),
        "directive_override_execution_rate": (
            len(executed) / float(len(activated)) if activated else None),
        "directive_unresolved_or_expired_before_action": (
            unresolved if direc else None),
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
    "LifecycleRecorder", "summarize",
]
