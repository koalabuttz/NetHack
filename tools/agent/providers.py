"""Tier contracts and (stubbed) provider adapters.

Wave 1 is scripted-only: the default configuration makes **zero** network
calls by construction.  This module fixes the interfaces a later wave fills in
so the wire path never has to change:

    Provider.available(config) -> Availability(enabled, reason)
    ReflexProvider.decide(context, deadline) -> ReflexResult
    StrategyProvider.deliberate(context, deadline) -> StrategyResult
    ScriptedReflex.fallback(context) -> ReflexResult          # in policy.py

The DeepSeek and Jev adapters here are deliberately *disabled placeholders*:
they advertise themselves unavailable and perform no I/O.  Real activation
(worker supervision, socket timeouts, secret handling, budget reservation)
belongs to Wave 2 and its own review; nothing in this file opens a socket.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import protocol, state


@dataclass(frozen=True)
class Availability(object):
    enabled: bool
    reason: str = ""


@dataclass
class ProviderConfig(object):
    reflex: str = "scripted"          # scripted | jev
    strategy: str = "off"             # off | deepseek
    role: str = "Valkyrie"
    max_ticks: int = 2000
    confidence_threshold: float = 0.8
    strategy_call_cap: int = 8
    deepseek_model: str = "deepseek-v4.1-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_key_file: Optional[str] = None
    jev_key_file: Optional[str] = None
    reflex_deadline: float = 0.75
    answer_deadline: float = 1.0
    content_deadline: float = 5.0
    strategy_deadline: float = 20.0


@dataclass
class ReflexContext(object):
    episode: int
    tick: int
    need: dict
    need_key: protocol.NeedKey
    snapshot: protocol.Snapshot
    pages: List[Any]
    memory: state.EpisodeMemory
    intent: str = ""
    directives: List[Any] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)
    deadline: float = 0.0


@dataclass
class ReflexResult(object):
    action: Optional[dict]
    confidence: Optional[float] = None
    provider: str = "scripted"
    reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0


@dataclass
class StrategyContext(object):
    episode: int
    tick: int
    summary: Dict[str, Any] = field(default_factory=dict)
    boundaries: List[Any] = field(default_factory=list)
    map_text: str = ""
    status_text: str = ""
    recent_messages: List[str] = field(default_factory=list)
    inventory: List[Any] = field(default_factory=list)
    history: List[Any] = field(default_factory=list)
    goals: List[str] = field(default_factory=list)
    remaining_budget: int = 0


@dataclass
class StrategyResult(object):
    directives: List[Any] = field(default_factory=list)
    provider: str = "off"
    usage: Dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0
    reason: str = ""


class Provider(object):
    name = "provider"

    def available(self, config: ProviderConfig) -> Availability:
        return Availability(False, "not implemented")


class ReflexProvider(Provider):
    def decide(self, context: ReflexContext, deadline: float) -> \
            Optional[ReflexResult]:
        return None

    def fallback(self, context: ReflexContext) -> Optional[ReflexResult]:
        return None


class StrategyProvider(Provider):
    def deliberate(self, context: StrategyContext, deadline: float) -> \
            Optional[StrategyResult]:
        return None


class NullStrategy(StrategyProvider):
    """The strategy interface with a disabled/no-op result.

    The reflection also serves as the ``ScriptedReflex.fallback`` shim a later
    wave composes with.
    """

    name = "off"

    def available(self, config: ProviderConfig) -> Availability:
        return Availability(False, "strategy disabled")

    def deliberate(self, context: StrategyContext, deadline: float) -> \
            Optional[StrategyResult]:
        return StrategyResult(provider="off", reason="strategy disabled")


class ScriptedReflexProvider(ReflexProvider):
    """The always-available scripted tier (decisions live in policy.py)."""

    name = "scripted"

    def available(self, config: ProviderConfig) -> Availability:
        if config.reflex == "scripted":
            return Availability(True, "scripted reflex is always available")
        return Availability(False, "reflex tier is %s" % config.reflex)


class JevReflex(ReflexProvider):
    """Placeholder.  JevReflex ships DISABLED pending official API terms."""

    name = "jev"

    def available(self, config: ProviderConfig) -> Availability:
        if config.reflex != "jev":
            return Availability(False, "reflex tier is scripted")
        return Availability(False, "Jev API contract not available yet")

    def decide(self, context: ReflexContext, deadline: float) -> \
            Optional[ReflexResult]:
        return None


class DeepSeekStrategy(StrategyProvider):
    """Placeholder.  No socket is opened; real activation is Wave 2."""

    name = "deepseek"

    def available(self, config: ProviderConfig) -> Availability:
        if config.strategy != "deepseek":
            return Availability(False, "strategy tier is off")
        return Availability(False, "DeepSeek adapter not implemented yet")

    def deliberate(self, context: StrategyContext, deadline: float) -> \
            Optional[StrategyResult]:
        return None


def reflex_provider(config: ProviderConfig) -> ReflexProvider:
    if config.reflex == "jev":
        return JevReflex()
    return ScriptedReflexProvider()


def strategy_provider(config: ProviderConfig) -> StrategyProvider:
    if config.strategy == "deepseek":
        return DeepSeekStrategy()
    return NullStrategy()
