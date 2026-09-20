# Jev Acceptance, Applied-Decision Cap, Navigation Recovery, and Room Awareness Plan (Revision 4 — pending plan review)

**Status:** Revision 4 after plan review round 3 (VERDICT: REVISE — round-2 ledger: 5 FIXED, 3 PARTIAL; round-3 findings: 2 Medium — remembered-bars openings contradiction and missing recovery-branch test names — and 1 Low stale sentence, all addressed here; room-awareness section (§5) merged and integrated into Phase 4).

Design produced by `architect:architect-gate-osc`. Post-campaign follow-up to `doc/agent-jev-presentation-plan.md`; supersedes its D3 scope restrictions per operator approval.

## Recommendation

Use the selected option's validated probability—not the service's separate confidence scalar—to accept Jev choices when `p_selected > 1.5 / N`. Count only Jev decisions that survive controller validation/overrides and are completely sent against `reflex_call_cap`. Add a bounded, observation-owned immediate-backtrack preference and explicitly route detected short cycles into safe recovery.

These are four independently testable changes (probability acceptance, applied cap, navigation recovery, room-awareness enrichment — see Phases 1–4). Phases 1–3 must not redesign providers, navigation planning, recording, or DeepSeek history, and must preserve Jev presentation bytes; Phase 4 intentionally amends the Jev state payload and criteria under the narrow presentation boundary above.

## Goal

Restore useful Jev participation without relaxing safety, prevent rejected consultations from exhausting the applied-decision allowance, and stop avoidable scripted two-cell oscillation while retaining legitimate retreats, dead-end exits, door approaches, and forced-search ownership.

The merged room-awareness section (§5) visibly enriches the Jev state payload and movement criterion text. That presentation change must bump the presentation version, update exact snapshots/golden fixtures, and stay pure. It must not add/drop/reorder retained candidates or alter `key_index`, table identity, option keys, response probability-key equality, or the offered-count denominator used by arbitration; N is frozen from `prepared.table.ordered_candidates` before presentation, and presentation refusal remains whole-request. DeepSeek/cache invariants are unchanged.

Caller-supplied evidence: the four-episode jev-ds campaign accepted zero Jev decisions; consultations were rejected at confidence 0.08–0.58 against 0.8; episode 2 had 74% alternating movement, with runs of 6–42 moves. The caller reports 805 existing passing tests. These results were not independently reproduced during this design review.

## Implementation Summary

1. Add an explicit selected-probability field to the provider result and neutral RawChoice records. Keep the existing confidence field for compatibility and legacy-mode diagnostics.
2. Add `jev_confidence_mode` (`relative`, default; `absolute`, rollback) and `jev_relative_factor` (default 1.5). Retain `confidence_threshold=0.8` for absolute mode only. Do not combine the absolute gate with relative mode.
3. Evaluate the rule in pure arbitration using the exact immutable offered table's retained count. Preserve every identity, parser, index, rejection, and eligibility check.
4. Add a ledger `reflex_applied` counter; use it for cap admission. Preserve paid reservation/usage settlement and existing consultation diagnostics.
5. Increment applied count only after a complete send of the unoverridden, locally valid Jev proposal.
6. Track the previous distinct confirmed cell in existing reflex-local recovery state, not during preparation. Apply anti-backtracking only to ordinary navigation and cycle-recovery movement.
7. Make the existing cycle flag actively enter recovery even when `mem.no_progress` is zero. Preserve search budgets, emergency precedence, and the controller's forced-search transaction.
8. Keep sidecar schema unchanged; add explanatory reason strings. Add one ledger reporting field and configuration metadata, not new decision record fields.
9. Merge the room-awareness enrichment (§5): it amends the Jev state payload and movement criterion text, so it must bump the presentation version, update exact snapshots/golden fixtures, and remain pure. It must NOT add, drop, or reorder retained candidates or alter `key_index`, table identity, option keys, response probability-key equality, or the offered-count denominator used by arbitration (N is frozen from `prepared.table.ordered_candidates` before presentation; presentation refusal remains whole-request). DeepSeek/cache invariants stay intact.

## 1. Verified facts

### Confidence and offered options

- `tools/agent/arbitration.py:109–125` defines RawChoice with an index and scalar confidence, but no selected probability or vector.
- `tools/agent/arbitration.py:144–191` validates raw choices. Actual order is parse, abstention, identity, index, finite scalar probability, flat threshold, rejected membership, eligibility. Its docstring's stated ordering differs slightly; implementation is authoritative.
- `tools/agent/controller.py:2796–2807` constructs RawChoice and supplies `config.confidence_threshold` plus the emergency-family exclusion.
- `tools/agent/providers.py:1413–1435` validates exact probability keys, numeric finite values in [0,1], and sum within 1e-5.
- `tools/agent/providers.py:1438–1450,1722–1781` prefers `answers.action.confidence` when valid; otherwise it substitutes selected probability. The parser also checks that the named selection is a distribution maximum. Therefore confidence cannot safely be assumed equal to selected probability.
- `tools/agent/providers.py:1604–1635` bypasses singleton tables and constructs the criteria/key-index mapping from the retained table. `tools/agent/candidates.py:379–385,481–498` deduplicates equivalent wire actions before truncation. N must count retained offered options, not raw navigation targets.
- `test/agent/test_auto_jev_presentation.py:702–736` contains the named legacy confidence test. It tests scalar threshold behavior using three candidates; it does not exercise a parsed probability vector.

### Cap and application lifecycle

- `tools/agent/budget.py:185–194,634–658` compares `reflex_paid_dispatched` to the cap and increments it when reserving a paid consultation.
- `tools/agent/controller.py:2751–2774` checks availability/cap and skips unsupported, singleton, or presentation-refused builds before reservation.
- `tools/agent/controller.py:2786–2789` settles paid usage exactly once before accepting/rejecting the raw answer. This must remain independent of applied-decision accounting.
- `tools/agent/controller.py:2808–2815` returns scripted fallback on rejection and increments `reflex_successful` on arbitration acceptance, before final action validation or send.
- `tools/agent/controller.py:2544–2615` subsequently validates the action, may substitute fallback, may override with a controller-owned forced-search action, records the decision, and emits it. A successful complete emit is the appropriate applied-count boundary.

### Navigation and recovery

- `tools/agent/policy.py:555–626` gives quit, emergency disengagement, hunger, and maintenance precedence. Existing loop recovery uses stationary `no_progress` thresholds 3/6/10.
- `tools/agent/state.py:585–591` resets `no_progress` when the hero changes cells. It also sets `last_hero` to the new current cell; this is not a reliable previous-departed-cell source after observation folding.
- `tools/agent/policy.py:637–665` computes one fresh Dijkstra plan and scores each target's first step. No persisted-target-following operation appears in this path.
- `tools/agent/navigation.py:185–229` enumerates stairs, closed-door approaches, frontiers, and unvisited cells. A visited cell can remain a frontier. `tools/agent/policy.py:1042–1062` gives frontiers a higher base than unvisited targets, with bounded path adjustment.
- Inference: fresh frontier target reselection plus bounded visit/path penalties can drive reversals; unvisited targets share the same first-step mechanism but cease being unvisited once entered. Source review does not prove which target family caused each campaign action.
- `tools/agent/policy.py:932–940` has an older nearest-frontier helper, but it is used by `_unblock` (`911–924`), not the primary candidate path. Do not mistakenly fix only that helper.
- `tools/agent/recovery.py:179–210` already detects ABAB after four position samples and ABCABC after six. `tools/agent/policy.py:755–767` folds it at committed observations. `_cycled` currently suppresses ordinary search (`611,668`), rather than independently entering recovery.
- `tools/agent/policy.py:778–791` resets reflex-local recovery on a fresh instance. `navigation.TargetStore.invalidate_cycle` exists (`tools/agent/navigation.py:309–310`) and has unit coverage, but the inspected primary policy path does not use that store. No target-store integration is needed here.
- `tools/agent/policy.py:949–967` picks a shuffled known-passable fallback neighbor. It does not explicitly apply the planner's edge-legality predicate. A new cycle escape must not inherit unsafe diagonal/door assumptions.

### Artifacts and prior commitments

- `tools/agent/recording.py:238–244` records proposal, selected action, provider, and a free-form reason. Preserve these keys and their types.
- `tools/agent/evaluate.py:1265–1266,1412–1413` accepts companion decisions and carries reasons through. `1135–1171` has a separate paid-reflex fallback path; do not assume its module-level arbitration comments mean it currently validates Jev through the live choice gate.
- `tools/agent/exploration_metrics.py:95–105,122–123` defines loop spans as repeated identical `(hero, displayed time)` frames. These fields do NOT measure alternating movements.
- `doc/agent-jev-presentation-plan.md:213–218,266,315,323–324` preserves the old gate under deferred D3 and excludes policy changes. This approved follow-up explicitly supersedes those scope restrictions; it does not revise the request envelope or retained-candidate/key contract, while §5 (Phase 4) intentionally revises the nested Jev state and criterion presentation contract (with presentation-version bump and snapshot/golden regeneration).
- `doc/agent-cache-plan.md:31–50,62–103,105–111` fixes DeepSeek rendering order, episode-owned history, transactional commits, reservations, and paid-failure settlement. All remain invariant.

Inspection limitation: a search for `tools/agent/campaign.py` failed because that file does not exist. A filename search found no campaign-named file; searches under tools located the metrics consumer above. An external campaign summarizer may exist outside the inspected tree and needs caller/implementer verification.

