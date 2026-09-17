"""Per-episode call, token and cost accounting.

The ledger is the single authority for "may we spend another paid call?".
Three rules from ``doc/agent-autoplay-plan.md`` (section "Configuration,
budgets and security") are enforced here rather than at the call site:

  * **reserve before dispatch.**  A call is charged when it is *started*, so a
    request that times out with no returned usage is still billed -- the
    exposure is real and is never refunded merely because the provider
    answered nothing;
  * **reserve the conservative bound.**  A call is admitted only when every
    cap can still cover its per-request *upper bound* -- the estimated prompt
    plus the configured maximum output, priced with the tariff.  A request
    whose bound exceeds what remains is refused *before* a worker is spawned;
    the reported usage settles the true figure afterwards, and a call that
    returned no usage keeps its bound as *unknown exposure*;
  * **reserve the postmortem slot.**  The default strategy cap is 8 calls per
    episode, of which one is held back for the postmortem, so at most 7 are
    spent while playing.  When the remaining cap cannot conservatively cover
    one more call, paid dispatch is disabled for the rest of the episode.

Pricing is *operator-configured*.  No tariff is invented: when none is set,
no USD figure is asserted and the unknown-price exposure is counted instead.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Tariff(object):
    """Operator-supplied per-million-token prices (USD).

    Every field is ``Optional``: a *partial* configuration (only a prompt
    price, only a completion price, or only a cache-hit price) is *usable*
    for the components it prices but is explicitly *incomplete* --
    ``to_dict`` reports ``complete=False`` and the ledger never fabricates
    the absent component as a ``0.0`` rate.  A usage component with no
    configured price is priced as nothing and counted as unknown-priced
    instead, so a partial tariff can never masquerade as a numerically
    complete, zero-priced one.  Only a complete tariff (prompt *and*
    completion) can enforce a USD cap.

    ``cache_hit_per_mtok`` is appended last so existing
    two-positional-argument callers keep working.  When it is unset the
    effective cache-hit price falls back to the full input price, which is
    the conservative upper bound: a reservation that never discounts
    cached tokens can never under-count the spend.  A configured cache-hit
    price above a configured input price would break that invariant, so it
    is rejected rather than silently clamped (see :func:`_check_tariff`);
    with no configured input price there is nothing to compare against, so
    a cache-only tariff is accepted as incomplete rather than rejected as
    an invalid hit-above-zero rate.
    """

    prompt_per_mtok: Optional[float] = None
    completion_per_mtok: Optional[float] = None
    cache_hit_per_mtok: Optional[float] = None

    def is_complete(self) -> bool:
        """True only with *both* a prompt and a completion price configured.

        Completeness is what a USD cap requires; the cache-hit price is
        deliberately not part of it (the full input price is the conservative
        fallback), and neither is a partial one-sided configuration.
        """
        return (self.prompt_per_mtok is not None
                and self.completion_per_mtok is not None)

    def effective_cache_hit_per_mtok(self) -> Optional[float]:
        """The cache-hit price actually applied: configured, else input."""
        if self.cache_hit_per_mtok is not None:
            return self.cache_hit_per_mtok
        return self.prompt_per_mtok

    def to_dict(self) -> Dict[str, Any]:
        return {"prompt_per_mtok": self.prompt_per_mtok,
                "completion_per_mtok": self.completion_per_mtok,
                "cache_hit_per_mtok": self.cache_hit_per_mtok,
                "effective_cache_hit_per_mtok":
                    self.effective_cache_hit_per_mtok(),
                "complete": self.is_complete()}


def _finite_number(name: str, v) -> float:
    """Return *v* as a float, raising for a bool, NaN or +/-inf value."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("%s must be a number (got %r)" % (name, v))
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("%s must be finite (got %r)" % (name, v))
    return f


def _int_arg(name: str, v) -> int:
    """Return *v* as an int, raising for a non-finite/non-integral value."""
    f = _finite_number(name, v)
    if f != int(f):
        raise ValueError("%s must be an integer (got %r)" % (name, v))
    return int(f)


def _nonneg_int(name: str, v) -> int:
    """Return *v* as a nonnegative int, raising rather than clamping.

    A negative cap would change the *meaning* of the enforcement rather than
    merely its size (``token_cap=-1`` refuses every call; a negative style
    bound can never be covered), so the ledger rejects it loudly instead of
    silently rewriting the operator's intent.
    """
    n = _int_arg(name, v)
    if n < 0:
        raise ValueError("%s must be nonnegative (got %r)" % (name, v))
    return n


