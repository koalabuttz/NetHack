"""Per-episode call, token and cost accounting.

The ledger is the single authority for "may we spend another paid call?".
Two rules from ``doc/agent-autoplay-plan.md`` (section "Configuration,
budgets and security") are enforced here rather than at the call site:

  * **reserve before dispatch.**  A call is charged when it is *started*, so a
    request that times out with no returned usage is still billed -- the
    exposure is real and is never refunded merely because the provider
    answered nothing;
  * **reserve the postmortem slot.**  The default strategy cap is 8 calls per
    episode, of which one is held back for the postmortem, so at most 7 are
    spent while playing.  When the remaining cap cannot conservatively cover
    one more call, paid dispatch is disabled for the rest of the episode.

Pricing is *operator-configured*.  No tariff is invented: when none is set,
no USD figure is asserted and the unknown-price exposure is counted instead.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Tariff(object):
    """Operator-supplied per-million-token prices (USD)."""

    prompt_per_mtok: float
    completion_per_mtok: float

    def to_dict(self) -> Dict[str, float]:
        return {"prompt_per_mtok": self.prompt_per_mtok,
                "completion_per_mtok": self.completion_per_mtok}


class BudgetLedger(object):
    """One episode's counters, reservations and estimated cost."""

    def __init__(self, strategy_cap: int = 8, postmortem_reserve: int = 1,
                 usd_cap: Optional[float] = None,
                 tariff: Optional[Tariff] = None,
                 reflex_cap: int = 0, token_cap: int = 0) -> None:
        self.strategy_cap = int(strategy_cap)
        self.postmortem_reserve = int(postmortem_reserve)
        self.usd_cap = usd_cap
        self.tariff = tariff
        self.reflex_cap = int(reflex_cap)
        self.token_cap = int(token_cap)
        # -- reflex counters --------------------------------------------
        self.reflex_attempted = 0
        self.reflex_successful = 0
        self.reflex_timeout = 0
        self.reflex_invalid = 0
        self.reflex_low_confidence = 0
        self.reflex_fallback = 0
        self.reflex_paid_dispatched = 0
        # -- boundary counters ------------------------------------------
        self.boundaries_detected = 0
        self.boundaries_queued = 0
        self.boundaries_dispatched = 0
        self.boundaries_suppressed = 0
        self.boundaries_expired = 0
        self.boundaries_applied = 0
        # -- usage -------------------------------------------------------
        self.strategy_dispatched = 0
        self.strategy_reserved = 0
        self.postmortem_dispatched = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_usd = 0.0
        self.unknown_price_calls = 0

    # -- boundaries ------------------------------------------------------
    def note_boundary(self, state: str, n: int = 1) -> None:
        """Increment one boundary-state counter (``detected``, ...)."""
        attr = "boundaries_%s" % state
        if not hasattr(self, attr):
            raise ValueError("unknown boundary state %r" % (state,))
        setattr(self, attr, getattr(self, attr) + int(n))

    # -- strategy reservations ------------------------------------------
    def strategy_available(self, postmortem: bool = False) -> bool:
        """True when one more paid strategy call may conservatively start."""
        spent = self.strategy_dispatched + self.strategy_reserved
        if postmortem:
            budget = self.strategy_cap
        else:
            # hold the postmortem slot back while playing
            budget = max(0, self.strategy_cap - self.postmortem_reserve)
        if spent >= budget:
            return False
        if self.usd_cap is not None and self.tariff is not None \
                and self.estimated_usd >= self.usd_cap:
            return False
        if self.token_cap and \
                (self.prompt_tokens + self.completion_tokens) \
                >= self.token_cap:
            return False
        return True

    def reserve_strategy(self, postmortem: bool = False) -> bool:
        """Charge one strategy call up front; False when the cap is spent."""
        if not self.strategy_available(postmortem=postmortem):
            return False
        self.strategy_reserved += 1
        return True

    def commit_strategy(self, usage: Optional[Dict[str, Any]] = None,
                        postmortem: bool = False) -> None:
        """Settle a reserved call.

        The reservation was made before dispatch, so this always consumes it
        -- including a timeout that returned no usage.  Reported tokens are
        added and, when a tariff is configured, priced.
        """
        if self.strategy_reserved > 0:
            self.strategy_reserved -= 1
        self.strategy_dispatched += 1
        if postmortem:
            self.postmortem_dispatched += 1
        self.add_usage(usage)

    def release_strategy(self) -> None:
        """Drop a reservation that never reached the wire.

        Only for a call that was *reserved but never dispatched* (for
        example a reserve that succeeded and then failed local validation
        before any process was spawned).  A dispatched-but-failed call must
        settle with :meth:`commit_strategy` instead.
        """
        if self.strategy_reserved > 0:
            self.strategy_reserved -= 1

    def add_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        if not usage:
            return
        prompt = _num(usage.get("prompt_tokens"))
        completion = _num(usage.get("completion_tokens"))
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        if prompt or completion or usage.get("reported"):
            if self.tariff is not None:
                self.estimated_usd += (
                    prompt / 1000000.0 * self.tariff.prompt_per_mtok
                    + completion / 1000000.0
                    * self.tariff.completion_per_mtok)
            else:
                self.unknown_price_calls += 1

    # -- reflex paid bound (Jev) ----------------------------------------
    def reflex_paid_available(self) -> bool:
        if self.reflex_cap <= 0:
            return False
        return self.reflex_paid_dispatched < self.reflex_cap

    def reserve_reflex_paid(self) -> bool:
        if not self.reflex_paid_available():
            return False
        self.reflex_paid_dispatched += 1
        return True

    # -- reporting -------------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        out = {
            "reflex": {
                "attempted": self.reflex_attempted,
                "successful": self.reflex_successful,
                "timeout": self.reflex_timeout,
                "invalid": self.reflex_invalid,
                "low_confidence": self.reflex_low_confidence,
                "fallback": self.reflex_fallback,
                "paid_dispatched": self.reflex_paid_dispatched,
            },
            "boundaries": {
                "detected": self.boundaries_detected,
                "queued": self.boundaries_queued,
                "dispatched": self.boundaries_dispatched,
                "suppressed": self.boundaries_suppressed,
                "expired": self.boundaries_expired,
                "applied": self.boundaries_applied,
            },
            "strategy": {
                "cap": self.strategy_cap,
                "postmortem_reserve": self.postmortem_reserve,
                "dispatched": self.strategy_dispatched,
                "reserved": self.strategy_reserved,
                "postmortem_dispatched": self.postmortem_dispatched,
            },
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "estimated_usd": round(self.estimated_usd, 6),
                "unknown_price_calls": self.unknown_price_calls,
                "tariff": self.tariff.to_dict() if self.tariff else None,
                "usd_cap": self.usd_cap,
                "token_cap": self.token_cap,
            },
        }
        return out


def _num(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if v != v or v in (float("inf"), float("-inf")):
        return 0
    return int(v)
