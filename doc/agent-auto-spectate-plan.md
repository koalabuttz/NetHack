# Live autoplay spectating — Revision 3 (implementation handoff)

Design for live spectating of `./agent.sh auto` campaigns. Revision 3 =
Revision 2 (architect) + the two plan-review corrections (render-wake
deadline capping; transactional TTY painter). Supersedes Revision 2 in full.
This is a design, not a patch; a coding agent implements it.

## Recommendation

Extract deterministic presentation into `tools/agent/render.py` and add an
episode-local synchronous sink in proposed `tools/agent/spectating.py`. Use
the existing controller boundary for observation offers, the existing select
loop for trailing-frame delivery, and a newly composed final frame after
final outcome resolution.

Transport: **select-gated writes** in chunks no larger than PIPE_BUF,
independently opened destinations made nonblocking, a 0.25-second per-frame
deadline, and disable after three consecutive exhausted frames. No helper
process or rendering thread. Every render attempt is additionally capped by
any active wire deadline (Revision 3 correction 1). TTY painter state is
transactional: height commits only after complete delivery, and a partial
TTY write forces absolute resynchronization before the next ordinary frame
(Revision 3 correction 2).

## 1. Requirements, constraints, and success criteria

- Stdlib-only, synchronous rendering; no new threads, subprocesses,
  protocol readers, provider calls, or timer services.
- `--spectate [tty|stderr|none]`; omitted flag defaults to none, bare flag
  means stderr. Never send display bytes through fd 1 or the game wire.
- `--spectate-interval SECONDS`, default 0.15; zero is unthrottled. Reject
  negative, NaN, and infinite intervals before launching episodes.
- One observation candidate per successfully applied and need-validated
  snapshot. Throttling may coalesce candidates, retaining only the newest.
- Final rendering is an additional, freshly composed frame; it is not a
  forced write of a stale observation candidate.
- 80x21 describes the map. Status, messages, need, directives, and counters
  are additional compact lines. Source/prose follow the 78-column
  convention; mandatory 80-column map output is an exception.
- Presentation failures cannot alter recorder health, provider policy,
  budget settlement, event emission, outcome, or campaign exit accounting.
- Only two new metadata keys are permitted; no new recording sidecars,
  config fields in recordings, campaign-summary fields, or schema version.
- Existing spectate proxy/replay output and behavior remain byte-identical.
- Preserve evaluator determinism and provider/budget inputs.

Operational acceptance statement (deadline/disable contract):

> A stalled render destination never blocks the controller longer than the
> per-frame deadline (capped by any active wire deadline), never corrupts or
> delays the wire beyond that bounded window, and disables itself after
> repeated stalls.

Honest limitation published alongside it: synchronous deadlines cannot
preempt a blocking syscall in an exceptional stderr race or pathological
device/filesystem stall (see §5 residual). Do not present the contract as a
mathematical all-sinks guarantee.

## 2. Existing architecture and evidence (verified by two reviews)

- `tools/agent/controller.py:914-965`: `_on_obs` checks sequence, applies
  Snapshot, observes memory, detects boundaries, validates need, sets
  pending state. `last_seq` is assigned at line 921, BEFORE apply/need
  validation — not a valid spectator-success marker. `Snapshot.apply` is
  not fully atomic (`protocol.py:109-150`: pal/map assigned before cursor/
  windows can fail) — candidate capture must be post-success.
- `controller.py:624-660`: `_service_strategy` precedes pending page/action
  handling. `_cancel_strategy` and `_maybe_postmortem` precede `_finish`.
- `controller.py:662-692`: `_finish` populates result counters, resolves
  unanswered closure/stop reason, infers outcome LAST. Final composition
  belongs after its current last statement.
- `controller.py:805-835`: `_readline` selects wire stdout with
  `min(remaining, 1.0)` under an absolute `bound = min(deadline,
  self.deadline)`. This is the trailing-render wakeup integration site.