def _check_tariff(tariff) -> None:
    """Reject a non-finite, negative or inconsistent *present* tariff price.

    A *partial* tariff -- a component left unset -- is deliberately allowed to
    construct: it is *incomplete* (see :meth:`Tariff.is_complete`), cannot
    enforce a USD cap, but is still usable for the components it does price.
    The absent component is never fabricated as a ``0.0`` rate; the ledger
    routes it to unknown exposure instead, so a partial tariff cannot
    present a numerically complete, zero-priced total.

    Every *present* price must be finite and nonnegative.  The cache-hit
    price must not exceed a *configured* input price, because a cache-hit
    price above it would let a discount inflate a reservation -- the
    operator is told rather than silently clamped.  With no configured input
    price there is nothing to compare against, so a cache-only tariff is not
    rejected here.
    """
    for field in ("prompt_per_mtok", "completion_per_mtok",
                  "cache_hit_per_mtok"):
        value = getattr(tariff, field, None)
        if value is None:
            continue
        if _finite_number("tariff.%s" % field, value) < 0:
            raise ValueError("tariff.%s must be nonnegative" % field)
    hit = getattr(tariff, "cache_hit_per_mtok", None)
    prompt = getattr(tariff, "prompt_per_mtok", None)
    if hit is None or prompt is None:
        return
    if _finite_number("tariff.cache_hit_per_mtok", hit) \
            > _finite_number("tariff.prompt_per_mtok", prompt):
        raise ValueError(
            "tariff.cache_hit_per_mtok (%r) must not exceed "
            "tariff.prompt_per_mtok (%r): the full input price is the "
            "conservative fallback that keeps every reservation an upper "
            "bound" % (hit, prompt))


