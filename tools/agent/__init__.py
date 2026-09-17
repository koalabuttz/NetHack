"""Two-tier autonomous play harness for the NetHack agent wire.

The package is stdlib-only and user-facing.  ``tools.agent`` owns the game
pipe, reconstructs public state, fulfills transport obligations, validates
decisions and sends actions; it never reaches into engine state and never
speaks a private protocol.  See ``doc/agent-autoplay.md`` for the operational
guide and ``doc/agent-autoplay-plan.md`` for the design handoff.

Wave 1 ships scripted-only play (``--reflex scripted --strategy off``) and
keeps the provider contracts stubbed so a later wave can slot in a strategy
tier without changing the wire path.  The default configuration makes no
network calls of any kind.
"""

__all__ = [
    "codec",
    "protocol",
    "state",
    "policy",
    "providers",
    "controller",
    "recording",
]