- `controller.py:1495-1544`: `_answer_now` selects an action, activates
  pending directives at line 1525, obtains book.view. Do not reorder.
- `controller.py:786-797`: recorder failure changes rec_healthy, disables
  paid work, cancels strategy. Renderer failures must never enter it.
- `tools/agent/protocol.py:85-150`: every observation is a complete current
  presentation (full snapshot); Snapshot.map is the presentation authority.
- `tools/agent/state.py:284-340`: EpisodeMemory.grid is the remembered
  79-col strategy view — NOT presentation; recent_messages(3) is episode
  history. Status/messages suffice for annotations.
- `tools/agent/directives.py:232-260`: view() calls active() which may
  expire directives and log events — display must never call it.
- `test/agent/spectate.py:165-325`: pure format helpers (_sgr, _map_row,
  _status_line, _need_text, obs_frame, one_liner, FramePainter with
  height-commit semantics at 293-319) are the extraction seam. Its
  transport is LiveRenderSession — leave it in the test-side tool.
- `tools/agent/codec.py` imports only json; render.py -> codec is cycle-free.
  format_obs.py already re-exports codec.
- `tools/agent/budget.py:295-306,530-568` and `__main__.py:224-235`:
  classified-token cache reporting exists; reuse it.
- `agent.sh:114-122`: auto arguments pass through via "$@".
- `recording.py:164-302`: four sidecar writers + meta; no writer change.

## 3. Presentation module and frame contract

### `tools/agent/render.py` (pure; no I/O, clocks, policy, controller)

Move and re-export from spectate.py without semantic change:

    def _sgr(cell)
    def _map_row(grid, cur, y, color)
    def _status_line(rec)
    def _need_text(need, windows)
    def obs_frame(rec, messages=DEFAULT_MESSAGES, color=True)
    def one_liner(rec)
    class FramePainter  # messages/color/tty ctor; frame/obs/note

Move color/style tables, reset sequence, DEFAULT_MESSAGES. Import codec
directly (map_grid lives there). Legacy formatting keeps its exact bordered
map, Unicode handling, ANSI output, painter behavior.

New autoplay API:

    def auto_frame(snapshot: Snapshot, memory: EpisodeMemory, *,
                   episode: int, seq: int, tick: int,
                   need: Optional[dict], windows: Sequence[dict],
                   directives: DirectiveView,
                   strategy_calls: int, usage: Mapping[str, Any],
                   messages: int = DEFAULT_MESSAGES,
                   final_reason: Optional[str] = None,
                   outcome: Optional[str] = None) -> List[str]:

Reads arguments synchronously, retains none, returns detached strings.
Use snapshot.windows for need titles; explicit need overrides snapshot.need.
No DirectiveBook or BudgetLedger argument.

Frame contract:

- Header: episode, successful snapshot seq, display tick; final frame adds
  `final`, stop reason, resolved visible outcome.
- Status from memory.status: level, HP/max, time, XP, hunger, gold; unknown
  is `?`, never invented zero.
- Map from the successfully applied current Snapshot.map ONLY (Revision 3:
  NOT memory.grid). Missing cells blank; exactly 21 rows of 80 ASCII cells
  with synthesized blank x=0; no borders widening rows.
- Cursor from Snapshot.cur with the `*` overlay at its valid coordinate;
  never inferred from remembered hero position; cells not mutated.
- Last three `memory.recent_messages(3)` (episode history, documented: a
  message-less new observation retains prior messages).
- Need line: observation frames show the need AS OF that observation; final
  shows current outstanding need (`pending_need if pending else None`).
- Active directives via peeked view: goals, target, risk, TTL/generation;
  `directives: none` when inactive. Never pending advice or response bodies.
- Counters: ledger.strategy_dispatched; cache hit/miss/unclassified with
  hit% over hit+miss only; n/a when no classified tokens.