## 2. Relative acceptance contract

### Rule and rationale

For a valid multi-option Jev Choice:

- `N = len(table.ordered_candidates)`; require N >= 2.
- `p = raw.selected_probability`, supplied from the validated response vector at the selected key.
- Accept on concentration only when `p > k/N`, with k = 1.5 by default.
- Require p to be a finite non-bool number in [0,1]. Missing/malformed selected probability in relative mode fails closed; do not silently fall back to the unrelated confidence scalar.

Thresholds: N=2: >0.75; N=3: >0.5; N=4: >0.375; N=5: >0.3; N=6: >0.25. Exactly uniform distributions still abstain. Equality fails because the approved rule says "exceeds." Preserve inclusive equality in legacy absolute mode.

Choose 1.5 rather than 2 because strict 2/N makes two-option acceptance impossible. Add no floor or cap: a floor reintroduces unrelated absolute gating, while a cap weakens the multiplier's meaning. For fixed N acceptance is monotone in p; for fixed p it becomes no harder as N grows. Large-table thresholds are a known tradeoff; present navigation is bounded by distinct movement actions, and safety remains independent.

### Interface and configuration

- Proposed optional field `selected_probability` on `ReflexChoiceResult` and `RawChoice`, default None for constructor compatibility. The parser populates it only after vector validation/max validation.
- Keep the full vector provider-local: arbitration needs p and the already-bound retained table, not service-shaped data or a provider import.
- Add explicit keyword policy parameters to `validate_raw_choice`; prefer its new default to be relative so new internal callers cannot accidentally retain the bad gate. Update existing legacy scalar tests to request absolute mode explicitly.
- `jev_confidence_mode`: enum relative/absolute, default relative.
- `jev_relative_factor`: finite non-bool numeric, 1 < k < 2. This guarantees above-uniform behavior and attainable binary decisions. Default 1.5.
- Preserve `confidence_threshold` and its validation, CLI spelling, and metadata. It affects absolute mode only; document that changing it alone no longer tunes default Jev acceptance.
- Wire configuration consistently through verified files `providers.py`, `__main__.py`, `evaluate.py`, and controller allowlisted metadata (`controller.py:2849–2876`). Do not enable paid network replay as part of this work.
- New defaults deliberately change behavior, while old config construction remains valid. Rollback is explicit absolute mode, not implicit detection of an old threshold value.

### Safety and diagnostics

Retain parse/abstain/identity/index/rejected-member/eligible checks. Relative mode validates selected probability; absolute mode retains scalar confidence validation. A passing concentration test never bypasses rejection or safety.

Keep rejection code `confidence`; add reason text such as `relative concentration p=0.300 N=6 k=1.500 requires >0.250` for diagnosis, including on accepted Jev decisions through the existing reason field. Preserve useful `jev rejected:` and `jev skipped:` prefixes. Do not claim that concentration is permission, calibrated survival probability, or evidence an alternative is unsafe.

Test exact threshold boundaries with deliberate float cases; do not introduce a hidden acceptance epsilon. Parser sum tolerance remains unchanged.

## 3. Applied-decision cap contract

### Definition

An applied Jev decision is one accepted Jev proposal that passes local protocol validation, is not replaced by fallback or forced-search override, and completes its action-wire send. It need not later produce movement or a successful gameplay result. A later native invalid response does not refund the count: the paid choice was applied to the game interface.

### Ledger and controller responsibilities

- Add `reflex_applied=0` and additive ledger output `reflex.applied`.
- `reflex_paid_available()` retains cap<=0 meaning disabled, but compares applied count rather than reservation count.
- Keep `reflex_paid_dispatched` and its existing diagnostic semantics for compatibility; do not repurpose or decrement it. It currently measures reservations, notwithstanding its name. Clarify this limitation in documentation.
- Do not use `reflex_successful` for cap accounting. It is an aggregate provider-success/pre-send counter whose semantics differ by tier: the scripted path increments it on success at `tools/agent/controller.py:2717`, the Jev path increments it only after arbitration acceptance at `:2813`, and the Jev fallback does not increment it. It remains unsuitable for cap accounting because it is pre-send and tier-ambiguous.
- Add `BudgetLedger.note_reflex_applied(token)` and invoke it only on the complete-send path in `_answer_now`, after `_emit` succeeds. Pass a controller-owned applied-decision token captured at arbitration acceptance and carried through validation/override; comparing action dictionaries alone is insufficient because a fallback may coincidentally equal the original action. `note_reflex_applied` must be idempotent per token. **Idempotence storage contract:** the token set is private, episode-owned `BudgetLedger` state — it resets with the episode, stores no provider payload or secret material, is never emitted in artifacts or sidecars, and is naturally bounded by at most `reflex_call_cap` unique entries (repairs reuse an existing token). Idempotence and episode-reset assertions are folded into the cap/repair tests.
- No count for unsupported needs, singleton/refused builds, parse failure, abstention, stale/unsafe/rejected choices, low concentration, timeout/provider failure, local validation replacement, forced override, or failed/partial send.
- Count once per newly applied decision. Delivery repair must not charge twice. Retain the controller-owned applied-decision token across `invalid(incomplete)` delivery repair, keyed to the original accepted consultation (not action-dictionary equality, not the sent ordinal). On `invalid(incomplete)`, reissue the page obligation and resend the frozen validated action `without consulting Jev again`. Because `note_reflex_applied(token)` is idempotent, a resend of the same token yields one applied increment with two sent ordinals. If the table/need identity becomes stale before the resend, fail closed to the scripted action rather than treating the resend as the same decision.
- Retain usage settlement under the original paid reservation even on rejection, timeout, or later failed send. Applied-count changes must not release legitimate paid exposure.

The live flow is sequential at the decision/send boundary, so no new applied-slot reservation framework is needed. If future code supports concurrent applicable Jev decisions, admission must reserve applied slots then; it is not part of this change.

### Recording and operations

Skipped/rejected consultations continue to produce the normal final fallback decision record with their reason and available usage. A decision record precedes the send and is not proof of application; reconcile with the existing action/send record and ledger applied count.

An applied cap no longer bounds the number or cost of unsuccessful consultations. Preserve deadlines, episode/tick bounds, token/USD admission and fail-closed behavior. Explicitly document this operational consequence. Do not quietly introduce a rejection-attempt cap that undoes the operator's decision.

## 4. Navigation anti-oscillation

### State ownership

Use existing reflex-local RecoveryState/CycleDetector as the owner of bounded confirmed-position history. Fold only from `note_observation`; preparation, rejected candidates, selected keys, and failed writes do not advance it. RecoveryState owns and exposes an explicit movement-history state machine:

- `current` — the last confirmed position.
- `previous_distinct` — the last confirmed position distinct from `current` (the anti-backtrack reference).
- **deduplicated trailing movement history** — the ordered list of distinct confirmed positions used for period-2/period-3 cycle detection (consecutive duplicates excluded).
- `cycle_active` — whether the trailing movement is currently on a detected period-2 or period-3 cycle.

Transitions:

1. **Identical confirmed position.** A repeated confirmation equal to `current` preserves `previous_distinct`, the trailing history, and `cycle_active` unchanged. It must not erase the last departed cell or dilute cycle detection; stationary handling stays with `mem.no_progress`.
2. **Adjacent distinct move.** A confirmed position Chebyshev-adjacent to `current` is appended to the trailing history, recomputes period-2/period-3 status, and clears `cycle_active` only when the resulting trailing movement is off-cycle.
3. **Unknown or Chebyshev-nonadjacent relocation.** A `None` hero or a non-adjacent relocation clears the trailing history, the previous-cell evidence, and `cycle_active`.
4. **Instance reset.** A fresh instance clears all of the above.

RecoveryState exposes both the previous distinct cell and the active-cycle state (not an event-return boolean), so policy can read the anti-backtrack reference and the cycle condition independently. Update the existing unknown-position test honestly if unknown positions now break cycle continuity.

Use confirmed positions rather than inverse keys: a movement key can open a door or fail without moving. No new controller-to-policy last-key plumbing is necessary. Preserve period-three detection.

### Ordinary navigation preference

Apply in `_navigation_candidates` while target metadata remains available, before candidate construction/deduplication. Do not modify the Dijkstra graph or permanently forbid an edge.

For each reverse first-step target whose destination is the previous distinct cell, the preference is a **pure operation over scored target/action representatives**, applied while target metadata is still available, before candidate construction:

1. Compute the final candidate score **including the directive contribution** (the +30 directive bonus participates in the score).
2. Group raw entries by `(candidate family, canonical first-step action signature)` and retain the same deterministic best representative that dedup would retain.
3. Remove rejected action signatures from consideration; otherwise a rejected alternative can suppress the only usable retreat.
4. Within each `family`, compare each reversing representative against the best non-reversing representative **across action signatures in that family** (an exact `(family, action signature)` group can never contain a non-reversing alternative, since the signature encodes the movement key).
5. Keep the reversal when no non-reversing alternative exists, or when `reverse_score − best_alt_score > 40` (strict: a margin of exactly 40 does **not** qualify and the reversal is **suppressed**; only margins strictly greater than 40 keep it). Otherwise suppress that reversing representative for this preparation.
6. Then construct candidates and run normal global dedup/order; preserve existing ordering among remaining candidates.

