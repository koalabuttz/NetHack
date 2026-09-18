"""Immutable candidate tables and canonical identity (neutral leaf).

This module is the dependency-neutral leaf of the reflex upgrade
(``doc/agent-reflex-upgrade-plan.md`` sections 3.1-3.3).  It imports the
standard library and *nothing else* -- not ``policy``, not ``providers``,
not even the sibling ``protocol`` module -- so any layer may depend on it
without creating a cycle.  It holds:

  * an immutable tagged action model that mirrors the wire action shapes;
  * a versioned, deterministic canonical encoding and content-addressed
    candidate/table identities;
  * deterministic deduplication, ordering and truncation of a candidate
    set into at most :data:`MAX_CANDIDATES` entries;
  * neutral immutable lifecycle records (features, prepared table, sent
    attempt) with no memory mutation, navigation or provider behaviour.

Determinism rules (section 3.2): explicit ordered records, sorted object
keys, stable UTF-8 JSON separators, no NaN/Infinity, integer scores, and no
wall clock, Python ``hash`` or mutable RNG anywhere.  A table is
canonicalized **once**; the exact retained bytes are carried on the table
and are what later hashing, fake-Jev payload construction, telemetry and
replay comparison reuse.  Re-serializing a retained table is a defect --
:func:`canonicalize_count` exists so a test can prove it did not happen.
"""

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Versioned identity: bump when the canonical body shape changes, so an old
# table can never be mistaken for a new one.
CANDIDATE_SCHEMA_VERSION = 1

# One deterministic, immutable table of at most 255 candidates (3.1).
MAX_CANDIDATES = 255

# Action tags mirror the wire shapes in ``protocol.validate_action``.
ACTION_TAGS = ("ack", "cancel", "key", "menu", "position", "text", "yn")

# Lifecycle terminal categories (section 3.4).  ``observed`` is the only
# category that credits a gameplay outcome, and it carries one of the
# observed sub-kinds; ``rejected`` and ``discarded-no-observation`` commit no
# gameplay effect; ``delivery-repair`` is the sole non-gameplay repair exit
# (``incomplete``), never a gameplay rejection.
TERMINAL_STATES = ("observed", "rejected", "discarded-no-observation",
                   "delivery-repair")
OBSERVED_KINDS = ("moved", "stationary-time-advanced", "no-time",
                  "prompt-opened", "unknown")

# Ordered candidate families.  The rank breaks equal scores explicitly and
# deterministically (section 3.2/3.3), so two candidates that tie on score
# still order the same way on every process, every run.
FAMILY_ORDER = (
    "emergency",        # low-HP disengagement / escape
    "descend",          # standing on a known down staircase
    "stair",            # reachable down staircase
    "door",             # cardinal closed-door approach (open it)
    "frontier",         # known-safe observation frontier
    "unvisited",        # unvisited known cell
    "secret-search",    # justified, budgeted secret-door search
    "recovery",         # bounded recovery step
    "food",             # eligible inventory/floor food action
    "inventory",        # eligible inventory inspection
    "rest",             # proven-safe rest
    "prompt",           # mandatory prompt continuation
    "other",
)
_FAMILY_RANK = {name: i for i, name in enumerate(FAMILY_ORDER)}


def family_rank(family: str) -> int:
    """The deterministic tie-break rank for a candidate family."""
    return _FAMILY_RANK.get(family, len(_FAMILY_RANK))


# -- canonical encoding ---------------------------------------------------

#: Instrumented canonicalization counter.  ``build_table`` and friends bump
#: it once per serialize so a test can assert the retained bytes are reused
#: rather than recomputed (mutation M22, repeated_canonicalization).
_CANONICALIZE_CALLS = [0]


def reset_canonicalize_count() -> None:
    """Zero the canonicalization counter (tests only)."""
    _CANONICALIZE_CALLS[0] = 0


def canonicalize_count() -> int:
    """How many canonical serializations this process has performed."""
    return _CANONICALIZE_CALLS[0]