- Annotation lines sanitized to printable ASCII, deterministically clipped
  to 80 columns; one-cell ASCII fallback for non-ASCII map glyphs.
  Autoplay-only policy; legacy spectator formatting unchanged.
- Plain (color=False) for autoplay; TTY redraw sequences only via
  FramePainter on a TTY; no new color CLI.

### Observation candidate lifetime

At the END of successful `_on_obs` (after apply, memory observe, boundary
detection, need validation, pending setup ALL succeed — never from
last_seq), guarded capture of render-only evidence: a frozen copy of the
accepted Snapshot presentation (map/cursor/windows/need — deep-copy the
nested presentation containers; Snapshot.apply is not atomic, so
post-success capture compensates), observation need, memory status/history
references. Bounded: only the latest accepted copy. None mode copies
nothing.

At the next run boundary after `_service_strategy`, compose the observation
candidate once (peeked directives, counters), detach into an immutable
tuple, and offer it. Pages, invalid retries, action sends, and close
transitions never re-annotate an emitted candidate. A delayed frame may
truthfully show a need already answered; its header identifies the
observation. Advice settled in _service_strategy is pending until
_answer_now activates it — it first appears in the NEXT snapshot's frame,
never retroactively; the final frame may show it if currently active.
Display eligibility uses peek_view at composition time.

### Final frame lifetime

After `_cancel_strategy`, `_maybe_postmortem`, all `_finish` counters, stop/
unanswered resolution, and outcome inference: freshly call auto_frame with
the last accepted Snapshot presentation, current peeked directives, settled
counters, and CURRENT outstanding need. Replace/discard any older pending
candidate and force the fresh frame once, even inside the interval. Never
implement final rendering as flush(force=True) of a prior candidate. No
accepted snapshot → no fabricated frame. Disabled rendering is not revived.

## 4. Read-only directives: one eligibility authority

In `tools/agent/directives.py`, factor a pure helper:

    def _ineligibility_reason(dset, activated_tick, activated_level, *,
                              tick, level, st) -> Optional[str]:

None = eligible/no set; else existing precedence: `level-changed`,
`ttl-expired`, `precondition-failed`. Preserve exact `tick -
activated_tick > dset.ttl` semantics (equality eligible) and None-level
behavior. `active()` calls it and retains expire/log behavior; `view()`
continues to call active(). Add:

    def peek_view(self, tick, level, st) -> DirectiveView:

Same helper, never expire/log/sink/mutate. Return the existing
DirectiveView (generation when eligible, inactive generation zero). No
mirrored predicate implementation.

## 5. Transport, scheduling, and failure handling

### `tools/agent/spectating.py`

    FRAME_WRITE_TIMEOUT = 0.25
    CONSECUTIVE_STALL_LIMIT = 3
    DEFAULT_INTERVAL = 0.15

    def validate_spectate(destination, interval) -> Optional[str]
    def open_destination(destination) -> RenderDestination
    class RenderDestination:
        def write(self, data: bytes, *, deadline: float) -> bool
        def close(self) -> None
    class RenderStream:
        def __init__(self, destination, interval, *, clock=time.monotonic,
                     write_timeout=FRAME_WRITE_TIMEOUT, diagnostic=None)
        def offer(self, lines) -> None
        def next_due(self) -> Optional[float]
        def flush(self, force=False) -> None
        def finish(self, lines=None) -> None
        def disable(self, reason) -> None
        def close(self) -> None

write returns True only for a fully written payload, False on deadline
exhaustion, raises ordinary errors for the enclosing guard. Inject
clock/select/write seams for deterministic tests. Deadline and clock share
the monotonic domain. Read-only `frames_rendered`, `frames_dropped`,
`disabled_reason`. Dropped frames are renderer-local; only
frames_rendered and disabled_reason reach EpisodeResult/meta (two keys
total; no third key). Coalesced-away candidates have a private count.

### Destination preparation and isolation