Different-family priorities, directives, stairs, and door approaches continue to use existing scoring. An unvisited or frontier alternative must not displace a uniquely required higher-priority stair/door route just to avoid reversal. Directives are not cross-family suppression: the 40-point comparison is made within a `family`, across its action-signature representatives.

This bounded preference is deliberately stronger than an exact-score tie-break but weaker than a universal ban. It addresses comparable exploratory alternatives without turning every backtrack into a detour. Include an explanatory additive reason when selecting an anti-backtrack alternative. Added fixtures cover the 40/41 score boundary and the directive-bonus interaction: a 40-point margin **suppresses** the reversal, a 41-point margin **keeps** it, and the +30 directive contribution is included in the compared score. Mutation demonstration: flipping `>` to `>=` must fail the equality case.

### Active cycle recovery

Treat `_cycled` as an independent condition at the existing loop-breaker location, after emergency/hunger handling and before ordinary navigation. The existing ABAB detector fires after three alternating moves; this meets "about four" and should remain the documented threshold. Require recovery no later than the decision following A-B-A-B-A in integration tests.

Do not just set `np=3`: `_cycled` suppresses search there and currently falls back into navigation again. Do not just set `np=6`: the existing shuffled move can immediately reverse and lacks an explicit edge-legality check.

For cycle-triggered recovery, reuse the safe-movement part of the existing recovery path with an explicit cycle mode:

- Enumerate known safe neighbors using the same terrain and `navigation.edge_legal` checks as planning; exclude monster/unknown destinations.
- Prefer a non-reversing exit; use deterministic visit-count/direction-rank ordering for this branch.
- If the only legal escape is backtracking, retain it. Never classify a traversable dead end as trapped merely because of the preference.
- If no legal movement exists, use existing bounded search-fallback/forced-search nomination machinery. Do not manufacture an unbudgeted search, direct dangerous prefix, or indefinite wait.
- ~~Leave ordinary stationary 3/6/10 recovery semantics unchanged except shared safe helper improvements proven by regression tests.~~ **SUPERSEDED by `doc/agent-stall-recovery-plan.md` (Revision 3) §1.** The stationary 3/6/10 thresholds now share **the same** legal, edge-legal bounded recovery builder as cycle recovery (the former "unchanged" clause is withdrawn). This changes only the *recovery mechanism*; the strict `>40` uncommitted anti-backtrack margin, the strict `p > k/N` relative confidence gate, and the emergency/hunger/mandatory-continuation precedence above recovery are all unchanged.

The recovery selection should be a singleton recovery candidate, as existing loop-breaker actions are, so Jev cannot choose the oscillating alternative on that tick. The next committed off-cycle movement clears the cycle condition naturally; do not mutate cycle history merely because a proposal was produced.

Emergency `_escape` is untouched and can reverse whenever danger requires it. Controller forced-search overrides remain authoritative after final selection. A door-opening key that leaves the hero stationary must not be counted as a new traversed edge.

## 5. Room-awareness enrichment

### Goal and recommendation

Give Jev the room-scale display evidence it currently lacks: where item-like appearances, creatures, and features are shown, and where known corridors, doorways, and stairs lie. Preserve the distinction between **shown in the current player screen** and **remembered terrain**; neither means physically verified present, reachable, or safe.

Recommend a closed canonical foreground overlay, a bounded `room.contents` list of display categories, and a bounded `room.openings` summary of classified terrain landmarks. Retain `*` exclusively as the canonical creature marker; introduce `&` as an item-appearance marker and `?` as an unclassified-display marker. Raw input glyphs must never leak into the rendered map. Add category-level destination annotations to movement criteria, but do not fabricate item names unavailable in the snapshot.

This is presentation enrichment, not room segmentation, path planning, or a change to the relative acceptance gate. Richer evidence may improve concentration naturally; no acceptance parameter depends on the enrichment.

### 5.1 Verified facts and boundaries

- The approved evidence order is canonical action/current need, candidate direction plus confirmed hero, persistent classified terrain plus current-snapshot occupancy, recognized route purpose, then exact bound item text (`doc/agent-jev-presentation-plan.md:80–90`). Missing optional evidence degrades safely rather than refusing the whole request (`92–104`).
- The approved map explicitly excludes items and other foreground content, canonicalizes creatures to `*`, and gives the confirmed hero final precedence (`doc/agent-jev-presentation-plan.md:146–190,204`). Its closed emitted-glyph/legend guarantee is a requirement, not an incidental snapshot to weaken.
- `tools/agent/presentation.py:319–345,459–481` already separates persistent terrain from current occupancy and computes the actual adjacent destination. Enrichment belongs at that seam, not in policy reason parsing.
- `tools/agent/presentation.py:803–812` delegates map construction to `state.bounded_map`; `827–861` defines the state object and purity/null-vs-empty contract.
- `tools/agent/state.py:259–284,287–343` defines the canonical terrain vocabulary and bounded map helper. It currently overlays only creatures, uses remembered terrain/stairs for the underlay, and computes one-cell-margin protocol-clamped bounds.
- `tools/agent/protocol.py:88–100,118–139` establishes complete `base:null` snapshots. Map triples `[x,y,palette_id]` resolve to display tuples `(glyph, foreground color, style, frame color)`; an absent cell is blank. These fields are not per-cell object descriptions; neither style nor frame nor palette entries provide an established item-name binding.
- **Source availability predicate (exact):** the current snapshot is an available contents/openings source iff `snapshot is not None` and `snapshot.map` is a dict — the empty dict `{}` is *known empty*, not unavailable; a missing or non-dict `map` is *unavailable*. Room helpers must implement this predicate explicitly and must not inherit the existing `getattr(..., "map", None) or {}` collapse, which silently merges absent/malformed with known-empty. Fixture vectors must cover malformed-map versus empty-map versus absent-snapshot.
- `tools/agent/instances.py:22–42,89–149` owns the terrain classes and color-aware classification. Reuse it; do not create another wall/door/water classifier in presentation.
- `instances.py:49–64,103–106` treats `]` as a creature-class glyph. Armor’s pinned symbol is `[`, not `]` (`include/defsym.h:468–480`).
- `include/defsym.h:468–484` also shows important collisions: `*` is gem/rock appearance, `+` is a spellbook symbol as well as a door/wall display, `_` can be a chain, and `.` can be venom. Glyphs alone cannot establish all object identities. `instances.py:116–123,142–145` nevertheless intentionally gives these glyphs terrain interpretations under its existing classification contract.
- `TerrainMemory.floor_objects` exists but the inspected merge path does not populate it (`instances.py:163–195`). Do not use it as a current contents source or build this feature around it.

**Truthfulness terminology:** use “shown,” “appearance,” and “classified terrain.” A current full screen can itself contain the game’s remembered display; the protocol supplies no visibility/line-of-sight bit. Do not equate “screen” with freshly seen, alive, still there, or reachable. This design preserves the existing current-snapshot evidence contract while making that limitation explicit.

### 5.2 Components and data flow

1. Add a small pure display-appearance classifier alongside `instances.classify_cell` (proposed helper, no new module). It accepts the existing display tuple plus confirmed-hero/position information, delegates terrain/occupant interpretation to existing classification, and returns a closed presentation category. It does not change navigation walkability, `Cell` safety semantics, terrain merge behavior, or persistence.
2. `state.bounded_map` uses this appearance result for foreground overlays and the existing `TERRAIN_GLYPHS` for classified terrain. Extend this existing helper rather than building a second crop renderer.
3. `presentation.render_state` computes the map once, then derives bounded `room.contents` and `room.openings` against the same map bounds and frozen context.
4. Movement criterion rendering uses the same appearance helper at the actual adjacent destination. It must not look up the first entry in a capped contents list.

All helpers are read-only. No `merge`, observation fold, cache update, target selection, or memory repair is allowed during rendering. Inputs may be absent; optional evidence failure must not cause new pre-dispatch refusal categories.

### 5.3 Current-display appearance contract

Apply the following precedence to each valid nonblank current snapshot cell:

1. Confirmed hero coordinate: hero; do not infer identity by scanning for the first `@`.
2. Existing monster classification, including `@` off the confirmed hero coordinate: `creature`.
3. Existing `classify_cell` returns known terrain: retain its terrain class. This deliberately preserves current ambiguity handling for `+`, `_`, `.`, and backtick; see limitations below.
4. A glyph in this closed item-appearance table: `item` with the exact category below.
5. Otherwise: `unclassified`.

| Input glyph | `category` |
|---|---|
| `)` | `weapon appearance` |
| `[` | `armor appearance` |
| `=` | `ring appearance` |
| `"` | `amulet appearance` |
| `(` | `tool appearance` |
| `%` | `food appearance` |
| `!` | `potion appearance` |
| `?` | `scroll appearance` |
| `/` | `wand appearance` |
| `$` | `coin appearance` |
| `*` | `gem or rock appearance` |
| `0` | `iron ball appearance` |

Iron balls are included because no earlier classification consumes `0` and the pinned profile provides the exact class (`include/defsym.h:482`); the precedence already distinguishes a current raw iron-ball appearance (foreground overlay → `&`) from a remembered boulder (`T_BOULDER` terrain underlay → `0`).

For creatures, `category` is `creature`; for known feature terrain, use the existing terrain class string; for unclassified content, `category` is `unclassified display`.

Do not infer species, disposition, edible/safe food, BUC, identification, count, stack size, or exact object name. A food appearance can conceal a corpse or other unsafe item. Unknown or custom symbols map to `unclassified`, not guessed object categories. Blank and absent cells are not contents.