class BudgetLedger(object):
    """One episode's counters, reservations and estimated cost."""

    def __init__(self, strategy_cap: int = 8, postmortem_reserve: int = 1,
                 usd_cap: Optional[float] = None,
                 tariff: Optional[Tariff] = None,
                 reflex_cap: int = 0, token_cap: int = 0) -> None:
        self.strategy_cap = _nonneg_int("strategy_cap", strategy_cap)
        # A *negative* reserve would silently enlarge the play budget
        # (cap - reserve), so it is clamped here as well as rejected at the
        # CLI: the ledger never trusts its own inputs.  (This is the one
        # documented clamp; every other invalid figure is rejected.)
        self.postmortem_reserve = max(0, _int_arg("postmortem_reserve",
                                                  postmortem_reserve))
        self._check_usd_cap(usd_cap, tariff)
        self.usd_cap = usd_cap
        self.tariff = tariff
        self.reflex_cap = _nonneg_int("reflex_cap", reflex_cap)
        self.token_cap = _nonneg_int("token_cap", token_cap)
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
        # -- reported prompt-cache accounting ------------------------------
        # A discount is granted only for a *complete, consistent* cache
        # partition (H + M == P).  Anything else -- no cache metadata, a
        # partial or contradictory report, a negative/bool/float field -- is
        # priced at the full input price and counted as unclassified, so a
        # high hit rate over thin reporting coverage cannot mislead.
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.cache_unclassified_tokens = 0
        self.reasoning_tokens = 0
        # Exposure from committed calls that returned *no usage at all*: the
        # conservative bound is carried rather than dropped.
        self._reserved_bounds: List[Tuple[int, int]] = []
        self.unknown_exposure_calls = 0
        self.unknown_prompt_tokens = 0
        self.unknown_completion_tokens = 0
        self.unknown_estimated_usd = 0.0

    # -- defensive invariants --------------------------------------------
    @staticmethod
    def _check_usd_cap(usd_cap, tariff) -> None:
        """Refuse an unenforceable USD cap at the ledger boundary.

        A USD cap is enforceable only against a complete, valid tariff: with
        no tariff (or a half/invalid one) every USD figure is an
        under-estimate, so the cap would be *silently ignored* here -- the
        exact failure the CLI rejects.  A ledger built directly must refuse
        the combination rather than pretend to enforce it.
        """
        if tariff is not None:
            _check_tariff(tariff)
        if usd_cap is None:
            return
        if _finite_number("usd_cap", usd_cap) < 0:
            raise ValueError("usd_cap must be nonnegative")
        if tariff is None:
            raise ValueError("a usd_cap requires a configured tariff")
        if getattr(tariff, "prompt_per_mtok", None) is None \
                or getattr(tariff, "completion_per_mtok", None) is None:
            raise ValueError("a usd_cap requires a complete tariff")

    @staticmethod
    def _bound(prompt_tokens, completion_tokens) -> Tuple[int, int]:
        """Validate a conservative (prompt, completion) bound.

        A negative or NaN bound would change the meaning of the cap check --
        a negative bound can never be covered, a NaN bound silently fails
        every comparison -- so it is rejected loudly rather than clamped into
        a smaller (and wrong) reserve.
        """
        return (_nonneg_int("prompt_tokens", prompt_tokens),
                _nonneg_int("completion_tokens", completion_tokens))

    # -- boundaries ------------------------------------------------------
    def note_boundary(self, state: str, n: int = 1) -> None:
        """Increment one boundary-state counter (``detected``, ...)."""
        attr = "boundaries_%s" % state
        if not hasattr(self, attr):
            raise ValueError("unknown boundary state %r" % (state,))
        setattr(self, attr, getattr(self, attr) + int(n))

    # -- strategy reservations ------------------------------------------
    def _price(self, prompt_tokens: int, completion_tokens: int) -> float:
        """The USD cost of a token count under the configured tariff.

        Only *configured* components are priced: an absent price is not
        fabricated as zero, so a partial tariff simply leaves that side of
        the spend unrepresented here.  The settled-cost and
        unknown-exposure callers route the unpriced component to
        unknown-priced accounting instead, so a partial tariff never looks
        numerically complete.
        """
        if self.tariff is None:
            return 0.0
        total = 0.0
        if self.tariff.prompt_per_mtok is not None:
            total += prompt_tokens / 1000000.0 * self.tariff.prompt_per_mtok
        if self.tariff.completion_per_mtok is not None:
            total += completion_tokens / 1000000.0 \
                * self.tariff.completion_per_mtok
        return total

    @property
    def unknown_exposure_tokens(self) -> int:
        """Total tokens carried as unknown exposure (no usage returned)."""
        return self.unknown_prompt_tokens + self.unknown_completion_tokens

    def _effective_tokens(self) -> int:
        """Tokens already billed, carried as unknown, or still reserved."""
        reserved = sum(p + c for p, c in self._reserved_bounds)
        return (self.prompt_tokens + self.completion_tokens
                + self.unknown_prompt_tokens + self.unknown_completion_tokens
                + reserved)

    def _effective_usd(self) -> float:
        """USD already billed, carried as unknown, or still reserved."""
        reserved = sum(self._price(p, c) for p, c in self._reserved_bounds)
        return self.estimated_usd + self.unknown_estimated_usd + reserved

    def cache_hit_rate(self) -> Optional[float]:
        """Hit rate over *classified* tokens, or None when none are.

        Measured as ``H / (H + M)``: unclassified prompt tokens are excluded
        rather than fabricated as misses, so a high rate over thin reporting
        coverage is not mistaken for a high rate over the whole prompt.  The
        companion ``cache_unclassified_tokens`` field carries that coverage.
        """
        denom = self.cache_hit_tokens + self.cache_miss_tokens
        if denom <= 0:
            return None
        return round(self.cache_hit_tokens / float(denom), 6)

    def strategy_available(self, postmortem: bool = False,
                           prompt_tokens: int = 0,
                           completion_tokens: int = 0) -> bool:
        """True when one more paid strategy call may conservatively start.

        ``prompt_tokens``/``completion_tokens`` are the *conservative upper
        bound* of the call under consideration.  Every cap -- call count,
        tokens and USD -- must be able to cover that bound out of what is
        still left, not merely out of the totals reported so far.
        """
        prompt_tokens, completion_tokens = self._bound(prompt_tokens,
                                                       completion_tokens)
        spent = self.strategy_dispatched + self.strategy_reserved
        if postmortem:
            budget = self.strategy_cap
        else:
            # hold the postmortem slot back while playing
            budget = max(0, self.strategy_cap - self.postmortem_reserve)
        if spent >= budget:
            return False
        if self.usd_cap is not None and self.tariff is not None:
            bound = self._price(prompt_tokens, completion_tokens)
            if self._effective_usd() + bound > self.usd_cap:
                return False
        if self.token_cap:
            bound = int(prompt_tokens) + int(completion_tokens)
            if self._effective_tokens() + bound > self.token_cap:
                return False
        return True

    def reserve_strategy(self, postmortem: bool = False,
                         prompt_tokens: int = 0,
                         completion_tokens: int = 0) -> bool:
        """Charge one strategy call and its conservative bound up front.

        Returns False -- charging nothing -- when the cap is spent or the
        bound cannot be covered by the remainder, so a request that could not
        conservatively fit is refused *before* any worker is spawned.
        """
        if not self.strategy_available(
                postmortem=postmortem, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens):
            return False
        self.strategy_reserved += 1
        self._reserved_bounds.append((int(prompt_tokens),
                                      int(completion_tokens)))
        return True

    def commit_strategy(self, usage: Optional[Dict[str, Any]] = None,
                        postmortem: bool = False) -> None:
        """Settle a reserved call.

        The reservation was made before dispatch, so this always consumes it
        -- including a timeout that returned no usage.  Reported tokens are
        added and, when a tariff is configured, priced.  A call whose reported
        usage does not establish *both* prompt and completion totals retains
        the conservative bound as *unknown exposure* for whichever component
        is missing: the spend was real even though no figure came back for it,
        so it is never silently dropped merely because some other field
        arrived.
        """
        if self.strategy_reserved > 0:
            self.strategy_reserved -= 1
        bound = self._reserved_bounds.pop(0) if self._reserved_bounds \
            else (0, 0)
        self.strategy_dispatched += 1
        if postmortem:
            self.postmortem_dispatched += 1
        prompt_known, completion_known = self._settle_reported(usage)
        if not (prompt_known and completion_known):
            # carry the bound for the missing component(s) only; a component
            # that *was* reported is already billed above
            self._note_unknown_exposure(
                (0 if prompt_known else bound[0],
                 0 if completion_known else bound[1]))

    def release_strategy(self) -> None:
        """Drop a reservation that never reached the wire.

        Only for a call that was *reserved but never dispatched* (for
        example a reserve that succeeded and then failed local validation
        before any process was spawned).  A dispatched-but-failed call must
        settle with :meth:`commit_strategy` instead.
        """
        if self.strategy_reserved > 0:
            self.strategy_reserved -= 1
        if self._reserved_bounds:
            self._reserved_bounds.pop(0)

    def _note_unknown_exposure(self, bound) -> None:
        prompt, completion = bound
        self.unknown_prompt_tokens += int(prompt)
        self.unknown_completion_tokens += int(completion)
        self.unknown_estimated_usd += self._price(prompt, completion)
        self.unknown_exposure_calls += 1

    def add_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        """Record reported usage (wraps :meth:`_settle_reported`).

        Kept as the public entry point for callers that only have usage to
        report (a reflex answer); settling a *reserved* strategy call goes
        through :meth:`commit_strategy`, which also carries any missing
        component's bound as unknown exposure.
        """
        self._settle_reported(usage)

    def _settle_reported(self, usage: Optional[Dict[str, Any]]) \
            -> Tuple[bool, bool]:
        """Add reported usage; return ``(prompt_known, completion_known)``.

        Every cache field is re-normalized here as well as at the provider, so
        a fake or programmatic provider cannot smuggle a negative, floating,
        boolean or contradictory figure in to reduce the billed exposure.  A
        reported *zero* is genuinely known and distinct from a missing field;
        only a complete consistent partition (``H + M == P``) earns the cache
        discount, and everything else is priced at the full input price and
        counted as unclassified.  A component the configured tariff does not
        price is counted as unknown-priced rather than billed as a fabricated
        zero, so a partial tariff cannot present a complete-looking total.
        """
        if not usage:
            return (False, False)
        prompt = _opt_int(usage.get("prompt_tokens"))
        completion = _opt_int(usage.get("completion_tokens"))
        hit = _opt_int(usage.get("prompt_cache_hit_tokens"))
        miss = _opt_int(usage.get("prompt_cache_miss_tokens"))
        reasoning = _opt_int(usage.get("reasoning_tokens"))
        # derive the aggregate prompt from a complete cache partition when the
        # aggregate is missing but both parts are valid
        if prompt is None and hit is not None and miss is not None:
            prompt = hit + miss
        classified_hit = 0
        if prompt is not None:
            if hit is not None and miss is not None and hit + miss == prompt:
                classified_hit = hit
                self.cache_hit_tokens += hit
                self.cache_miss_tokens += miss
            else:
                self.cache_unclassified_tokens += prompt
            self.prompt_tokens += prompt
        if completion is not None:
            self.completion_tokens += completion
        if reasoning is not None:
            # a diagnostic subset of completion_tokens, never billed twice
            self.reasoning_tokens += reasoning
        if prompt is not None or completion is not None \
                or usage.get("reported"):
            self.estimated_usd += self._reported_price(
                classified_hit, prompt or 0, completion or 0)
            if self._has_unpriced_component(prompt, completion):
                # A reported component with no configured price is never
                # priced as a fabricated zero: the call is counted as
                # unknown-priced so a partial tariff never yields a
                # complete-looking total.
                self.unknown_price_calls += 1
        return (prompt is not None, completion is not None)

    def _has_unpriced_component(self, prompt: Optional[int],
                                completion: Optional[int]) -> bool:
        """True when a *reported* component has no configured price.

        With no tariff at all every reported call is unknown-priced.  With a
        partial tariff only the reported component that lacks a price counts;
        an absent *usage* component (``None``) is not a pricing gap here --
        the reservation path already carries its bound as unknown exposure.
        """
        if self.tariff is None:
            return True
        if prompt is not None and self.tariff.prompt_per_mtok is None:
            return True
        if completion is not None \
                and self.tariff.completion_per_mtok is None:
            return True
        return False

    def _reported_price(self, hit_tokens: int, prompt_tokens: int,
                        completion_tokens: int) -> float:
        """Cache-aware USD cost of one *reported* call.

        Cached prompt tokens are billed at the effective cache-hit price, the
        rest of the prompt (misses and unclassified) at the full input price,
        and completion -- which the API already includes reasoning in -- at
        the output price.  Only *configured* components are priced: an absent
        prompt or completion price contributes nothing here and the caller
        counts the call as unknown-priced, so a partial tariff never yields
        a complete-looking total.  This is deliberately a *separate*
        function from :meth:`_price`, which stays the conservative
        full-price figure used for reservations and unknown exposure.
        """
        if self.tariff is None:
            return 0.0
        total = 0.0
        # Known cache-hit tokens are priced whenever a hit rate is
        # available -- configured cache price, or the input price as the
        # documented fallback -- even when the rest of the prompt has no
        # configured rate (a cache-only tariff prices its one known
        # component and leaves misses unknown-priced).
        hit_rate = self.tariff.effective_cache_hit_per_mtok()
        if hit_rate is not None:
            total += hit_tokens / 1000000.0 * hit_rate
        prompt_rate = self.tariff.prompt_per_mtok
        if prompt_rate is not None:
            missed = max(0, prompt_tokens - hit_tokens)
            total += missed / 1000000.0 * prompt_rate
        if self.tariff.completion_per_mtok is not None:
            total += completion_tokens / 1000000.0 \
                * self.tariff.completion_per_mtok
        return total

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
                "unknown_exposure_calls": self.unknown_exposure_calls,
                "unknown_exposure_tokens": (self.unknown_prompt_tokens
                                            + self.unknown_completion_tokens),
                "unknown_exposure_usd": round(self.unknown_estimated_usd, 6),
                "cache_hit_tokens": self.cache_hit_tokens,
                "cache_miss_tokens": self.cache_miss_tokens,
                "cache_unclassified_tokens": self.cache_unclassified_tokens,
                "cache_hit_rate": self.cache_hit_rate(),
                "reasoning_tokens": self.reasoning_tokens,
                "reserved_bounds": [list(b) for b in self._reserved_bounds],
                "tariff": self.tariff.to_dict() if self.tariff else None,
                "usd_cap": self.usd_cap,
                "token_cap": self.token_cap,
            },
        }
        return out


def _opt_int(v) -> Optional[int]:
    """A nonnegative, non-bool integer, or None for anything else.

    Returning ``None`` (rather than coercing to 0) is what keeps a missing
    field distinct from a reported zero, and keeps a negative, floating or
    boolean figure from silently *reducing* the billed exposure.
    """
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if v < 0:
        return None
    return int(v)