- `stderr` uses OS fd 2 regardless of sys.stderr reassignment. Own a
  duplicate; close only the duplicate; NEVER alter its status flags
  (duplicates share the open-file description). Normally blocking.
- `tty` separately opens /dev/tty; on failure falls back to fd 2 with one
  best-effort note per campaign.
- Independently opened tty/file destinations: set O_NONBLOCK on the owned
  descriptor at open (own open-file description — safe).
- Distinguish `owns_fd` from `independent_open_description`.
- Reject fd 1 and detectable stdout aliases: same FIFO, regular-file inode,
  or duplicated socket identity via fstat. Check on tty fallback too,
  before either note or frame is written. Conservative same-file rejection
  acceptable. Independently opened terminal devices keep legacy behavior.
- If open/dup allocates fd 0/1/2 because it was closed, relocate the owned
  fd to >=3 and restore fd 1 to CLOSED, not repurposed.
- No child stdin/stdout or recorder fd reaches the sink. `none` opens,
  dups, and probes nothing; performs no clock work.
- Open failure disables spectating for the episode, not spawn/gameplay.

### Bounded writes (Revision 3 correction 1)

For each attempt establish ONE absolute deadline = monotonic now + 0.25,
NEVER reset after progress, EINTR, partial write, or EAGAIN. When invoked
from _readline under an active wire bound, the frame deadline is further
capped: `min(now + 0.25, wire_bound)`. If the wire bound is already
exhausted, skip/drop the frame and let the existing wire deadline path run
— a render wake must never extend the wire timeout.

1. Safe chunk bound: PIPE_BUF via fpathconf where available, else
   select.PIPE_BUF fallback.
2. Before EVERY write: check remaining time, then select for writable with
   that remaining timeout; recompute after interruptions.
3. After writable: recheck time, write at most
   min(PIPE_BUF, remaining_payload).
4. Partial writes advance the offset and return to select; EAGAIN returns
   to select; unexpected zero write = write-error.
5. Deadline exhaustion: stop, discard the rest, increment frames_dropped
   and the consecutive-exhaustion streak. Never resume a dropped frame's
   suffix; a partially delivered frame is not counted as rendered.
6. Full delivery increments frames_rendered and resets the streak.
   Coalescing/idle does not reset it.
7. Third consecutive exhausted frame disables the episode with reason
   `write-deadline` and diagnostic
   `spectate disabled: write-deadline (3 consecutive frame stalls)`.
   Other ordinary faults disable immediately with fixed categories.

A partial frame may remain visible on the side channel — not wire
corruption. Painter redraw-state handling is transactional (correction 2,
below).

Diagnostics: best-effort, sanitized, attempted once, same fd safety and
select/chunk discipline, no fresh blocking allowance after an exhausted
frame; if no safe sink, retain the reason in meta rather than waiting.
Fallback notes follow the same rule.

### Blocking limitation — revised (honest)

Owned independent descriptions are nonblocking; every write is
select-gated and chunk-limited with one per-frame deadline; an unread pipe
exercises the exhaustion/drop/disable path. Residual: on duplicated
blocking stderr, another writer can fill a pipe between select's writable
indication and our write — a <=PIPE_BUF write preserves pipe atomicity but
readiness and write are not atomic; one short write can block in that rare
race and a monotonic deadline cannot interrupt it. Regular-file
O_NONBLOCK cannot protect against pathological storage stalls. This is the
caller-selected compromise. Test and document it; normal tests establish
the bounded exhaustion/disable path, not absence of an adversarial race.

### Throttle and trailing delivery (Revision 3 correction 1)

- offer detaches lines, replaces the sole pending candidate, then flushes
  if due. First attempt immediate (`last_attempt=None`, not zero).
- Due time = last attempt completion + interval (rate-limits exhausted
  attempts too; no busy loop). Interval zero permits each new candidate.