This table is a **display-symbol interpretation**, not a second terrain classifier. Keep it in `instances.py` with explicit pinned-profile documentation and tests. It must not feed policy eligibility.

**Known unavoidable ambiguity:** the existing terrain classifier consumes some object/terrain-colliding glyphs. This enrichment will not label every spellbook, chain, venom, statue, or disguised creature as an item. The payload must explicitly disclaim completeness, rather than silently claiming a full room inventory. Changing classifier semantics or supplying richer public look descriptions is a separate task.

### 5.4 Map overlay and fixed legend

Preserve the map object shape `{x_min,x_max,y_min,y_max,text}`, engine coordinates, row prefixes, interior blanks, bounds, margin, and null semantics.

Cell precedence becomes:

1. Persistent classified terrain underlay and existing remembered-stair fallback.
2. Current snapshot known classified terrain, rendered via `TERRAIN_GLYPHS` (never verbatim raw terrain glyph/color). This makes current feature evidence visible even if a test/direct caller supplies a lagging terrain reference; it does not mutate the memory.
3. Current foreground appearance: creature → `*`; item → `&`; unclassified nonblank display → `?`.
4. Confirmed hero → `@`, always final.

Compute bounds from **all** resulting nonblank map cells plus hero and remembered stairs, before list caps. Thus an item in a previously unknown area expands the crop; truncating `room.contents` never hides its map marker. The existing protocol rectangle bounds the map to at most 79×21 cells.

Do not emit raw monster or object symbols. In particular:

- Raw gem `*` becomes canonical item `&`, never creature `*`.
- Raw demon `&` becomes creature `*`, never item `&`.
- Raw scroll `?` becomes item `&`, not unclassified `?`.
- Raw `]` becomes creature `*`; raw armor `[` becomes item `&`.

Replace the legend with this exact fixed object (in the following insertion order):

```json
{
  " ": "unknown or unobserved",
  ".": "classified floor",
  "#": "classified corridor or tree",
  "-": "classified open door; orientation omitted",
  "|": "classified wall or bars; orientation omitted",
  "+": "classified closed door or doorway of unknown state",
  ">": "classified stairs down",
  "<": "classified stairs up",
  "@": "your confirmed hero",
  "*": "creature appearance shown on the current screen; disposition unknown",
  "&": "item appearance shown on the current screen; see room.contents",
  "?": "unclassified nonblank display shown on the current screen",
  "^": "classified trap",
  "}": "classified water or lava",
  "0": "classified boulder",
  "{": "classified fountain",
  "_": "classified altar"
}
```

The existing map does not carry color. Remove misleading “color distinguishes” wording; exact class distinctions are available in structured records where relevant. Terrain glyphs can be current or remembered, as stated by the fixed room scope text. No raw symbol escape hatch is allowed. The coverage assertion is over glyph columns only, excluding row-number prefixes and newlines.

### 5.5 Exact state field: `room`

Add required `room` immediately after `map` and before `stairs` in state insertion order:

```json
{
  "scope": "Current screen within map bounds, not a segmented room. Screen appearances may be remembered by the game; contents are not exhaustive. Terrain may be remembered. Openings are landmarks, not verified exits or routes.",
  "contents": [],
  "contents_omitted": 0,
  "openings": [],
  "openings_omitted": 0
}
```

`scope` is the exact constant above. `room` is always an object. Each list is `null` when its required source is unavailable; its omitted count is then `null`. `[]` with count 0 means the available source contains no matching records, not that the room is empty or lacks real exits.

#### `room.contents`

Source: only current `snapshot.map`, within the returned map rectangle. Never use `EpisodeMemory.grid`, persistent occupancy, `floor_objects`, inventory, or unbound messages.

Include one record per non-hero position for:

- creature or item appearance;
- unclassified nonblank display;
- current classified feature in `{tree,water,lava,trap,boulder,fountain,altar}`.

**Bars limitation (exact):** `instances.classify_cell` never produces `T_BARS` from a current display cell (`#` resolves to tree/corridor/unknown by color; the protocol color vocabulary has no metal slot, and native bars use `#` with `HI_METAL`). Screen-derived `bars` records are therefore **not** promised and this is a documented, deliberate omission — a live iron-bar square may classify as corridor, tree, or unclassified. Remembered `T_BARS` renders as `|` in the map terrain underlay (legend-covered) but produces **no structured record** in either `room.contents` or `room.openings` — bars are deliberately excluded from both lists to keep the openings schema an exits/landmark contract rather than an obstruction inventory. Do not extend `classify_cell` in this change: altering it would change navigation walkability and `TerrainMemory.merge` semantics; a pinned display-only bars rule (map-only or structured) is a separate, explicitly scoped task. A named test asserts remembered bars stay legend-covered in the map without producing a structured record.

Do not include ordinary floor, wall, corridor, doors, or stairs here; doors/stairs/corridor landmarks belong to `openings`.

Exact record shape and key order:

```json
{"at":[12,8],"kind":"item","category":"food appearance"}
```

`kind` is one of `item`, `creature`, `feature`, `unclassified`; category follows §5.3. No raw glyph, name, BUC, inferred count, or remembered contents entry.

Ordering: when hero is confirmed, ascending `(Chebyshev distance from hero, y, x)`; otherwise ascending `(y,x)`. Cap at **16 records**. `contents_omitted` is the exact number of matching records beyond the cap. All markers remain present in the map. An empty available map yields `[]`; unavailable/malformed snapshot input yields `null`. If map rendering is unavailable, both room lists are null rather than independently choosing another bounding rectangle.

#### `room.openings`

Purpose: identify displayed or remembered corridor/door/stair landmarks across the bounded map, including doors on the far side of a room. Do not limit this to immediately adjacent moves and call that “room exits.”

Eligibility classes: `{corridor,doorway,open-door,closed-door,stairs-up,stairs-down}` only. Ordinary floor is already visible on the map; do not label every floor square an exit. Closed doors are included as landmarks but not described as openings that can currently be traversed.

For each map-bounded coordinate:

- Read remembered class through `TerrainMemory.ter`, if available.
- Classify the current display tuple with `instances.classify_cell`, if available.
- If the current classification is known, it controls the record: include iff it is an eligible class; set `source="screen"`.
- Otherwise, if remembered classification is eligible, include it with `source="memory"`. A current item/creature hiding a doorway does not make the doorway freshly observed.
- A currently shown wall replacing a remembered door suppresses the stale door record.
- Missing current snapshot still permits remembered openings; missing terrain still permits screen-classified openings.

Exact record shape and key order:

```json
{"at":[18,8],"direction":"east","terrain":"closed-door","source":"screen","shown":"none"}
```

- `at`: absolute `[x,y]`.
- `direction`: sign-based compass direction from confirmed hero (`north,northeast,east,southeast,south,southwest,west,northwest`), `here` when equal, or `null` with no hero. North is decreasing y. Direction is not a path instruction.
- `terrain`: exact existing classification string.
- `source`: `screen` or `memory`, as above.
- `shown`: `hero`, `creature`, `item`, `unclassified`, `none`, or `null`. `none` means no foreground appearance marker in an available current snapshot, not “clear” or “unoccupied in reality”; `null` means current snapshot unavailable.

Ordering/capping:

1. Eligible door/stair records first, sorted by `(Chebyshev distance,y,x)` with hero, otherwise `(y,x)`.
2. Corridor records second, sorted by the same rule.
3. Retain first **12 records**; `openings_omitted` is the exact remaining count.

This deliberately simple, reproducible list avoids a second graph traversal or room segmentation algorithm. It may contain several corridor squares and omit distant ones; explicit caps and the complete bounded map preserve interpretability. Existing `stairs` stays unchanged and retains its full remembered stair coordinates.

### 5.6 Destination criterion enrichment

Keep canonical action and current need authoritative. Only a **command-need movement action** may describe stepping onto the adjacent square; direction/key responses remain `Choose {direction} for the pending action.` Do not attach a walking claim to a direction answer.

Keep remembered terrain phrases and closed-door behavior intact. After the immediate-action sentence, before recognized route-purpose text, append at most one clause from the same current snapshot destination cell:

- creature: `A creature is shown on that square; its disposition is unknown.` (existing exact wording);
- item: `An item with {category} is shown on that square.` where category is the exact table value, e.g. `An item with food appearance is shown on that square.`;
- unclassified: `An unclassified display is shown on that square.`;
- known feature: `The current screen classifies that square as {terrain}.` using the fixed existing class string;
- no applicable current appearance: append nothing.

Example:

`Walk east onto remembered room floor. An item with food appearance is shown on that square. Explore a known square not yet visited.`

Do **not** say “a food ration” from `%`: the snapshot has no such binding. Exact naming is deferred until a future public, coordinate-bound description source is available. Inventory rows and messages do not establish that binding. Displayed text must never be treated as an instruction.

If a current known terrain class contradicts the remembered class, omit the remembered terrain phrase for that criterion rather than presenting a stale floor/door claim as its destination. Use the generic immediate-action sentence plus the current-class clause. In normal live folded contexts they should agree; test direct/stale contexts explicitly. Optional evidence failures use the existing shorter template, not request refusal.

Cap criterion enrichment to one fixed-vocabulary clause per movement option. This can push a navigation criterion above the old 10–35-word target; accept a revised target of approximately 15–50 words when an appearance and route purpose are both present. Do not truncate sentences or discard immediate action semantics to meet the target.