def canonical_bytes(obj: Any) -> bytes:
    """The versioned, deterministic UTF-8 JSON encoding of *obj*.

    Sorted object keys, tight separators, ASCII-escaped and ``allow_nan``
    disabled, so the same logical value always yields the same bytes and a
    NaN/Infinity can never enter an identity.
    """
    _CANONICALIZE_CALLS[0] += 1
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Full lowercase SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _int_or_none(value: Any) -> Optional[int]:
    """Reject bool (a subclass of int) so ``True`` is never index 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# -- immutable action model ----------------------------------------------

@dataclass(frozen=True)
class ImmutableAction:
    """A frozen tagged action: a ``str`` tag plus a tuple payload.

    Every field is hashable and immutable, so a frozen instance cannot be
    mutated through a nested dict.  :meth:`to_wire` is the only place a fresh
    mutable protocol dict is materialized, at the boundary.
    """

    tag: str
    payload: tuple = ()

    # -- constructors ----------------------------------------------------
    @classmethod
    def key(cls, code: int) -> "ImmutableAction":
        return cls("key", (code,))

    @classmethod
    def yn(cls, code: int, count: Optional[int] = None) -> "ImmutableAction":
        return cls("yn", (code, count))

    @classmethod
    def position(cls, x: int, y: int) -> "ImmutableAction":
        return cls("position", (x, y))

    @classmethod
    def text(cls, text: str) -> "ImmutableAction":
        return cls("text", (text,))

    @classmethod
    def cancel(cls) -> "ImmutableAction":
        return cls("cancel", ())

    @classmethod
    def ack(cls) -> "ImmutableAction":
        return cls("ack", ())

    @classmethod
    def menu(cls, generation: int,
             rows: Sequence[Sequence[int]]) -> "ImmutableAction":
        """A menu commit.  Rows are sorted so a selection *set* canonicalizes
        identically regardless of the order the caller listed it in -- a
        reordered commit must not become a different identity (3.2)."""
        pairs = tuple(sorted((int(r), int(c)) for r, c in rows))
        return cls("menu", (int(generation), pairs))

    # -- canonical form --------------------------------------------------
    def canonical(self) -> dict:
        """The explicit ordered record used for hashing and payloads."""
        t = self.tag
        if t == "key":
            return {"t": "key", "k": self.payload[0]}
        if t == "yn":
            code, count = self.payload
            return {"t": "yn", "k": code, "c": count}
        if t == "position":
            x, y = self.payload
            return {"t": "position", "p": [x, y]}
        if t == "text":
            return {"t": "text", "s": self.payload[0]}
        if t == "menu":
            generation, rows = self.payload
            return {"t": "menu", "g": generation,
                    "rows": [[r, c] for r, c in rows]}
        return {"t": t}

    # -- boundary --------------------------------------------------------
    def to_wire(self) -> dict:
        """A fresh mutable protocol dict, built at the send boundary."""
        t = self.tag
        if t == "key":
            return {"key": self.payload[0]}
        if t == "yn":
            code, count = self.payload
            out: Dict[str, Any] = {"yn": code}
            if count is not None:
                out["count"] = count
            return out
        if t == "position":
            x, y = self.payload
            return {"position": [x, y]}
        if t == "text":
            return {"text": self.payload[0]}
        if t == "menu":
            generation, rows = self.payload
            return {"menu": generation,
                    "commit": [[r, c] for r, c in rows]}
        if t == "cancel":
            return {"cancel": True}
        if t == "ack":
            return {"ack": True}
        raise ValueError("unknown action tag %r" % (t,))

    def signature(self) -> str:
        """The canonical action signature used for rejection equivalence."""
        return sha256_hex(canonical_bytes(self.canonical()))


def wire_to_action(action: dict) -> ImmutableAction:
    """Canonicalize a wire action dict into an :class:`ImmutableAction`.

    Raises ``ValueError`` for a shape this model does not represent; the
    structural gate remains :func:`protocol.validate_action`.
    """
    if not isinstance(action, dict):
        raise ValueError("action is not an object")
    if "cancel" in action:
        return ImmutableAction.cancel()
    if "ack" in action:
        return ImmutableAction.ack()
    if "key" in action:
        return ImmutableAction.key(action["key"])
    if "yn" in action:
        return ImmutableAction.yn(action["yn"], action.get("count"))
    if "position" in action:
        pos = action["position"]
        return ImmutableAction.position(pos[0], pos[1])
    if "text" in action:
        return ImmutableAction.text(action["text"])
    if "menu" in action:
        return ImmutableAction.menu(action["menu"],
                                    action.get("commit") or [])
    raise ValueError("unrecognised action shape %r" % (sorted(action),))


def canonical_action(action: Any) -> ImmutableAction:
    """Coerce a wire dict or an :class:`ImmutableAction` to the model."""
    if isinstance(action, ImmutableAction):
        return action
    return wire_to_action(action)


# -- candidates -----------------------------------------------------------

@dataclass(frozen=True)
class ActionCandidate:
    """One immutable, content-addressed candidate.

    ``candidate_id`` binds the canonical action plus the semantic/effect
    identity (never the score, so a score tweak does not rename a candidate
    and defeat exclusion across table versions).  ``action_signature`` binds
    the canonical action alone, so an equivalent action renamed or relabelled
    cannot evade rejection (3.2/3.5).
    """

    candidate_id: str
    action: ImmutableAction
    action_signature: str
    semantic_label: str
    family: str
    family_rank: int
    direction: tuple
    direction_rank: int
    rows: tuple
    score: int
    score_components: tuple
    reason: str
    proposed_effect: str

    def order_key(self) -> tuple:
        """The deterministic ordering key: score desc, then explicit ranks."""
        row_rank = self.rows[0][0] if self.rows else 0
        return (-self.score, self.family_rank, self.direction_rank,
                row_rank, self.semantic_label, self.candidate_id)

    def body(self) -> dict:
        """The ordered canonical record hashed into the table identity."""
        return {
            "id": self.candidate_id,
            "action": self.action.canonical(),
            "action_signature": self.action_signature,
            "label": self.semantic_label,
            "family": self.family,
            "direction": list(self.direction),
            "rows": [[r, c] for r, c in self.rows],
            "score": self.score,
            "components": [[n, v] for n, v in self.score_components],
            "reason": self.reason,
            "effect": self.proposed_effect,
        }

    def to_wire(self) -> dict:
        return self.action.to_wire()


def make_candidate(action: Any, semantic_label: str, family: str = "other",
                   direction: Sequence[int] = (),
                   direction_rank: int = 0,
                   score: int = 0,
                   score_components: Sequence[Sequence[Any]] = (),
                   reason: str = "",
                   proposed_effect: str = "") -> ActionCandidate:
    """Build a content-addressed candidate from an action and its metadata.

    Scores must be integers (3.2).  ``direction_rank`` is normally supplied
    by the caller from a fixed direction table; ``direction`` is informational
    and participates only through the action itself.
    """
    if isinstance(score, bool) or not isinstance(score, int):
        raise ValueError("score must be an integer, not %r" % (score,))
    imm = canonical_action(action)
    comps = tuple(sorted((str(n), int(v)) for n, v in score_components))
    rows = imm.payload[1] if imm.tag == "menu" else ()
    direction = tuple(direction)
    body = {"s": CANDIDATE_SCHEMA_VERSION, "a": imm.canonical(),
            "l": semantic_label, "e": proposed_effect}
    cid = sha256_hex(canonical_bytes(body))
    return ActionCandidate(
        candidate_id=cid, action=imm, action_signature=imm.signature(),
        semantic_label=semantic_label, family=family,
        family_rank=family_rank(family), direction=direction,
        direction_rank=int(direction_rank), rows=rows, score=int(score),
        score_components=comps, reason=reason,
        proposed_effect=proposed_effect)


def candidate_to_wire(candidate: ActionCandidate) -> dict:
    """The only place a candidate becomes a mutable wire action dict."""
    return candidate.to_wire()


def dedup_and_order(candidates: Sequence[ActionCandidate],
                    limit: int = MAX_CANDIDATES
                    ) -> Tuple[ActionCandidate, ...]:
    """Deterministically deduplicate, order and truncate *candidates*.

    Equivalent wire actions are deduplicated **before** truncation, keeping
    one deterministic winning representative (highest score, then the family/
    direction/row tie-break order), so a duplicate cannot consume one of the
    255 slots (3.2).  The survivors are sorted by :meth:`order_key` and cut
    to *limit*.
    """
    ordered = sorted(candidates, key=lambda c: c.order_key())
    seen = set()
    out: List[ActionCandidate] = []
    for cand in ordered:
        if cand.action_signature in seen:
            continue
        seen.add(cand.action_signature)
        out.append(cand)
        if len(out) >= limit:
            break
    return tuple(out)


# -- tables ---------------------------------------------------------------

def normalize_need_key(need_key: Any) -> tuple:
    """Coerce any need-key-like value to a neutral ``(episode, seq, id)``.

    Accepts a tuple/list, or any object exposing ``episode``/``seq``/``id``
    (such as :class:`protocol.NeedKey`) without importing it.
    """
    if need_key is None:
        return ()
    if isinstance(need_key, (tuple, list)):
        return tuple(need_key)
    parts = []
    for name in ("episode", "seq", "id"):
        if not hasattr(need_key, name):
            raise ValueError("unrecognised need key %r" % (need_key,))
        parts.append(getattr(need_key, name))
    return tuple(parts)


@dataclass(frozen=True)
class CandidateTable:
    """A prepared, immutable, content-addressed table of candidates.

    ``canonical_bytes`` are the exact retained bytes the identity was hashed
    from; they are reused for fake-Jev payloads, telemetry and replay
    comparison and are never recomputed.
    """

    schema_version: int
    need_key: tuple
    table_version: int
    table_id: str
    rejection_version: int
    ordered_candidates: tuple
    scripted_index: int
    jev_eligibility: bool
    canonical_bytes: bytes

    def __len__(self) -> int:
        return len(self.ordered_candidates)

    def candidate_at(self, index: int) -> Optional[ActionCandidate]:
        if 0 <= index < len(self.ordered_candidates):
            return self.ordered_candidates[index]
        return None

    def by_id(self, candidate_id: str) -> Optional[ActionCandidate]:
        for cand in self.ordered_candidates:
            if cand.candidate_id == candidate_id:
                return cand
        return None

    def scripted(self) -> Optional[ActionCandidate]:
        """The retained argmax (index 0), or ``None`` for an empty table."""
        return self.candidate_at(self.scripted_index)


def table_body(need_key: tuple, table_version: int, rejection_version: int,
               features_digest: str,
               ordered: Sequence[ActionCandidate],
               jev_eligibility: bool) -> dict:
    """The canonical table body, *excluding* the ID and retained bytes."""
    return {
        "schema": CANDIDATE_SCHEMA_VERSION,
        "need_key": list(need_key),
        "table_version": table_version,
        "rejection_version": rejection_version,
        "features": features_digest,
        "candidates": [c.body() for c in ordered],
        "scripted_index": 0,
        "jev_eligibility": bool(jev_eligibility),
    }


def build_table(need_key: Any, table_version: int,
                candidates: Sequence[ActionCandidate],
                features_digest: str = "", jev_eligibility: bool = False,
                rejection_version: int = 0,
                limit: int = MAX_CANDIDATES) -> CandidateTable:
    """Build one immutable table, canonicalizing its body exactly once.

    The table ID is SHA-256 of the canonical body *excluding* the ID and
    retained-byte fields themselves, so the identity is not circular (3.2).
    """
    key = normalize_need_key(need_key)
    ordered = dedup_and_order(candidates, limit=limit)
    body = table_body(key, table_version, rejection_version, features_digest,
                      ordered, jev_eligibility)
    retained = canonical_bytes(body)
    table_id = sha256_hex(retained)
    return CandidateTable(
        schema_version=CANDIDATE_SCHEMA_VERSION, need_key=key,
        table_version=table_version, table_id=table_id,
        rejection_version=int(rejection_version),
        ordered_candidates=ordered, scripted_index=0,
        jev_eligibility=bool(jev_eligibility), canonical_bytes=retained)


def jev_payload(table: CandidateTable) -> dict:
    """The fake-Jev payload for a prepared table.

    Built from the already-canonical candidate records on the table, so it
    never re-canonicalizes the retained bytes (M22).  It is an *envelope*:
    it echoes the table ID but does not become a new identity input.
    """
    return {
        "table_id": table.table_id,
        "schema": table.schema_version,
        "need_key": list(table.need_key),
        "table_version": table.table_version,
        "candidates": [
            {"index": i, "id": c.candidate_id, "label": c.semantic_label,
             "action": c.action.canonical(), "effect": c.proposed_effect,
             "score": c.score}
            for i, c in enumerate(table.ordered_candidates)
        ],
    }


# -- neutral feature and lifecycle records --------------------------------

@dataclass(frozen=True)
class ReflexFeatures:
    """The immutable public-state subset a preparation is bound to.

    Wave 1 scaffolds the neutral container and its deterministic digest; the
    full extraction lives in the later waves.  Every field is a scalar or a
    tuple of scalars/tuples, so the whole record is hashable and canonically
    encodable.
    """

    episode: int = 0
    controller_tick: int = 0
    need_key: tuple = ()
    observation_generation: int = 0
    level_instance_id: int = 0
    displayed_level: str = ""
    map_revision: int = 0
    hero_confirmed: tuple = ()
    hero_possible: tuple = ()
    hero_status: str = ""
    hp: Optional[int] = None
    hp_max: Optional[int] = None
    game_time: Optional[int] = None
    conditions: tuple = ()
    terrain: tuple = ()
    inventory_signature: tuple = ()
    floor_evidence: tuple = ()
    directives: tuple = ()
    directive_generation: int = 0
    targets: tuple = ()
    budgets: tuple = ()
    rejection_version: int = 0

    def body(self) -> dict:
        return {
            "episode": self.episode,
            "tick": self.controller_tick,
            "need_key": list(self.need_key),
            "obs_gen": self.observation_generation,
            "instance": self.level_instance_id,
            "dlvl": self.displayed_level,
            "map_rev": self.map_revision,
            "hero_confirmed": list(self.hero_confirmed),
            "hero_possible": [list(p) for p in self.hero_possible],
            "hero_status": self.hero_status,
            "hp": self.hp,
            "hp_max": self.hp_max,
            "time": self.game_time,
            "conditions": list(self.conditions),
            "terrain": [list(t) for t in self.terrain],
            "inventory": list(self.inventory_signature),
            "floor": [list(f) for f in self.floor_evidence],
            "directives": [list(d) for d in self.directives],
            "directive_gen": self.directive_generation,
            "targets": [list(t) for t in self.targets],
            "budgets": [list(b) for b in self.budgets],
            "rejection_version": self.rejection_version,
        }

    def digest(self) -> str:
        """The canonical feature digest used as a table identity input."""
        return sha256_hex(canonical_bytes(self.body()))


@dataclass(frozen=True)
class PreparedReflex:
    """Immutable features plus their immutable table (3.1)."""

    immutable_features: ReflexFeatures
    table: CandidateTable

    @property
    def table_id(self) -> str:
        return self.table.table_id

    @property
    def canonical_bytes(self) -> bytes:
        return self.table.canonical_bytes


@dataclass(frozen=True)
class SentAttempt:
    """One gameplay attempt in flight (3.1/3.4).

    The primary key is the four-tuple NeedKey + table ID + candidate ID +
    sent ordinal, available from :meth:`primary_key`.  A live attempt has
    ``terminal_state == ""``; :meth:`terminate` returns a new frozen instance
    so an attempt is never mutated in place.
    """

    need_key: tuple
    table_id: str
    candidate_id: str
    sent_ordinal: int
    action: ImmutableAction
    before_fingerprint: str
    before_hero_set: tuple
    source_instance: int
    expected_effect: str
    terminal_state: str = ""
    observed_kind: str = ""

    @property
    def primary_key(self) -> tuple:
        return (self.need_key, self.table_id, self.candidate_id,
                self.sent_ordinal)

    @property
    def live(self) -> bool:
        return self.terminal_state == ""

    def terminate(self, terminal_state: str,
                  observed_kind: str = "") -> "SentAttempt":
        """Return a terminated copy; validate the terminal category."""
        if terminal_state not in TERMINAL_STATES:
            raise ValueError("unknown terminal state %r" % (terminal_state,))
        if observed_kind and observed_kind not in OBSERVED_KINDS:
            raise ValueError("unknown observed kind %r" % (observed_kind,))
        return replace(self, terminal_state=terminal_state,
                       observed_kind=observed_kind)


def make_sent_attempt(need_key: Any, table: CandidateTable,
                      candidate: ActionCandidate, sent_ordinal: int,
                      before_fingerprint: str, before_hero_set,
                      source_instance: int,
                      expected_effect: str = "") -> SentAttempt:
    """Create the single frozen attempt for a successful gameplay send."""
    return SentAttempt(
        need_key=normalize_need_key(need_key), table_id=table.table_id,
        candidate_id=candidate.candidate_id, sent_ordinal=int(sent_ordinal),
        action=candidate.action, before_fingerprint=before_fingerprint,
        before_hero_set=tuple(sorted(tuple(p) for p in before_hero_set)),
        source_instance=int(source_instance),
        expected_effect=expected_effect or candidate.proposed_effect)


# Re-export ``field`` so callers that want to extend these dataclasses can do
# so without importing dataclasses themselves; keeps the leaf the single
# source of the DTO vocabulary.
__all__ = [
    "CANDIDATE_SCHEMA_VERSION", "MAX_CANDIDATES", "ACTION_TAGS",
    "TERMINAL_STATES", "OBSERVED_KINDS", "FAMILY_ORDER",
    "ImmutableAction", "ActionCandidate", "CandidateTable", "PreparedReflex",
    "ReflexFeatures", "SentAttempt", "canonical_bytes", "sha256_hex",
    "wire_to_action", "canonical_action", "make_candidate",
    "candidate_to_wire", "dedup_and_order", "normalize_need_key",
    "build_table", "table_body", "jev_payload", "family_rank",
    "make_sent_attempt", "reset_canonicalize_count", "canonicalize_count",
    "field",
]