- next_due returns None when no pending candidate or disabled; else the
  absolute monotonic due time. finish/drop clears pending.
- Integrate into `_readline` at its existing select site:

      timeout = min(wire_bound - now, 1.0, max(0.0, render_due - now))

  Omit the third term when next_due is None. After wakeup: preserve wire
  read/EOF handling; service a due display through a guarded flush whose
  frame deadline is CAPPED by the remaining wire bound (correction 1);
  re-evaluate the unchanged absolute wire deadline. A render-only wakeup
  must not become EOF, a protocol record, a content-deadline reset, or an
  extra _service_strategy call. The main-loop boundary also flushes
  (buffered-input starvation). Do not modify outbound _write_all.

### TTY painter transaction (Revision 3 correction 2)

FramePainter currently commits `self.height` while composing. Make
painter/transport a transaction:

1. Composition must not irreversibly commit height before the delivery
   outcome is known: the caller retains old/new height explicitly
   (compose with an explicit prior-height argument, or painter exposes
   compose/p commit split).
2. Zero bytes written: restore/retain the old displayed height.
3. Any bytes written but the frame incomplete: mark TTY position UNKNOWN;
   before the next ordinary frame, emit the documented absolute
   resynchronization sequence (full clear + home, no in-place
   continuation), or disable in-place redraw for the rest of the episode.
   `height = 0` alone is not sufficient.
4. Commit the new height only after complete delivery.

Non-TTY files/pipes have no cursor repair; partial frames are counted
dropped and never resumed.

## 6. Controller and CLI integration: exact anchors

`tools/agent/controller.py`:
1. Keyword-only Controller options `spectate: str = "none"`,
   `spectate_interval: float = 0.15`, validated separately via
   validate_spectate (NOT ProviderConfig/_safe_config/provider contexts).
2. Two defaulted EpisodeResult fields:
   `spectate_frames_rendered: int = 0`,
   `spectate_disabled_reason: Optional[str] = None`.
3. End of `_EpisodeRunner.__init__` (after rec_healthy and existing state):
   render-only state + guarded destination creation. Its OSError must not
   reach run_episode's spawn-failure handler.
4. End of successful `_on_obs` (after pending-key assignment, ~line 965):
   guarded capture + success marker. No chunks/raw records.
5. In run, immediately after `_service_strategy()` (~628) and before
   `if self.pending`: guarded `_spectate_boundary()` (compose/offer once
   for a new accepted marker, else flush only).
6. `_readline` select-site integration per §5 (capped frame deadline).
7. End of `_finish` (after outcome assignment, ~692): guarded
   `_spectate_finish()` (fresh composition, force).
8. run_episode finally: guarded idempotent spectator close + stats-to-
   result copy BEFORE `_finalize_recording`; original reap/cleanup
   ordering unchanged; no duplicate final output.
9. Exactly the two result keys added to `_finalize_recording` meta
   (~263-287). recording.py, schema, campaign rollup unchanged.

Guard pattern:

    if off_or_disabled:
        return
    try:
        capture_or_project_or_offer_or_flush_or_finish()
    except Exception:
        disable_once_with_fixed_category()
    finally:
        synchronize_render_only_result_fields()

Never call _emit, _event_sink, _note_recorder_health, _cancel_strategy, or
provider cancel from a spectator guard. Do not gate display on
rec_healthy. Do not move directive activation or deadline anchors.

`tools/agent/__main__.py`: auto-only `--spectate` nargs='?' const='stderr'
default='none' choices=('tty','stderr','none'); `--spectate-interval`
float 0.15; validate via validate_spectate before launch; pass Controller
keywords; preserve summaries/exit accounting/evaluator CLI.

`agent.sh`: passthrough works; update the stale scripted-only comment.
Do NOT wrap auto with test/agent/spectate.py.