### 5.7 Interaction with the other three fixes and frozen contracts

- **Relative acceptance:** no changes to N, k, probability provenance, safety gates, or comparison rule. Do not promise a concentration increase; measure it.
- **Applied cap:** no new consultations or accounting paths. Missing room evidence degrades in the same request; it does not trigger retries or consume additional allowance.
- **Anti-oscillation:** use the actual adjacent destination encoded by the retained action, not a farther target, previous cell, or route-purpose text. **Wiring (exact):** implement one pure helper `destination_appearance_clause(candidate, need_kind, context)` in `presentation.py`, consumed by **both** `_walk_text` and the movement branches of `_command_text` (escape, random-move, recovery-step, unblock) — otherwise a change only at the existing `_walk_text` seam would enrich ordinary navigation but silently miss cycle-recovery and emergency moves. The helper returns an empty string for non-command needs, for nonmovement actions (search/wait/inventory/etc.), and when no current appearance exists; direction/key answers remain neutral. Nonmovement recovery/search must not acquire destination claims. New policy reasons must still pass the existing recognized-purpose mapping; never expose raw diagnostic reasons as instructions. Named tests cover a cycle-recovery movement, an emergency escape movement, and search/wait/nonmovement recovery asserting exactly which of them carries the clause.
- **Wire freeze:** criteria remain a JSON object in retained order; semantic keys, key→index bindings, option count, and canonical actions remain unchanged. This amends the nested state/criterion text contract, not §2’s request-envelope contract.
- **DeepSeek:** no changes to its renderer/history/cache or `state.render_map` default. Only the Jev bounded-map presentation path is enriched.
- Update the existing presentation-version traceability identifier according to the repository’s current mechanism; no decision-sidecar schema change is required.

### 5.8 Token budget and alternatives

The map has the same geometric maximum and one character per cell; new foreground evidence can expand a previously smaller crop up to that existing maximum. Legend growth is two marker entries plus more accurate wording. Structured additions are capped at 16 contents and 12 opening records, with short fixed vocabulary, and each movement criterion adds at most one clause.

Expected incremental cost is **hundreds of tokens**, plausibly approaching **roughly 1–1.5k tokens** with both lists full, depending on tokenizer and coordinate/category lengths; this is an estimate, not a measured token bound. The fixed map can additionally grow toward its existing maximum when newly included items expand the crop. Record UTF-8 payload size and actual service usage during validation. No unbounded displayed descriptions or raw per-cell palette dump are added.

Alternatives considered:

- **Verbatim current glyph overlay:** rejected. It recreates raw-monster letter collisions; `*` gems collide with canonical creatures, `?` scrolls with generic unknown conventions, and several item symbols collide with terrain. A large legend still cannot recover hidden semantic identity or color once omitted.
- **Generic item marker only:** smaller and legend-safe but insufficient to distinguish a food-like display from coins or a potion. Chosen as the map layer, supplemented by the bounded category list.
- **Structured list only:** preserves the old map but obscures spatial overview and loses contents beyond its cap. Rejected as the sole mechanism; markers remain across the full bounded map.
- **Exact names from glyphs/messages:** rejected as unsupported inference. A future coordinate-bound public look-description channel could justify richer names, but it is not present in the inspected snapshot contract.
- **Room segmentation/BFS exit discovery:** rejected for this change. The remembered map is partial and lacks room identity; a deterministic landmark summary satisfies useful room-scale context without inventing boundaries or routes.

### 5.9 Implementation handoff (ordered)

1. Add fixture coverage for display-symbol collisions, absent-vs-current cells, all existing terrain classes, and stale terrain/current-screen contradictions.
2. Add the pure appearance helper in `tools/agent/instances.py`, preserving all existing navigation classification and memory semantics.
3. Extend `tools/agent/state.py::bounded_map` foreground/current-feature rendering and evidence bounds; leave other renderers untouched.
4. Update the fixed legend and add room schema helpers in `tools/agent/presentation.py`. Compute one map payload and reuse its rectangle for both lists.
5. Add category-level destination annotations through existing movement templates; retain safe degradation and immediate-action precedence.
6. Update exact snapshots, presentation version, and the approved presentation plan’s §4/§5 contracts. Record the intentional change from “items excluded” to “display appearances included.”
7. Run focused tests, complete auto-agent regression suite, and existing offline presentation/replay checks. An optional operator-approved live sample should compare payload size, item/door usefulness, concentration by N, acceptance, and applied counts; do not retune k in this phase.

### 5.10 Acceptance criteria

- **RA.1** Items/creatures/unclassified appearances come only from the current full snapshot; stale remembered objects/creatures never appear as current contents.
- **RA.2** Every emitted glyph column has a fixed legend entry; no raw glyph escapes. Gem `*`, demon `&`, scroll `?`, mimic `]`, and armor `[` obey the specified mappings.
- **RA.3** Map bounds include foreground content before list caps, preserve engine coordinates/margins/prefixes, and give confirmed hero final precedence.
- **RA.4** Exact contents categories, ordering, 16-record cap, omitted counts, and null-vs-empty behavior match the contract. Features and unclassified symbols remain visible without guessed identities.
- **RA.5** Openings use existing classification, map bounds, exact source labels, direction rule, 12-record cap and omitted counts; a current wall suppresses a stale door, and an item/creature over remembered stairs keeps source=memory.
- **RA.6** Openings never claim reachability, passability, line of sight, verified room exits, or clear occupancy. Closed doors and remote landmarks are named truthfully.
- **RA.7** Movement annotations refer to the actual adjacent action destination and same snapshot, not route target or capped-list order. No item/species/BUC/hostility/safety inference; no walking claim for direction/key answers.
- **RA.8** Rendering does not mutate terrain, occupancy, episode memory, snapshot, rejection state, or cycle history. Missing evidence causes bounded degradation, not a new refusal.
- **RA.9** Choice envelope, criteria object order, semantic identities, N, action bindings, acceptance gate, applied cap, and anti-oscillation ownership remain unchanged.
- **RA.10** Payload growth is structurally bounded; full-list and crop-expansion fixtures report bytes, and DeepSeek rendering/history regression snapshots remain unchanged.

### 5.11 Named tests

Proposed additions in verified existing test files; the implementer runs them. No tests were run for this design.

`test/agent/test_auto_instances.py`:
- `test_display_appearance_item_classes_are_closed_and_pinned`
- `test_display_appearance_gem_demon_scroll_mimic_armor_collisions`
- `test_display_appearance_reuses_color_aware_terrain_classifier`
- `test_display_appearance_does_not_change_walkability_or_memory`

`test/agent/test_auto_jev_presentation.py`:
- `TestJevRoomAwareness::test_fixed_legend_covers_every_emittable_glyph`
- `TestJevRoomAwareness::test_full_room_state_snapshot`
- `TestJevRoomAwareness::test_current_items_overlay_remembered_floor_and_stairs`
- `TestJevRoomAwareness::test_stale_item_and_creature_absent_from_snapshot_not_overlaid`
- `TestJevRoomAwareness::test_current_features_and_unknown_display_expand_map_bounds`
- `TestJevRoomAwareness::test_confirmed_hero_wins_and_nonhero_at_is_creature`
- `TestJevRoomAwareness::test_contents_nearest_first_cap_and_exact_omitted_count`
- `TestJevRoomAwareness::test_contents_row_major_without_hero`
- `TestJevRoomAwareness::test_openings_screen_memory_sources_and_current_wall_override`
- `TestJevRoomAwareness::test_remote_doors_stairs_precede_corridor_landmarks`
- `TestJevRoomAwareness::test_openings_eight_compass_directions_here_and_unknown_hero`
- `TestJevRoomAwareness::test_openings_cap_and_no_accessibility_claim`
- `TestJevRoomAwareness::test_room_null_empty_and_unavailable_sources`
- `TestJevRoomAwareness::test_destination_appearance_not_route_target_or_capped_list`
- `TestJevRoomAwareness::test_food_appearance_never_named_ration_or_safe`
- `TestJevRoomAwareness::test_direction_answer_has_no_walking_or_destination_claim`
- `TestJevRoomAwareness::test_conflicting_current_terrain_omits_stale_criterion_phrase`
- `TestJevRoomAwareness::test_cycle_recovery_movement_gets_destination_appearance` (a `recovery-step` or `random-move` movement action carries the destination clause from the actual adjacent square)
- `TestJevRoomAwareness::test_emergency_escape_movement_gets_destination_appearance` (an `escape` movement action carries the clause; emergency priority untouched)
- `TestJevRoomAwareness::test_search_wait_and_nonmovement_recovery_omit_destination_appearance` (search, wait, and nonmovement `unblock`/recovery actions carry no destination clause)
- `TestJevRoomAwareness::test_remembered_bars_render_in_map_without_structured_record`
- `TestJevRoomAwareness::test_renderer_is_pure_and_repeatable`
- `TestJevRoomAwareness::test_criteria_keys_order_indices_and_option_count_unchanged`
- `TestJevRoomAwareness::test_maximum_lists_and_crop_have_bounded_serialized_size`

`test/agent/test_auto_integration.py`:
- `test_room_enrichment_leaves_relative_acceptance_and_applied_cap_unchanged`
- `test_room_enrichment_uses_same_frozen_snapshot_for_map_and_criteria`
- `test_room_enrichment_does_not_change_deepseek_payload_or_history`

