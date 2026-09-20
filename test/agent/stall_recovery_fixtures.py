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


# --- Phase 0 (prompt-edge plan): the vapor-cloud freeze geometry ----------
#
# A fog/vapor cloud is rendered as a **gray ``#``** (``include/defsym.h``
# ``S_vapor``/``S_poisoncloud``); ``instances.classify_cell`` folds a gray
# ``#`` to ``T_CORRIDOR`` -- structurally indistinguishable from a corridor
# glyph.  The wire therefore exposes *no* local overlay token that reveals the
# cloud at all, so the observed entry edge is a legal corridor edge right up
# to the engine's paranoid check ("Step into that vapor cloud?").

#: A vapor-cloud cell exactly as the agent sees it: a gray ``#`` (corridor).
VAPOR = ("#", "gray", 0, "none")

#: The live freeze geometry: the hero in a corridor whose only outbound edge is
#: the vapor step east; ``(12,10)`` is a frontier *beyond* the cloud.
VAPOR_HERO = (10, 10)
VAPOR_SRC = (10, 10)
VAPOR_DST = (11, 10)
VAPOR_BEYOND = (12, 10)
#: The exact player-visible blocking prompt (``src/hack.c:2542``).
VAPOR_PROMPT = "Step into that vapor cloud?"


def _walls(x0, x1, y0, y1):
    return {(x, y): WALL for x in range(x0, x1) for y in range(y0, y1)}


def vapor_corridor_cells():
    """Hero at ``(10,10)``; the only outbound edge is the vapor step east.

    ``(11,10)`` is the gray-``#`` vapor cell (a legal corridor edge to
    ``edge_legal``); ``(12,10)`` is a floor frontier whose unknown neighbour
    ``(13,10)`` makes it the elected default destination.  After the decline
    the edge is the only route, so the fixture exercises the fully-trapped
    variant of the freeze.
    """
    cells = _walls(9, 14, 9, 12)
    cells[VAPOR_SRC] = FLOOR
    cells[VAPOR_DST] = VAPOR
    cells[VAPOR_BEYOND] = FLOOR
    return cells


def vapor_corridor_two_frontiers_cells():
    """Two frontier destinations that share only the vapor entry edge.

    ``(12,10)`` (east, beyond the cloud) and ``(11,9)`` (north, reachable only
    by first stepping east into the cloud) both route through ``(11,10)``, so a
    suppression of that one directed edge must starve *both* acquisitions
    rather than redirect to a second destination crossing the same edge.
    """
    cells = vapor_corridor_cells()
    cells[(11, 9)] = FLOOR          # frontier: (11,8) stays unknown
    return cells


def vapor_corridor_route_around_cells():
    """The same vapor edge plus a legal north detour that never enters it.

    ``(10,10)->(10,9)->(11,9)->(12,9)->(12,10)`` is a longer (4-hop) but legal
    route to the same frontier, so filtering the vapor edge must keep the
    commitment satisfiable along the alternate route instead of retiring it.
    The detour cells are walled off above so they are *not* frontiers: the
    elected destination stays beyond the cloud and only its route changes.
    """
    cells = vapor_corridor_cells()
    for x in range(9, 14):
        cells[(x, 8)] = WALL
    cells[(10, 9)] = FLOOR
    cells[(11, 9)] = FLOOR
    cells[(12, 9)] = FLOOR
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