`test/agent/spectate.py`: import/re-export moved formatters (repo-root
import setup per format_obs.py); leave proxy/replay/destination policy/
LiveRenderSession/options untouched. `format_obs.py`, `codec.py`,
`state.py`: no changes required. Proposed `test/agent/test_auto_spectate.py`
for focused tests. `Files`: render.py, spectating.py, test_auto_spectate.py.

## 7. Recordings, docs, migration, rollback

Meta fields on EVERY episode incl. none mode:
`spectate_frames_rendered` (int >= 0), `spectate_disabled_reason`
(str or null). None mode writes 0/null. Off-mode is an operational
rollback, NOT byte-identical metadata rollback — these two keys are the
sole permitted recording-artifact extension. Nothing else persists
(drops, interval, destination, diagnostics, frames). Old meta without the
keys stays consumable.

doc/agent-autoplay.md: defaults, coalescing, idle select wakeup, Snapshot
map authority, historical messages, activation timing, fresh final state,
two keys, per-episode disable/reset, partial-frame possibility, stall
caveat. Examples:

    ./agent.sh auto --episodes 3 --strategy off \
        --output-dir /tmp/auto --spectate tty

    ./agent.sh auto --output-dir /tmp/auto \
        --spectate --spectate-interval 0.25 2>/tmp/auto-frames.txt

fd 2 also carries ordinary diagnostics; neither file is an authoritative
recording. Add spectate to the existing DeepSeek example. Warn game text
may be private. `2>&1` aliasing rejected for frames including fallback.

## 8. Verification and mutation acceptance matrix

Implementer runs; none pre-run.

### Extraction and regressions
- spectate selftest; existing test_spectate render/proxy/replay/failure
  suite; byte-exact check; import from outside repo cwd; format_obs
  selftest; test_auto* discover; make -C test/agent check; evaluator ×2
  on fixtures byte-identical; provider/budget/deadline tests.

### Frame/source-of-truth goldens
- 21x80 ASCII, blank x=0, exact cursor overlay, no hero fallback, source
  objects unchanged; disappearing-cell golden (fails if renderer reads
  memory.grid); message-history semantics (new obs with no messages
  retains history); unknown status `?`; need kinds/titles; sanitized
  clipping; active/inactive directives; n/a vs classified-zero;
  unclassified excluded; strategy count exact; repeated render mutates
  nothing; malformed apply/need → no success marker, last accepted map.

### Directive predicate and timing
- peek/view equivalence (no set, valid, level mismatch, unknown level,
  exact TTL, expired TTL, each precondition, multiple/precedence);
  immutability (active set, activation fields, generation, events, sinks);
  activation timing (frame N excludes advice settled during service;
  frame N+1 includes if eligible; delayed frame N unchanged; final may
  show current).

### Throttle, deadline, transport
- Fake clock: immediate first; interval equality; zero interval; newest
  coalescing; single pending; final forced once; per-episode reset.
- Select integration: throttled trailing frame attempted by due time
  without new wire input; wire deadline unchanged; render wake is not
  EOF/record/reset; main-loop flush covers starvation.
- Unread/full pipe: attempts exhaust 0.25s, drops counted, disable on
  third, wire bytes preserved. Controlled clocks for exact deadlines;
  subprocess tests with generous tolerance + outer timeout.
- select before every write; chunks <= PIPE_BUF; EINTR/EAGAIN never extend
  the absolute deadline; partial failure not counted rendered; success
  resets stall streak; coalescing does not; no re-attempt of dropped.
- Independent tty/file fd O_NONBLOCK; dup'd stderr flags unchanged.
  Residual-race behavior documented, not hidden.
- Diagnostics: no extra blocking allowance; meta reason intact when no
  sink; painter redraw state after partial; recovery appends complete
  frame.

### FD lifecycle matrix (isolated subprocesses)
- Closed stdout: render fd relocated >=3; fd1 restored closed; no leak; no
  frame on fd1. Socket alias rejected. `2>&1` pipe/file aliases + tty
  fallback rejected before note and frames. sys.stderr reassigned → fd 2
  still used. none: no open/dup/probe; close never closes caller fd2.
  Fallback note ≤1 per campaign; per-episode counter/reason reset;
  idempotent close.