Keep existing map/criterion/refusal/legend coverage tests, changing expected snapshots only where this contract intentionally amends them. Mutation demonstrations: using persistent grid as contents source, emitting raw gem `*`, omitting unknown marker coverage, treating remembered stairs under a creature as current, or deriving criteria from the capped list must each fail a named test.

### 5.12 Review, documentation, risks and decisions

Review this as an explicit amendment to the approved presentation plan, especially the round-6 closed-legend lesson: enumerate **all** terrain outputs and **all** foreground output categories, including unknown future symbols. Do not merely test the glyphs in a happy-path room fixture. The relevant approved guarantee is present at `doc/agent-jev-presentation-plan.md:190,204`; no separately labeled round-6 section was found in the inspected material.

Document the exact fixed scope sentence, record caps, current-screen versus remembered-source semantics, item-symbol collisions, and lack of a true object-name/visibility channel. Snapshot tests should freeze field order, values, legend wording, list ordering, and omission counts. Review probability/cap diffs independently: this section authorizes no changes to those rules.

Decisions and residual risks:

1. **Chosen:** canonical markers plus capped categories, not raw glyph passthrough. This resolves the `*` gem/creature collision rather than asking Jev to infer which meaning applies.
2. **Chosen:** landmarks across the bounded map, not claimed room exits. The operator receives room-scale useful spatial evidence without a false room boundary or route guarantee.
3. **Chosen:** category wording only. Exact displayed object/species naming is deferred because the current map tuple has no established description binding.
4. **Limitation:** native display ambiguities and mimics remain ambiguous. Current classifier precedence is reused, not silently “fixed” by presentation. A spellbook resembling a door is not reliably distinguishable here.
5. **Limitation:** a snapshot is the player’s screen, not a freshness oracle. The fixed scope wording is essential; “current screen” must not become “currently visible physical object” in later templates.
6. **Risk:** marker choice `&` differs from native demon notation. The closed canonical legend and collision tests are mandatory; raw demon glyphs still map to `*`.
7. **Risk:** dense screens can fill caps. All foreground markers remain on the map, omitted counts are explicit, and criteria query the actual destination directly.
8. **Review check:** adding foreground evidence can expand the crop even though list sizes are capped; measure full serialized bytes and avoid claiming a token bound from list caps alone.

### 5.13 Non-goals

No engine traversal or hidden-state inspection; no new look commands, screen scraping outside the existing snapshot, or object-name inference; no room segmentation, pathfinding, route ranking, legal-move expansion, inventory mutation, species/hostility/BUC inference, or memory persistence for contents. No raw monster glyph emission, arbitrary text passthrough, dynamic legend, or changes to DeepSeek. No retuning of relative acceptance or applied-cap semantics.

This is a read-only design handoff. No files were modified and no tests or commands were executed.

## 6. Metrics and schema compatibility

Keep `longest_loop_span` and `loop_spans_ge_2` unchanged. `longest_loop_span` is the longest run of consecutive duplicate `(hero, displayed_time)` observations, counted after the first frame of the run; `loop_spans_ge_2` counts runs of length >= 2. These fields measure stationary duplicate frames only and cannot prove this fix succeeded — they are still not a measure of alternating movement.

For the validation report, separately derive consecutive confirmed AB alternations from wire observations, scoped to level instance, and distinguish key reversals from actual moved-cell reversals. A named test-local/report helper is enough; do not require a production metrics-schema migration. Report alternating-move share, longest alternating run, cycle-recovery activations, and discovered/entered cells alongside existing metrics.

Keep decision-sidecar keys and schema version stable. Consumers must tolerate additive reason strings. Controller metadata records the new mode/factor; ledger output adds applied count. Resolve the controlled-live reporting gap additively: per-episode records and the campaign summary gain `reflex.applied`, `paid_dispatched` (consulted/reserved), accepted, rejected, and fallback counts, with compatibility defaults (absent fields default to 0) so older consumers still parse. Aggregation is covered by `test_auto_integration.py::test_episode_and_campaign_summary_report_reflex_applied_and_consulted_counts`. Check any operator-owned campaign summarizer before deployment, particularly assumptions that `paid_dispatched <= reflex_call_cap` or that every `provider=jev` record proves a successful send.

## 7. Implementation plan (phased)

### Phase 0 — Pin regressions

Build canned provider responses and deterministic maps reproducing: a valid spread winning probability below 0.8; repeated rejected consultations followed by accepted choices; two frontier cells with a safe third exit; a true dead end; a stair/door route requiring reversal. Capture old behavior as expected-to-change evidence, not a new golden standard.

### Phase 1 — Probability contract and arbitration

Change neutral/provider value records, parser plumbing, arbitration, config/CLI/metadata, and confidence tests. Preserve Jev presentation bytes and DeepSeek rendering/history. Merge independently once AC.1–4 pass.

### Phase 2 — Applied cap

Add ledger accounting and complete-send charging; audit validation substitution, forced override, transport failures, and repair paths. Preserve paid settlement. Merge independently once AC.5–7 pass.

### Phase 3 — Navigation

Add confirmed movement history/accessors and bounded anti-backtrack filtering. Connect cycles to safe recovery. Test dead ends, corner/door legality, emergency and forced-search ownership, repeated preparation purity, and instance reset. Merge independently once AC.8–11 pass.

### Phase 4 — Room-awareness enrichment

Implement §5: the pure display-appearance helper in `instances.py`; `bounded_map` foreground/current-feature rendering and evidence bounds in `state.py`; the new fixed legend and `room` schema helpers in `presentation.py`; destination-appearance enrichment via the shared pure helper (see §5.7 wiring below); presentation-version bump; regenerated exact snapshots and golden fixtures. Gate: RA.1–RA.10 and their named tests pass; DeepSeek regression snapshots unchanged. Scope note: "Preserve Jev presentation bytes" applies to Phases 1–3 commits only; Phase 4 intentionally amends the Jev state payload and criteria per the narrow presentation boundary.

### Phase 5 — Review, replay, and controlled live validation

Run the full existing auto-agent suite plus added tests. Run offline replay for scripted behavior/artifact compatibility; do not claim offline replay exercises live Jev. Then conduct an operator-approved live comparison, recording per episode and across the campaign: mode/factor, `reflex.applied` and `paid_dispatched` (consulted/reserved) counts, accepted/rejected/fallback counts, N/p acceptance distribution, payload sizes, costs, alternation metrics, coverage, and safety outcomes. Reject success claims based solely on Jev acceptance rate or stationary loop metrics.

## 8. Acceptance criteria

- **AC.1** Relative mode uses validated selected probability and exact offered N; service confidence cannot change its outcome.
- **AC.2** Strict p>1.5/N boundaries, binary reachability, monotonicity, finite/type validation, singleton bypass, and uniform rejection hold.
- **AC.3** Parse, identity, membership, rejection, and eligibility gates remain effective at arbitrarily high p.
- **AC.4** New default/config validation/metadata are consistent; explicit absolute mode preserves old threshold semantics and rollback.
- **AC.5** Only complete sends of unoverridden locally valid Jev proposals increment applied count; cap C permits exactly C such applications and then suppresses paid consultation.
- **AC.6** Rejections/skips/abstentions/errors do not spend applied allowance but remain recorded; paid usage remains settled exactly once.
- **AC.7** Decision schema is unchanged and additive ledger/config reporting works with existing consumers.
- **AC.8** Comparable exploratory alternatives avoid immediate reversal; rejection filtering cannot eliminate the only usable retreat.
- **AC.9** AB cycling enters safe recovery by approximately four alternating moves even with no_progress=0; period-three detection still works.
- **AC.10** Dead-end exits, uniquely best stairs/door approaches, legal corners, danger disengagement, search budgets, and forced-search ownership remain intact.
- **AC.11** Navigation history advances only on confirmed observations; duplicates/proposals/failed sends cannot fabricate motion, and unknown/instance changes invalidate stale evidence.
- **AC.12** DeepSeek renderer/history and reservation invariants remain unchanged; validation distinguishes stationary-frame loops from actual movement alternation.

## 9. Test strategy (named tests)

All following added names are proposed tests in existing verified test files unless identified as existing. The implementer should run them; none were run during this review.

### Confidence — `test/agent/test_auto_jev_presentation.py`

Retain `TestJevConfidence::test_confidence_gate_threshold_unchanged_for_spread_distribution`, but explicitly run absolute mode and revise its docstring to "legacy rollback coverage." Preserve all old scalar boundary assertions. This honestly preserves its meaning in the supported legacy branch rather than deleting it or asserting default behavior remains unchanged.

Add:
- `TestJevConfidence::test_relative_gate_uses_selected_probability_not_reported_confidence`
- `TestJevConfidence::test_relative_gate_thresholds_for_two_through_six_options`
- `TestJevConfidence::test_relative_gate_strict_boundary_and_uniform_rejection`
- `TestJevConfidence::test_relative_gate_monotone_in_probability_and_option_count`
- `TestJevConfidence::test_relative_gate_rejects_missing_bool_nonfinite_probability`
- `TestJevConfidence::test_relative_gate_preserves_identity_rejection_and_safety_checks`
- `TestJevConfidence::test_relative_gate_counts_retained_offered_actions_not_targets`
- `TestJevConfidence::test_spread_winner_passes_relative_and_fails_legacy_absolute`

### Provider/config — `test/agent/test_auto_providers.py`

