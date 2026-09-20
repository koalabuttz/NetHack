"""Small failure-capture fixtures for the stall-recovery plan (Rev 3, Phase 0).

Extracted from the live-campaign evidence quoted in
``doc/agent-stall-recovery-plan.md`` (the ep-4 15k-tick stall and the ep-2
trapped-quit sequence).  These are deliberately *small* hand-built public-state
fixtures, not slices of the large recording: the plan only needs the geometry,
the message text and the observation shape, all of which are reproducible from
public snapshots.

ep-4 evidence (``ep-4.wire.jsonl:1691-1696``): repeated time 727, hero
``(64,4)``, a closed door ``+`` at ``(65,4)``, a monster ``:`` at ``(63,4)``,
HP 13/16, and a newly numbered ``This door is locked.`` message each
observation.  ep-2 evidence (``ep-2.decisions.jsonl:26-34``): three ``m``/``s``
forced-search transactions, then ``forced search denied: trapped``, then native
quit and confirmation.
"""

FLOOR = (".", "gray", 0, "none")
WALL = ("|", "gray", 0, "none")
DOOR = ("+", "brown", 0, "none")
MONSTER = (":", "gray", 0, "none")

#: The ep-4 local geometry: the hero in a dead-end corridor whose only in-grid
#: exits are a locked door to the east and a monster to the west.  The nearest
#: reachable ``_frontier_target`` lies east *through* the locked door, which is
#: exactly the raw-grid stepping that produced the live stall.
EP4_HERO = (64, 4)
EP4_DOOR = (65, 4)
EP4_MONSTER = (63, 4)


def ep4_cells():
    """The ep-4 classified-terrain cells (raw glyph grid for ``EpisodeMemory``).

    A walled box with one floor cell under the hero, the locked door east and
    the monster west.  ``(66,4)`` is a reachable frontier *beyond* the locked
    door, so a raw-grid first-step search routes the hero into the door.
    """
    cells = {}
    for x in range(62, 67):
        for y in range(3, 6):
            cells[(x, y)] = WALL
    cells[(64, 4)] = FLOOR
    cells[(65, 4)] = DOOR
    cells[(63, 4)] = MONSTER
    cells[(66, 4)] = FLOOR          # a frontier beyond the locked door
    return cells


#: The ep-2 trapped sequence: three forced-search activations, the trapped
#: denial, and the native quit handshake.  Player-visible messages only.
EP2_TRAPPED_MESSAGES = (
    "You already found a monster.",
    "Searching doesn't feel like a good idea right now.",
    "You already found a monster.",
    "forced search denied: trapped",
    "Really quit?",
    "Goodbye...",
)