### Full isolation integration
Deterministic bidirectional fake runner, real OS pipes, attached render
file, fixed hello/full/chunked obs, pages, invalid retry, closed. Compare
none vs spectate at identical policy/strategy points:
- `.wire.jsonl` byte-for-byte AND captured outbound stdin bytes
  byte-for-byte.
- ALL actions, decisions, events records and meta: normalize ONLY approved
  timing fields (actions.t; decisions.t/latency; events.wall) and the two
  spectate keys. Preserve ordinals, offsets, keys, reasons, counts, usage,
  directives, writer statuses, outcome, completeness. Compare
  campaign.json too.
- Same episode_ok, exit status, provider calls, budget results.
- Inject ordinary exceptions at capture/projection and each offer, format,
  write, flush, finish, close hook, plus opening, next_due, clock, select,
  diagnostic. Assert _note_recorder_health/provider-cancel/event-sink call
  counts unchanged; genuine recorder-failure fixtures prove paid-work
  cancellation unchanged.
- Close fault appears in meta (cleanup stats precede finalize). Renderer
  faults never become spawn failures. Multi-episode: renderer restored per
  episode; none mode exactly 0/null keys.

Real auto smoke with responsive attached file: useful, not a byte-identity
test. No real DeepSeek calls required.

### Required mutation demonstrations
- Map from remembered grid → disappearing-cell golden fails.
- view instead of peek / duplicated TTL predicate → immutability fails.
- Display pending advice / recompose old candidates → activation and
  historical-need tests fail.
- Final only flushes old lines → settled final-state test fails.
- Remove render timeout from select → idle trailing-frame test fails.
- Remove write gating/chunk bound, reset deadline after progress, omit
  third-stall disable, count partial frames → transport tests fail.
- Mutate rec_healthy/cancel/event sink on render fault → counts + sidecar
  comparison fail.
- Render to child stdin/stdout, or miss socket/fallback alias checks →
  outbound identity and fd tests fail.
- Close/sync after meta finalization → close-fault metadata test fails.
- Broaden normalization to hide semantic differences → comparator rejects.

## 9. Ordered commits and acceptance gates

1. `Extract shared spectator rendering` — render.py, compat re-exports,
   Files. Spectator outputs/tests unchanged; no autoplay hooks.
2. `Add read-only autoplay frame views` — shared eligibility helper/peek,
   Snapshot-based auto_frame, goldens/immutability. No controller change.
3. `Bound autoplay render writes` — spectating.py, fd ownership/alias
   guards, select/chunks/deadline, drops/disable/throttle, diagnostics.
4. `Integrate live autoplay spectating` — CLI/controller hooks, select
   wakeup, fresh final composition, cleanup-before-meta, two keys; full
   isolation + mutation tests.
5. `Document live autoplay spectating` — docs, shell comments, manifest/
   width checks, optional real smoke.

Rollback: `--spectate none` operationally; code rollback needs no sidecar
migration (two optional meta keys only).

## 10. Reviewer focus (for the implementation review)

1. Every render hook and failure/diagnostic/cleanup path isolated —
   including pre-stream and post-finish failures.
2. Select deadline arithmetic, PIPE_BUF chunking, shared fd flags,
   residual-race honesty.
3. No new mutating calls (directives, events, recorder health, provider
   cancellation, policy deadlines); success marker follows full validation.
4. Frozen observation need vs current final need; postmortem counters;
   Snapshot map authority after malformed input.
5. Render-only state synced before meta finalize; two keys incl. none.
6. Isolation assertions cover every sidecar and hook.
7. Revision 3 corrections: render wake capped by the wire bound;
   transactional TTY painter (height commit on delivery, resync after
   partial).