- `test_jev_parser_preserves_selected_probability_and_legacy_confidence`
- `test_jev_parser_rejects_bad_vectors_and_nonmax_selection`
- `test_jev_confidence_config_defaults_validation_and_absolute_rollback`
- `test_live_and_evaluate_cli_propagate_jev_confidence_policy`

### Cap/controller — `test/agent/test_auto_integration.py`

- `test_jev_rejections_do_not_exhaust_applied_cap`
- `test_jev_skips_abstentions_and_timeouts_leave_applied_allowance`
- `test_jev_cap_stops_after_exactly_c_complete_applied_sends`
- `test_jev_validation_fallback_and_forced_override_do_not_charge_applied`
- `test_equal_action_forced_override_uses_provenance_not_dict_equality` — an override whose action dict coincides with the Jev proposal still does not charge applied: provenance, not dict equality, decides.
- `test_jev_failed_or_partial_send_does_not_charge_applied`
- `test_jev_delivery_repair_does_not_double_charge_decision` — full first Jev send → `invalid(incomplete)` → page repair → resend; asserts one provider call, one applied increment, two sent ordinals, unchanged paid settlement.
- `test_jev_applied_send_later_native_invalid_is_not_refunded`
- `test_jev_rejected_usage_settled_once_and_sidecar_retained`
- `test_jev_cap_zero_and_monetary_admission_remain_fail_closed`
- `test_episode_and_campaign_summary_consumer_compatibility_with_reflex_applied` — consumer-level compatibility (no BudgetLedger deserializer is added): feed `_episode_summary`, `campaign_summary`, and any replay/report reader both an old artifact dictionary lacking `reflex.applied` (defaults to 0) and a new dictionary containing it (count preserved).

### Navigation — `test/agent/test_auto_navigation.py`

- `test_navigation_prefers_comparable_nonbacktracking_frontier`
- `test_navigation_backtrack_filter_applies_before_action_deduplication`
- `test_navigation_ignores_rejected_nonbacktracking_alternative`
- `test_navigation_preserves_only_dead_end_exit`
- `test_navigation_preserves_uniquely_best_reverse_stair_and_door_routes`
- `test_antibacktrack_score_exception_at_40_41_and_directive_bonus` — a 40-point margin **suppresses** the reversal, a 41-point margin **keeps** it, and the +30 directive contribution participates in the compared score; the mutation check flips `>` to `>=` and must fail the equality case, and the +30 directive bonus participates in the compared scores.
- `test_navigation_preparation_does_not_advance_movement_history`
- `test_cycle_recovery_obeys_door_diagonal_and_corner_legality`
- `test_emergency_disengagement_may_reverse`

Keep existing `test_no_progress_still_searches_in_place`, stair/directive tests, and EdgeLegality tests.

### Recovery — `test/agent/test_auto_recovery.py`

- `test_ab_cycle_enters_recovery_with_zero_stationary_no_progress`
- `test_cycle_recovery_chooses_third_exit_by_four_alternating_moves`
- `test_duplicate_observations_do_not_dilute_movement_cycle`
- `test_duplicate_observation_before_detection_preserves_previous_cell` — a duplicate immediately before detection preserves `previous_distinct` and the trailing history.
- `test_duplicate_observation_after_detection_keeps_cycle_active` — a duplicate immediately after detection preserves `cycle_active`.
- `test_door_opening_stationary_observation_keeps_cycle_active` — a door-opening key that leaves the hero stationary is not a new traversed edge and does not clear an active cycle.
- `test_adjacent_third_exit_clears_cycle_active` — the first off-cycle adjacent move after a third-exit step clears `cycle_active`.
- `test_unknown_or_relocated_hero_invalidates_backtrack_evidence`
- `test_unknown_hero_clears_movement_history_and_cycle` — a `None` hero clears history, previous-cell evidence, and `cycle_active`.
- `test_nonadjacent_relocation_clears_movement_history_and_cycle` — a Chebyshev-nonadjacent relocation clears history, previous-cell evidence, and `cycle_active`.
- `test_period_three_trailing_movement_preserved` — an ABCABC trailing movement keeps period-three detection.
- `test_failed_proposal_does_not_fold_motion_history` — a produced-but-uncommitted proposal or rejected candidate does not advance movement history.
- `test_failed_and_partial_act_write_do_not_fold_motion_history` — a failed or partial action write does not advance movement history or clear an active cycle.
- `test_stationary_recovery_ladder_3_6_10_unchanged` — stationary `no_progress` 3/6/10 recovery semantics are unchanged by cycle recovery.
- `test_cycle_with_only_reverse_exit_does_not_quit`
- `test_cycle_without_exit_respects_search_budget_and_nomination`
- `test_instance_reset_clears_previous_cell_and_cycle`

Preserve existing `CycleDetectorTest::test_ab_oscillation`, `test_abc_cycle`, `test_stationary_is_not_a_cycle`; explicitly update `test_unknown_position_is_ignored` if continuity semantics change, replacing its old assumption with the stronger no-false-cycle invariant.

### Forced search, artifacts, metrics, cache

- `test_auto_forced_search.py::test_cycle_recovery_does_not_steal_forced_suffix_ownership`
- `test_auto_replay.py::test_additive_jev_reasons_preserve_decision_sidecar_loading`
- `test_auto_metrics.py::test_alternating_motion_is_distinct_from_stationary_loop_spans`
- `test_auto_metrics.py::test_validation_report_counts_confirmed_alternating_moves`
- `test_auto_integration.py::test_episode_and_campaign_summary_report_reflex_applied_and_consulted_counts`
- `test_auto_integration.py::test_deepseek_rendering_snapshot_unchanged` (existing prior-plan regression target; verify exact current fixture/test availability before relying on it).

Run all `test/agent/test_auto_*.py`, not just the new tests. Mutation checks: swapping selected probability for service confidence, restoring reservation-based admission, charging before emit, deleting `_cycled` recovery routing, or ignoring previous-cell preference must each fail a specifically named test above.

### Acceptance-criteria to test map

Each AC and RA below must be covered by the named tests (this mirrors the presentation plan's §11 AC-to-test table).

| AC | Named tests |
|---|---|
| AC.1 | `TestJevConfidence::test_relative_gate_uses_selected_probability_not_reported_confidence`; `TestJevConfidence::test_relative_gate_counts_retained_offered_actions_not_targets` |
| AC.2 | `TestJevConfidence::test_relative_gate_thresholds_for_two_through_six_options`; `TestJevConfidence::test_relative_gate_strict_boundary_and_uniform_rejection`; `TestJevConfidence::test_relative_gate_monotone_in_probability_and_option_count`; `TestJevConfidence::test_relative_gate_rejects_missing_bool_nonfinite_probability` |
| AC.3 | `TestJevConfidence::test_relative_gate_preserves_identity_rejection_and_safety_checks` |
| AC.4 | `test_jev_confidence_config_defaults_validation_and_absolute_rollback`; `test_live_and_evaluate_cli_propagate_jev_confidence_policy`; `TestJevConfidence::test_spread_winner_passes_relative_and_fails_legacy_absolute`; `TestJevConfidence::test_confidence_gate_threshold_unchanged_for_spread_distribution` (absolute-mode legacy rollback) |
| AC.5 | `test_jev_cap_stops_after_exactly_c_complete_applied_sends`; `test_jev_validation_fallback_and_forced_override_do_not_charge_applied`; `test_equal_action_forced_override_uses_provenance_not_dict_equality` |
| AC.6 | `test_jev_rejections_do_not_exhaust_applied_cap`; `test_jev_skips_abstentions_and_timeouts_leave_applied_allowance`; `test_jev_rejected_usage_settled_once_and_sidecar_retained`; `test_jev_delivery_repair_does_not_double_charge_decision`; `test_jev_failed_or_partial_send_does_not_charge_applied`; `test_jev_applied_send_later_native_invalid_is_not_refunded` |
| AC.7 | `test_jev_rejected_usage_settled_once_and_sidecar_retained`; `test_episode_and_campaign_summary_consumer_compatibility_with_reflex_applied`; `test_additive_jev_reasons_preserve_decision_sidecar_loading`; `test_jev_cap_zero_and_monetary_admission_remain_fail_closed` |
| AC.8 | `test_navigation_prefers_comparable_nonbacktracking_frontier`; `test_navigation_backtrack_filter_applies_before_action_deduplication`; `test_navigation_ignores_rejected_nonbacktracking_alternative`; `test_antibacktrack_score_exception_at_40_41_and_directive_bonus` |
| AC.9 | `test_ab_cycle_enters_recovery_with_zero_stationary_no_progress`; `test_cycle_recovery_chooses_third_exit_by_four_alternating_moves`; `test_period_three_trailing_movement_preserved`; `CycleDetectorTest::test_abc_cycle` |
| AC.10 | `test_navigation_preserves_only_dead_end_exit`; `test_navigation_preserves_uniquely_best_reverse_stair_and_door_routes`; `test_cycle_recovery_obeys_door_diagonal_and_corner_legality`; `test_emergency_disengagement_may_reverse`; `test_cycle_without_exit_respects_search_budget_and_nomination`; `test_auto_forced_search.py::test_cycle_recovery_does_not_steal_forced_suffix_ownership` |
| AC.11 | `test_navigation_preparation_does_not_advance_movement_history`; `test_duplicate_observations_do_not_dilute_movement_cycle`; `test_unknown_or_relocated_hero_invalidates_backtrack_evidence`; `test_instance_reset_clears_previous_cell_and_cycle`; `test_failed_and_partial_act_write_do_not_fold_motion_history` |
| AC.12 | `test_auto_providers.py::test_history_grows_across_successful_calls`; `test_auto_providers.py::test_history_reflects_only_successful_settlement`; `test_auto_providers.py::test_cancellation_adds_no_history`; `test_auto_providers.py::test_history_inclusive_bound_refuses_while_the_tail_alone_fits`; `test_auto_providers.py::test_a_cache_price_never_lowers_a_reservation`; `test_auto_metrics.py::test_alternating_motion_is_distinct_from_stationary_loop_spans`; `test_auto_metrics.py::test_validation_report_counts_confirmed_alternating_moves`; `test_auto_integration.py::test_deepseek_rendering_snapshot_unchanged` |
| RA.1 | `TestJevRoomAwareness::test_stale_item_and_creature_absent_from_snapshot_not_overlaid`; `TestJevRoomAwareness::test_current_items_overlay_remembered_floor_and_stairs` |
| RA.2 | `TestJevRoomAwareness::test_fixed_legend_covers_every_emittable_glyph`; `test_auto_instances.py::test_display_appearance_gem_demon_scroll_mimic_armor_collisions` |
| RA.3 | `TestJevRoomAwareness::test_current_features_and_unknown_display_expand_map_bounds`; `TestJevRoomAwareness::test_confirmed_hero_wins_and_nonhero_at_is_creature` |
| RA.4 | `TestJevRoomAwareness::test_full_room_state_snapshot`; `TestJevRoomAwareness::test_contents_nearest_first_cap_and_exact_omitted_count`; `TestJevRoomAwareness::test_contents_row_major_without_hero`; `TestJevRoomAwareness::test_room_null_empty_and_unavailable_sources` |
| RA.5 | `TestJevRoomAwareness::test_openings_screen_memory_sources_and_current_wall_override`; `TestJevRoomAwareness::test_remote_doors_stairs_precede_corridor_landmarks`; `TestJevRoomAwareness::test_openings_eight_compass_directions_here_and_unknown_hero`; `TestJevRoomAwareness::test_openings_cap_and_no_accessibility_claim` |
| RA.6 | `TestJevRoomAwareness::test_openings_cap_and_no_accessibility_claim` |
| RA.7 | `TestJevRoomAwareness::test_destination_appearance_not_route_target_or_capped_list`; `TestJevRoomAwareness::test_cycle_recovery_movement_gets_destination_appearance`; `TestJevRoomAwareness::test_emergency_escape_movement_gets_destination_appearance`; `TestJevRoomAwareness::test_search_wait_and_nonmovement_recovery_omit_destination_appearance`; `TestJevRoomAwareness::test_food_appearance_never_named_ration_or_safe`; `TestJevRoomAwareness::test_direction_answer_has_no_walking_or_destination_claim`; `TestJevRoomAwareness::test_conflicting_current_terrain_omits_stale_criterion_phrase` |
| RA.8 | `TestJevRoomAwareness::test_renderer_is_pure_and_repeatable`; `TestJevRoomAwareness::test_room_null_empty_and_unavailable_sources` |
| RA.9 | `TestJevRoomAwareness::test_criteria_keys_order_indices_and_option_count_unchanged`; `test_auto_integration.py::test_room_enrichment_leaves_relative_acceptance_and_applied_cap_unchanged`; `test_auto_integration.py::test_room_enrichment_uses_same_frozen_snapshot_for_map_and_criteria` |
| RA.10 | `TestJevRoomAwareness::test_maximum_lists_and_crop_have_bounded_serialized_size`; `test_auto_integration.py::test_room_enrichment_does_not_change_deepseek_payload_or_history` |

`TestJevConfidence` and `TestJevRoomAwareness` live in `test/agent/test_auto_jev_presentation.py`; unqualified names in the Cap/controller, Navigation, and Recovery rows live in their named test files.

## 10. Review strategy

Review against AC.1–12 and RA.1–10, with special attention to probability provenance, final-send provenance, exactly-once settlement, and observation-owned state. Review each independent phase before combined live validation. Inspect actual diff for inadvertent presentation/DeepSeek changes. Re-run the complete suite after fixes; report actual counts rather than assuming the caller's 805 baseline is still current.

**Implementation-review loop (required):** after the full suite and mutation checks pass, dispatch the prescribed reviewer on the actual diff against AC.1–12 and RA.1–10 plus the cache and presentation invariants; fix or explicitly rebut every finding; repeat the review after each round of fixes until no Critical/Important finding remains or an operator decision is explicitly required.

For live validation, tabulate relative acceptance by N and p; applied count must never exceed cap although paid consultation count may. Use a canned accepted-response integration test as deterministic proof of restored participation; a live service is not required to produce concentrated distributions. On deterministic branching maps, verify the hero takes a third exit rather than continuing an avoidable AB run. Real-campaign improvements are observational evidence, not substitutes for safety tests.

## 11. Documentation strategy

- Update prior presentation plan §7, AC.11 commentary, D3, and non-goals with a clearly dated/linked follow-up note: D3 is approved based on the caller's campaign evidence, and old invariant statements are historical, superseded here.
- Document relative concentration-not-permission semantics, exact strict boundaries, selected probability versus service confidence, and absolute rollback.
- Document `reflex_call_cap` as an applied-decision limit; explain that rejected paid consultations still cost money and that `paid_dispatched` retains its historical diagnostic semantics.
- Document anti-backtrack exceptions and observation-owned history. Do not claim persisted target invalidation was added when the primary path does not use TargetStore.
- Document that legacy loop-span metrics measure identical stationary frames. Include separate alternating-motion results in the validation report.
- Do not edit `doc/agent-cache-plan.md` to weaken its invariants.

## 12. Risks and decisions

- **D1 decided:** k=1.5, strict comparison, no floor/cap. Chosen for attainable binary acceptance and a clear relative interpretation. This is a policy heuristic, not calibrated risk probability.
- **D2 decided:** relative is the new default; explicit absolute mode retains the old threshold and enables rollback. Existing config files parse but default behavior intentionally changes.
- **D3 operator-approved:** supersede the flat choice gate based on supplied campaign evidence. Safety/eligibility remain independent.
- **D4 operator-approved:** rejected/abstained consultations do not consume the applied cap. Increased unsuccessful paid consultation volume is expected; no new hidden attempt cap.
- **D5 design decision:** "applied" means complete send, not provider acceptance and not successful movement. Failed wire writes and forced overrides do not count; later game rejection does.
- **D6 design decision:** bounded same-family anti-backtrack preference rather than global edge prohibition, persistent-target redesign, or larger visit penalties. A global ban breaks dead ends; larger penalties alone saturate and do not activate recovery.
- **D7 design decision:** reuse existing cycle observation ownership and recovery ladder, not a parallel key-string detector. Keys do not reliably imply displacement.
- **D8 risk:** eligible-but-malicious service distributions can manipulate concentration; this was already an untrusted provider boundary. Exact vector/max checks and independent action safety remain mandatory.
- **D9 risk:** source-derived frontier explanation is plausible, not campaign-level attribution. Before declaring root cause proven, label recorded oscillating decisions by their selected family/reason where artifacts permit.

## 13. Non-goals

No changes to NetHack C gameplay, candidate action vocabulary, DeepSeek prompts/history/cache accounting (including its renderer and the `state.render_map` default), provider transport, strategy directives, forced-search permission gates, paid-network replay support, persisted-target architecture, or production campaign metric meanings. The room-awareness section changes only the Jev state payload and movement criterion text: it may enrich those, must bump the presentation version, update exact snapshots/golden fixtures, and remain pure. It must not alter the request-envelope or retained-candidate contract: no add/drop/reorder of retained candidates (N is frozen from `prepared.table.ordered_candidates` before presentation; presentation refusal remains whole-request), and no change to `key_index`, table identity, option keys, or response probability-key equality. No dependency additions. No guaranteed live acceptance percentage or universal elimination of all legitimate backtracking.

## Reviewer scrutiny

1. Confirm offered criteria remain a one-to-one cover of retained candidates for every supported need; if future filtering appears, carry the immutable offered count explicitly rather than using a mismatched table size.
2. Confirm every live/fake provider construction path sets selected probability in relative mode. Missing data must be a deliberate fail-closed test, not silently accepted via confidence fallback.
3. Audit delivery-repair paths beyond `_answer_now` for exact once-per-decision applied charging. This targeted inspection identified the complete-send boundary but did not exhaustively symbol-analyze all repair paths.
4. The 40-point same-family preference is intentionally tied to the existing bounded path adjustment. Review directive contribution interactions and campaign fixtures before freezing its value; do not broaden it into cross-family suppression accidentally.
5. Consecutive-position deduplication changes cycle semantics across stationary frames. Unknown/nonadjacent relocation invalidation and door-opening tests must rule out stale-cell false positives without hiding genuine repeated motion.
6. The existing random fallback's known-passable check is weaker than explicit edge legality. Keep the safe cycle branch narrowly tested; do not make unrelated random-walk behavior changes without coverage.
7. An operator-owned campaign summarizer was not located. Compatibility with external tooling remains a handoff check, especially cap interpretation and exact reason-string parsing.
8. No files were written, commands executed, dependencies installed, or tests run during this design. All validation actions above are implementation requirements; the campaign and 805-test baseline are caller-supplied evidence.
