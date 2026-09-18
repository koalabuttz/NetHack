"""One bounded pipe-owning controller: scheduling, fallback, retry, lifecycle.

The controller owns exactly one game pipe per episode.  It never stalls the
wire:

  * page transfer keeps exactly **one** ``get_page`` in flight (the next page
    is requested only after its response has been consumed and validated), so
    a peer advertising 65,535 pages cannot make the outbound obligation grow
    without bound;
  * every outbound line is written with a select-based, monotonic-deadline
    write-all, so a full stdin pipe is bounded by the answer deadline instead
    of blocking a thread forever;
  * ``invalid`` is a retry state, not completion -- preserve the request id,
    repair once where applicable, then fall back per kind, then stop after a
    small total retry cap instead of spinning forever;
  * ``closed`` ends the episode; **EOF without closed is a transport
    failure**, never a fabricated terminal record.  Closure is best-effort
    (see ``sys/unix/agent_runner.c``), so a ``closed`` that arrives while a
    request is still unanswered is a *failure*, not a success;
  * timeout / forced kills / unanswered requests are reported separately from
    natural completions.

Session validation is bounded and per-episode: a wrong or missing ``hello``,
a duplicate ``hello``, an out-of-order or non-monotonic record, an oversized
physical line and an exhausted assembler budget each terminate *that episode*
promptly and the campaign continues.

The launcher child is spawned into its own process group with an allowlisted
environment (provider secrets are stripped), and teardown escalates
TERM -> KILL across the whole group while always reaping the direct child.
"""

import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

from . import protocol, recording, spectating
from .budget import BudgetLedger
from .codec import AssemblerLimit, ChunkError, IncrementalAssembler
from .directives import DirectiveBook, PreconditionState
from .events import (BoundaryQueue, EventLedger, directive_event,
                     hunger_index)
from .policy import INV_STALE_TICKS, ScriptedReflex
from .protocol import NeedKey, Request, Snapshot
from .providers import (NullStrategy, ProviderConfig, ReflexContext,
                        ReflexTimeout, ScriptedReflexProvider,
                        StrategyContext, StrategyConversation,
                        StrategyExchange, prepare_strategy_request,
                        strategy_provider, tariff_from_config)
from .render import auto_frame
from .state import EpisodeMemory, render_map

# How many detected boundary records the episode keeps as *boundary history*
# for the strategy prompt.  Bounded so a long episode cannot grow the window
# without limit; the current request's pending boundaries are rendered
# separately and always survive this truncation.
_BOUNDARY_HISTORY_MAX = 16

# Environment names the launcher is allowed to inherit.  Everything else --
# in particular DEEPSEEK_API_KEY and JEV_API_KEY -- is dropped by
# construction, not merely unset: this is an allowlist, so a new provider
# secret cannot leak just because someone forgot to add it to a deny list.
_CHILD_ENV_ALLOW = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
                    "TERM", "USER", "LOGNAME",
                    "NETHACK_AGENT_TEST_COPY_LIMIT")
_CHILD_ENV_DEFAULTS = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                       "LC_ALL": "C.UTF-8"}
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class _ProtocolFailure(Exception):
    """A bounded per-episode wire violation (the campaign continues)."""


class _TransportFailure(Exception):
    """An outbound write failed: terminal for this episode."""


class _DeadlineExceeded(Exception):
    """A per-need (content/transport) deadline expired."""


def child_env(source=None) -> dict:
    """The minimal, allowlisted environment for the launcher child.

    Only the names in :data:`_CHILD_ENV_ALLOW` survive, and anything whose
    name still looks like a credential is dropped as well.  The result is what
    is handed to :func:`subprocess.Popen` as ``env``.
    """
    source = os.environ if source is None else source
    env = {}
    for name in _CHILD_ENV_ALLOW:
        if name in source:
            env[name] = source[name]
    for name, value in _CHILD_ENV_DEFAULTS.items():
        env.setdefault(name, value)
    for name in list(env):
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            del env[name]
    return env


def ensure_private_dir(path: str) -> None:
    """Create *path* if needed and tighten it to owner-only.

    ``makedirs(mode=0700, exist_ok=True)`` does **not** tighten a directory
    that already exists with looser bits; this does, or raises if it cannot.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


@dataclass
class EpisodeResult(object):
    index: int
    spawn_ok: bool = False
    closed: bool = False
    eof: bool = False
    forced_kill: bool = False
    teardown_failure: bool = False
    unanswered: bool = False
    recorder_failed: bool = False
    protocol_failure: Optional[str] = None
    failure_reason: Optional[str] = None
    timed_out: bool = False
    stop_reason: str = "unknown"
    outcome: str = "unknown"
    ticks: int = 0
    needs: int = 0
    invalids: int = 0
    actions: int = 0
    reflex_timeouts: int = 0
    returncode: Optional[int] = None
    recording_complete: bool = False
    stderr_tail: str = ""
    budget: dict = field(default_factory=dict)
    boundaries: int = 0
    strategy_calls: int = 0
    directives_applied: int = 0
    # Render-only outcome of live spectating for this episode.  These are the
    # only two fields the presentation layer contributes to a result, and they
    # are written on every episode (0/null in none mode).
    spectate_frames_rendered: int = 0
    spectate_disabled_reason: Optional[str] = None


@dataclass
class ControllerPaths(object):
    worker: str
    runner: str
    data: str
    sysconf: Optional[str] = None


class Controller(object):
    def __init__(self, config: ProviderConfig, paths: ControllerPaths,
                 output_dir: str, episode_timeout: float = 300.0,
                 read_slack: float = 30.0, max_retries: int = 3,
                 reap_grace: float = 5.0, *,
                 spectate: str = "none",
                 spectate_interval: float = spectating.DEFAULT_INTERVAL):
        self.config = config
        # One validation authority: a programmatically built ProviderConfig
        # cannot bypass the checks the CLI enforces (the validate() docstring
        # documents this contract).
        err = config.validate(episode_timeout=episode_timeout)
        if err is not None:
            raise ValueError(err)
        # Live spectating is validated here too, through its own authority,
        # so a programmatic Controller cannot bypass the CLI's checks.  It is
        # deliberately *not* a ProviderConfig field: presentation is not
        # provider policy.
        spectate_err = spectating.validate_spectate(spectate,
                                                    spectate_interval)
        if spectate_err is not None:
            raise ValueError(spectate_err)
        self.spectate = spectate
        self.spectate_interval = float(spectate_interval)
        # The tty->fd2 fallback note is best-effort and emitted at most once
        # per campaign, so it lives on the campaign-scoped Controller.
        self._spectate_noted = False
        self.paths = paths
        self.output_dir = output_dir
        self.episode_timeout = episode_timeout
        self.read_slack = read_slack
        self.max_retries = max_retries
        self.reap_grace = reap_grace
        # The declared provider deadlines are the wire's deadlines too; they
        # are configurable (CLI -> ProviderConfig) and actually enforced here.
        self.answer_deadline = float(getattr(config, "answer_deadline", 1.0))
        self.content_deadline = float(
            getattr(config, "content_deadline", 5.0))
        # The reflex allowance is an absolute per-decision deadline enforced
        # at the provider boundary; 0 disables the bound.
        self.reflex_deadline = float(
            getattr(config, "reflex_deadline", 0.75) or 0.0)
        self.strategy_deadline = float(
            getattr(config, "strategy_deadline", 20.0) or 0.0)
        # The campaign rollup's outcome: set by run_campaign so the CLI can
        # report a summary-write failure instead of claiming a missing path.
        self.summary_path: Optional[str] = None
        self.summary_error: Optional[str] = None
        ensure_private_dir(output_dir)

    # -- provider construction (per episode) -----------------------------
    def _new_strategy_provider(self):
        return strategy_provider(self.config)

    def _new_reflex_provider(self, reflex):
        if self.config.reflex == "scripted":
            return ScriptedReflexProvider(reflex)
        from .providers import reflex_provider
        return reflex_provider(self.config)

    # -- campaign --------------------------------------------------------
    def run_campaign(self, episodes: int) -> List[EpisodeResult]:
        # Validate the campaign count through the same authority the rest of
        # the config uses, so a programmatic call cannot silently return an
        # empty result list for a zero/negative/non-integer count.
        err = self.config.validate(episodes=episodes,
                                   episode_timeout=self.episode_timeout)
        if err is not None:
            raise ValueError(err)
        results = []
        for i in range(1, episodes + 1):
            results.append(self.run_episode(i))
        # A compact, secret-free rollup of the campaign, written next to the
        # per-episode recordings.  Failure to write it must not lose the
        # episode results, so it is best-effort after the run -- but the
        # failure is recorded rather than swallowed, so the CLI can report it
        # and never claim a path that does not exist.
        self.summary_path = None
        self.summary_error = None
        try:
            self.summary_path = write_campaign_summary(
                self.output_dir, results, self.config, self.episode_timeout)
        except OSError as exc:
            self.summary_error = str(exc)
        return results

    def run_episode(self, index: int) -> EpisodeResult:
        result = EpisodeResult(index=index)
        priv = tempfile.mkdtemp(prefix="nh-auto-ep.")
        rec = None
        proc = None
        runner = None
        try:
            rec = recording.EpisodeRecorder(self.output_dir, index)
            proc = self._spawn(priv)
            result.spawn_ok = True
            runner = _EpisodeRunner(self, proc, rec, result)
            runner.run()
            result.recorder_failed = rec.failed
        except OSError as exc:
            if rec is None:
                # a recorder that could not open its files at 0600 is a
                # recording failure, not a spawn failure: fail closed without
                # aborting the campaign
                result.recorder_failed = True
                result.failure_reason = "recorder failed: %s" % exc
                result.stop_reason = "recorder-failure"
            else:
                result.failure_reason = "spawn failed: %s" % exc
                result.stop_reason = "spawn-failure"
        finally:
            self._reap(proc, result)
            shutil.rmtree(priv, ignore_errors=True)
            # Render-only cleanup and its stats-to-result copy run BEFORE the
            # recording is finalized: a close fault still lands in the meta,
            # and the two spectate keys are written on every episode.  The
            # close is guarded and idempotent, and never touches recording
            # health, paid work, events or the wire.
            if runner is not None:
                runner.spectate_close()
            if rec is not None:
                self._finalize_recording(rec, result)
        return result

    def _finalize_recording(self, rec, result: EpisodeResult) -> None:
        result.recorder_failed = result.recorder_failed or rec.failed
        meta = {
            "config": _safe_config(self.config),
            "episode_timeout": self.episode_timeout,
            "answer_deadline": self.answer_deadline,
            "content_deadline": self.content_deadline,
            "stop_reason": result.stop_reason,
            "game_outcome": result.outcome,
            "closed": result.closed,
            "eof": result.eof,
            "forced_kill": result.forced_kill,
            "teardown_failure": result.teardown_failure,
            "unanswered": result.unanswered,
            "recorder_failed": result.recorder_failed,
            "protocol_failure": result.protocol_failure,
            "failure_reason": result.failure_reason,
            "ticks": result.ticks,
            "needs": result.needs,
            "invalids": result.invalids,
            "reflex_timeouts": result.reflex_timeouts,
            "returncode": result.returncode,
            "budget": result.budget,
            "boundaries": result.boundaries,
            "strategy_calls": result.strategy_calls,
            "directives_applied": result.directives_applied,
            # The two permitted presentation keys, written on every episode
            # (0/null in none mode).  No drops, interval, destination or
            # diagnostics are persisted.
            "spectate_frames_rendered": result.spectate_frames_rendered,
            "spectate_disabled_reason": result.spectate_disabled_reason,
        }
        rec.finalize(meta)
        result.recording_complete = not rec.incomplete

    # -- process ---------------------------------------------------------
    def _child_env(self) -> dict:
        return child_env()

    def _spawn(self, priv):
        argv = [self.paths.runner, "--worker", self.paths.worker,
                "--private-root", priv, "--data", self.paths.data]
        if self.paths.sysconf:
            argv += ["--sysconf", self.paths.sysconf]
        deadline = int(self.episode_timeout + self.read_slack)
        argv += ["--deadline", str(deadline)]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0,
                                env=self._child_env(),
                                start_new_session=True)
        # Own the whole process group from the moment of spawn.  Resolving the
        # group at signal time is not enough: if the direct launcher exits on
        # TERM while a descendant ignores it, the leader PID is gone and the
        # group can no longer be found from it.
        proc._auto_pgid = _pgid_of(proc.pid)
        try:
            os.set_blocking(proc.stdin.fileno(), False)
        except (OSError, ValueError):
            pass
        proc._auto_stderr = _StderrDrain(proc.stderr)
        proc._auto_stderr.start()
        return proc

    def _reap(self, proc, result: EpisodeResult) -> None:
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        pgid = getattr(proc, "_auto_pgid", None)
        # Phase 1: a launcher that exits on its own keeps its real status.  A
        # TERM-ignoring descendant can survive it, so the owned GROUP is
        # assessed -- and killed -- independently of the direct child.
        if _wait_child(proc, self.reap_grace):
            if not _group_gone(pgid):
                result.forced_kill = True
                _signal_group(pgid, proc, signal.SIGTERM)
                if not _group_gone(pgid, 3.0):
                    _signal_group(pgid, proc, signal.SIGKILL)
                    if not _group_gone(pgid, 5.0):
                        # never silently continue: the group may still be
                        # alive
                        result.teardown_failure = True
        # Phase 2: the launcher itself is still running; escalate TERM ->
        # KILL across the whole group, then assess the group again even if the
        # leader has now exited, so a stubborn descendant is still killed.
        else:
            _signal_group(pgid, proc, signal.SIGTERM)
            if not _wait_child(proc, 3.0):
                result.forced_kill = True
                _signal_group(pgid, proc, signal.SIGKILL)
                if not _wait_child(proc, 5.0):
                    result.teardown_failure = True
            if not _group_gone(pgid):
                result.forced_kill = True
                _signal_group(pgid, proc, signal.SIGKILL)
                if not _group_gone(pgid, 5.0):
                    result.teardown_failure = True
        drain = getattr(proc, "_auto_stderr", None)
        if drain is not None:
            drain.join(timeout=2)
            result.stderr_tail = drain.text()[-1000:]
        result.returncode = proc.returncode


def _pgid_of(pid):
    """The owned process-group id for a spawned leader (or the pid)."""
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return pid


def _signal_group(pgid, proc, sig) -> None:
    """Signal the whole owned group; fall back to the direct child."""
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            # the entire group is already gone
            return
        except (PermissionError, OSError):
            pass
    send = getattr(proc, "kill" if sig == signal.SIGKILL else "terminate",
                   None)
    if send is not None:
        try:
            send()
        except (OSError, ProcessLookupError):
            pass


def _group_gone(pgid, timeout: float = 0.0) -> bool:
    """True once no process remains in the owned group *pgid*.

    Assesses the group itself, not the direct child: this is what catches a
    descendant that outlives a launcher which exited on TERM.
    """
    if pgid is None:
        return True
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _wait_child(proc, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


class _ReflexCall(object):
    """Run one provider decision on a bounded daemon thread.

    A cooperative in-process provider checks the absolute deadline it is
    handed (``ReflexContext.deadline``) and returns within it.  A provider
    that ignores the deadline -- or a truly blocking call such as a network
    round trip -- cannot be interrupted in-process, so this bounds only how
    long the *controller* waits: on timeout the thread is abandoned and the
    scripted fallback answers, which keeps the wire moving.  A real network
    provider must run in the Wave-2 killable worker process so the abandoned
    work is reaped too; the contract and plumbing for the deadline exist now.
    """

    def __init__(self, fn):
        self.fn = fn
        self.result = None
        self.error = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:     # noqa: BLE001 - re-raised by caller
            self.error = exc
        finally:
            self._done.set()

    def start(self):
        self._thread.start()

    @property
    def finished(self):
        """True once the bounded call has returned (result or error)."""
        return self._done.is_set()

    def wait(self, timeout):
        if timeout is None:
            return self._done.wait()
        return self._done.wait(max(0.0, timeout))


class _StderrDrain(object):
    """Drain the launcher's stderr on a daemon thread so it cannot block."""

    def __init__(self, fh, cap=256):
        import threading
        self.fh = fh
        self.cap = cap
        self.lines: List[str] = []
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def _run(self):
        try:
            for raw in iter(self.fh.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                self.lines.append(line)
                if len(self.lines) > self.cap:
                    del self.lines[:len(self.lines) - self.cap]
        except (OSError, ValueError):
            pass

    def join(self, timeout=2):
        self._t.join(timeout=timeout)

    def text(self):
        return "\n".join(self.lines)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _directive_dicts(dsets) -> list:
    """The complete validated directive set(s) as plain dicts.

    A strategy decision must record the whole set -- schema version, goals,
    target, risk, TTL, preconditions and explanation -- not just the goals,
    so an offline replay can round-trip it exactly.
    """
    out = []
    for d in dsets or []:
        to_dict = getattr(d, "to_dict", None)
        if to_dict is not None:
            out.append(to_dict())
        elif isinstance(d, dict):
            out.append(d)
    return out


def _crossed_dispatch_boundary(res) -> bool:
    """True when a strategy call actually reached the provider's wire.

    A provider reports this directly through ``StrategyResult.dispatched``; an
    in-process provider that leaves the flag unset is inferred from a result
    that carries usage or succeeded (it obviously ran).  A ``None`` result --
    an exception before any worker existed -- never crossed.
    """
    if res is None:
        return False
    if getattr(res, "dispatched", False):
        return True
    return bool(res.usage) or bool(res.ok)


def _quench_provider(provider) -> None:
    """Cancel and reap a strategy provider, never raising.

    The provider that ran the postmortem is its own instance; whether the call
    completed, timed out or raised, nothing it started may outlive the
    episode.  ``reap`` is optional (an in-process provider has no worker).
    """
    try:
        provider.cancel()
    except Exception:                        # noqa: BLE001 - teardown
        pass
    reap = getattr(provider, "reap", None)
    if reap is not None:
        try:
            reap()
        except Exception:                    # noqa: BLE001 - teardown
            pass


class _FrozenPresentation(object):
    """A detached copy of one accepted snapshot's presentation.

    ``Snapshot.apply`` is not atomic (it assigns pal/map before cursor and
    windows can fail), and the live ``Snapshot`` is mutated again by the very
    next observation.  A candidate therefore captures only the presentation
    authority -- the map cells and the cursor -- from the *successfully
    applied* snapshot, deep-copied, so a delayed frame renders the observation
    it describes rather than a later one.
    """

    __slots__ = ("map", "cur")

    def __init__(self, cells, cur):
        self.map = cells
        self.cur = cur


class _EpisodeRunner(object):
    def __init__(self, controller, proc, rec, result):
        self.c = controller
        self.proc = proc
        self.rec = rec
        self.result = result
        self.asm = IncrementalAssembler(
            max_retained_bytes=protocol.MAX_RETAINED_BYTES,
            max_chunks=protocol.MAX_CHUNKS, max_streams=protocol.MAX_STREAMS,
            max_line_bytes=protocol.MAX_PHYSICAL_LINE)
        self.snap = Snapshot()
        self.req = Request()
        self.mem = EpisodeMemory()
        self.reflex = ScriptedReflex(controller.config)
        self.reflex.max_ticks = controller.config.max_ticks
        self.reflex_provider = controller._new_reflex_provider(self.reflex)
        self.strategy_provider = controller._new_strategy_provider()
        self.strategy_enabled = self.strategy_provider.available(
            controller.config).enabled
        self.event_ledger = EventLedger(sink=self._event_sink)
        self.ledger = BudgetLedger(
            strategy_cap=controller.config.strategy_call_cap,
            postmortem_reserve=controller.config.postmortem_reserve,
            usd_cap=controller.config.usd_cap,
            tariff=tariff_from_config(controller.config),
            reflex_cap=controller.config.reflex_call_cap,
            token_cap=controller.config.token_cap)
        self.boundary_queue = BoundaryQueue(
            cooldown_ticks=controller.config.boundary_cooldown_ticks,
            cooldown_wall=controller.config.boundary_cooldown_wall,
            emergency_wall=controller.config.boundary_emergency_wall,
            ledger=self.ledger, event_ledger=self.event_ledger)
        self.book = DirectiveBook()
        self.detected_boundaries = []
        self.need_boundaries = []
        self.low_conf_streak = 0
        self._strategy_call = None
        self._strategy_pb = None
        self._strategy_level = None
        self._strategy_prepared = None
        # The conversation is episode-local and harness-owned: a provider or
        # worker respawn does not own or clear it, and it is never shared
        # across campaign episodes.  Boundary history is a bounded,
        # harness-owned deque updated at detection time, not a recorder
        # internal or an unbounded event log.
        self._conversation = StrategyConversation(
            identity=(controller.config.deepseek_model,
                      controller.config.deepseek_base_url),
            max_pairs=controller.config.deepseek_history_pairs)
        self._boundary_history = deque(maxlen=_BOUNDARY_HISTORY_MAX)
        self._pending_directives = None
        self._pending_directives_level = None
        self.paid_disabled = False
        self.tick = 0
        self.retries = 0
        self._needs = 0
        self._invalids: List[str] = []
        self.force_fallback = False
        self.pending = False
        self.pending_need = None
        self.pending_seq = None
        self.pending_key = None
        self.acked_chunks = set()
        self.buf = b""
        self.hello = None
        self.hello_seen = False
        self.last_seq = 0
        self.closed = False
        self.deadline = 0.0
        self.need_deadline = None
        self.action_ordinal = 0
        self.reflex_timeouts = 0
        self.rec_healthy = True
        # -- render-only state (never feeds policy, health or the wire) ----
        # The live RenderStream (None in none mode or after an open failure),
        # the newest accepted candidate evidence, the accepted-seq marker that
        # gates a single offer, and the last offered marker.
        self.spectate = None
        self._spectate_frozen = None      # _FrozenPresentation or None
        self._spectate_need = None        # frozen observation need
        self._spectate_windows = ()       # frozen need titles
        self._spectate_accepted = 0       # success marker: last applied seq
        self._spectate_offered = 0        # candidate already offered
        self._spectate_diag_ok = False
        self._open_spectate()

    def _open_spectate(self):
        """Guarded destination + stream creation (never a spawn failure).

        ``none`` opens nothing.  Any failure here disables spectating for the
        episode -- recorded as ``open-failed`` in the meta -- and must not
        propagate: it runs inside ``run_episode``'s OSError/except boundary
        that would otherwise misreport it as a spawn failure.
        """
        if self.c.spectate == "none":
            return
        try:
            self._spectate_diag_ok = True
            dest = spectating.open_destination(self.c.spectate)
            self.spectate = spectating.RenderStream(
                dest, self.c.spectate_interval,
                diagnostic=self._spectate_diagnostic)
            if dest.note is not None and not self.c._spectate_noted:
                self.c._spectate_noted = True
                self.spectate.offer([dest.note])
        except Exception:  # noqa: BLE001 - never a spawn failure
            self.spectate = None
            self._spectate_diag_ok = False
            self.result.spectate_disabled_reason = "open-failed"

    def _spectate_diagnostic(self, text):
        """Best-effort note on the side channel, no fresh blocking allowance.

        Never called for a write-deadline disable (that is exactly the case
        where granting another blocking write could stall); the reason stays
        in ``disabled_reason`` for the meta instead.
        """
        stream = self.spectate
        if not self._spectate_diag_ok or stream is None:
            return
        if stream.disabled_reason == "write-deadline":
            return
        try:
            stream.destination.write(
                (text + "\n").encode("utf-8", "replace"),
                deadline=time.monotonic() + spectating.FRAME_WRITE_TIMEOUT)
        except Exception:                    # noqa: BLE001 - best effort
            pass

    def _spectate_sync(self):
        """Copy the render stats into the result (cheap, idempotent)."""
        if self.spectate is not None:
            self.result.spectate_frames_rendered = (
                self.spectate.frames_rendered)
            self.result.spectate_disabled_reason = (
                self.spectate.disabled_reason)

    def _spectate_fail(self, category):
        """Disable rendering once, with a fixed category, never raising."""
        if self.spectate is not None:
            self.spectate.disable(category)

    def _spectate_capture(self, seq, need):
        """Freeze the presentation of one *successfully applied* snapshot.

        Called only after apply, memory observe, boundary detection, need
        validation and pending setup have all succeeded, so it can never
        capture the half-applied state ``Snapshot.apply`` leaves on failure.
        None mode copies nothing.
        """
        if self.spectate is None:
            return
        try:
            self._spectate_frozen = _FrozenPresentation(
                dict(self.snap.map), self.snap.cur)
            self._spectate_windows = tuple(
                dict(w) for w in self.snap.windows.values())
            self._spectate_need = dict(need) if need is not None else None
            self._spectate_accepted = seq
        except Exception:                    # noqa: BLE001 - render only
            self._spectate_fail("capture-error")
        finally:
            self._spectate_sync()

    def _spectate_compose(self, *, final, need):
        """Compose a frame from the frozen presentation and live state."""
        return auto_frame(
            self._spectate_frozen, self.mem,
            episode=self.result.index, seq=self._spectate_accepted,
            tick=self.tick, need=need, windows=self._spectate_windows,
            directives=self.book.peek_view(
                self.tick, self.mem.status.dlvl, self._precondition_state()),
            strategy_calls=self.ledger.strategy_dispatched,
            usage=self.ledger.as_dict()["usage"],
            final_reason=self.result.stop_reason if final else None,
            outcome=self.result.outcome if final else None)

    def _spectate_boundary(self):
        """Offer a new accepted candidate once, else flush a due frame.

        Runs right after ``_service_strategy`` at the top of each loop so a
        frame reflects advice settled there only from the *next* snapshot
        (never retroactively), and so buffered input that never re-enters the
        select loop still gets its due frame.
        """
        if self.spectate is None:
            return
        try:
            if self._spectate_frozen is not None \
                    and self._spectate_accepted != self._spectate_offered:
                lines = self._spectate_compose(final=False,
                                               need=self._spectate_need)
                self._spectate_offered = self._spectate_accepted
                self.spectate.offer(lines)
            else:
                self.spectate.flush()
        except Exception:                    # noqa: BLE001 - render only
            self._spectate_fail("compose-error")
        finally:
            self._spectate_sync()

    def _spectate_readline_flush(self, bound):
        """Service a due frame from the select loop, capped by the wire bound.

        The frame deadline is ``min(now + write_timeout, bound)`` (Revision 3
        correction 1): a render wake can never extend the wire's own timeout,
        and an already-exhausted wire bound simply drops the frame.
        """
        if self.spectate is None:
            return
        try:
            self.spectate.flush(deadline_cap=bound)
        except Exception:                    # noqa: BLE001 - render only
            self._spectate_fail("write-error")
        finally:
            self._spectate_sync()

    def _spectate_finish(self):
        """Force one freshly composed final frame after outcome resolution."""
        if self.spectate is None:
            return
        try:
            lines = None
            if self._spectate_frozen is not None:
                need = self.pending_need if self.pending else None
                lines = self._spectate_compose(final=True, need=need)
            self.spectate.finish(lines)
        except Exception:                    # noqa: BLE001 - render only
            self._spectate_fail("finish-error")
        finally:
            self._spectate_sync()

    def spectate_close(self):
        """Idempotent, guarded close; copies stats to the result first."""
        try:
            if self.spectate is not None:
                self.spectate.close()
        except Exception:                    # noqa: BLE001 - teardown
            self._spectate_fail("close-error")
        finally:
            self._spectate_sync()

    # -- top loop --------------------------------------------------------
    def run(self):
        self.deadline = time.monotonic() + self.c.episode_timeout
        try:
            while not self.closed:
                self._service_strategy()
                self._spectate_boundary()
                if self.pending:
                    need_dl = self._need_deadline()
                    if need_dl is not None and time.monotonic() >= need_dl:
                        raise _DeadlineExceeded(
                            "content deadline expired for need %r"
                            % (self.req.id,))
                    if self.req.pages_declared \
                            and not self.req.pages_complete():
                        if not self._request_page(need_dl):
                            break     # EOF
                        continue
                    if not self._answer_now(need_dl):
                        break
                    continue
                if not self._pump():
                    break
        except _DeadlineExceeded as exc:
            self.result.failure_reason = str(exc)
            self.result.stop_reason = "content-deadline"
        except _TransportFailure as exc:
            self.result.failure_reason = "stdin write failed: %s" % exc
            self.result.stop_reason = "transport-failure-write"
        except _ProtocolFailure as exc:
            self.result.protocol_failure = str(exc)
            self.result.stop_reason = "protocol-failure"
        except TimeoutError as exc:
            self.result.timed_out = True
            self.result.failure_reason = str(exc)
            self.result.stop_reason = "episode-timeout"
        self._cancel_strategy()
        self._maybe_postmortem()
        self._finish()

    def _finish(self):
        self.result.ticks = self.tick
        self.result.needs = self._needs
        self.result.invalids = len(self._invalids)
        self.result.actions = self.action_ordinal
        self.result.reflex_timeouts = self.reflex_timeouts
        self.result.closed = self.closed
        self.result.budget = self.ledger.as_dict()
        self.result.boundaries = self.ledger.boundaries_detected
        self.result.strategy_calls = self.ledger.strategy_dispatched
        self.result.directives_applied = self.ledger.boundaries_applied
        self._flush_events()
        # Closure is best-effort: `closed` while a request is still awaiting
        # pages or an action is an unanswered obligation, not a completion.
        if self.closed and self.pending and self.pending_need is not None:
            self.result.unanswered = True
            if self.result.failure_reason is None:
                self.result.failure_reason = (
                    "closed with an unanswered request (id %r)"
                    % (self.req.id,))
            self.result.stop_reason = "closed-unanswered"
        if self.result.stop_reason == "unknown":
            if self.closed:
                if self.reflex.quitting and self.reflex.quit_reason == \
                        "tick-cap":
                    self.result.stop_reason = "tick-cap-graceful-quit"
                else:
                    self.result.stop_reason = "closed"
            elif self.result.eof:
                self.result.stop_reason = "transport-failure-eof"
        self.result.outcome = recording.infer_outcome(self.mem.messages)
        # Final composition is the LAST thing _finish does: a fresh frame from
        # the settled counters, the current peeked directives and the CURRENT
        # outstanding need -- never a flush of an older candidate.
        self._spectate_finish()

    # -- outbound (one send-and-record path) -----------------------------
    def _event_sink(self, rec) -> None:
        """Incremental boundary-lifecycle persistence.

        The ledger hands over every record the moment it is finalised, so a
        long episode never accumulates an end-of-episode burst that could
        overflow the recording writer queue.
        """
        self.rec.record_event(rec)
        # A recorder that fails synchronously here must disable paid dispatch
        # at once: this sink runs inside the same loop iteration that may then
        # start a paid Jev decision, before any other health check runs.  The
        # note is reentrancy-safe -- it clears rec_healthy before it
        # suppresses, and a nested sink call returns immediately.
        self._note_recorder_health()

    def _flush_events(self):
        """Finalise any open lifecycle records and persist directive events.

        Boundary records are emitted incrementally through the sink as they
        are finalised; this end-of-episode pass finalises whatever is still
        open (never-queued boundaries such as the closed marker) and writes
        the directive lifecycle events.  Deterministic fields (tick, level,
        state) are separate from wall timing, so a replay comparison can drop
        timing exactly.
        """
        self.event_ledger.flush()
        for ev in self.book.events:
            self.rec.record_event(directive_event(ev))
        self._note_recorder_health()

    def _emit(self, kind, obj, need_key=None, write_deadline=None):
        """Write one outbound line and record it; return its ordinal.

        Every outbound line -- act, get_page, ack_chunk -- goes through here,
        so the actions sidecar is a faithful, ordered record of the wire.  A
        failed write is a terminal transport failure and is recorded with its
        status before propagating.
        """
        payload = (json.dumps(obj) + "\n").encode("utf-8")
        offset = self.rec.wire_bytes
        ordinal = self.action_ordinal + 1
        try:
            self._write_all(payload, write_deadline)
        except _TransportFailure:
            self.action_ordinal = ordinal
            self.rec.record_action(ordinal, offset, need_key, kind, obj,
                                   "write-failed")
            self._note_recorder_health()
            raise
        self.action_ordinal = ordinal
        self.rec.record_action(ordinal, offset, need_key, kind, obj, "sent")
        self._note_recorder_health()
        return ordinal

    def _write_all(self, data: bytes, deadline) -> None:
        stream = self.proc.stdin
        fileno = getattr(stream, "fileno", None)
        if fileno is None:
            # a test double without a real descriptor: a single atomic write
            try:
                stream.write(data)
                flush = getattr(stream, "flush", None)
                if flush is not None:
                    flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise _TransportFailure(str(exc))
            return
        fd = stream.fileno()
        if deadline is None:
            deadline = time.monotonic() + self.c.answer_deadline
        view = memoryview(data)
        while len(view):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportFailure("write deadline exceeded")
            try:
                _, w, _ = select.select([], [fd], [], min(remaining, 0.25))
            except (OSError, ValueError) as exc:
                raise _TransportFailure("select on stdin failed: %s" % exc)
            if not w:
                continue
            try:
                n = os.write(fd, view)
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise _TransportFailure(str(exc))
            if n <= 0:
                raise _TransportFailure("stdin accepted no data")
            view = view[n:]

    def _note_recorder_health(self) -> None:
        if not self.rec_healthy:
            return
        if self.rec.failed:
            # A recorder failure can no longer be trusted as lossless, so the
            # Wave-1 hook applies the graceful-stop policy here: paid dispatch
            # is disabled for the rest of the episode (scripted play
            # continues), and any pending strategy work is dropped.
            self.rec_healthy = False
            self.paid_disabled = True
            self.strategy_provider.cancel()
            self.boundary_queue.suppress("recorder-failure")

    # -- pipe ------------------------------------------------------------
    def _need_deadline(self):
        if not self.pending or self.pending_need is None:
            return None
        return self.need_deadline

    def _readline(self, deadline=None):
        if deadline is None:
            deadline = self.deadline
        bound = min(deadline, self.deadline)
        while b"\n" not in self.buf:
            remaining = bound - time.monotonic()
            if remaining <= 0:
                if self.deadline <= time.monotonic():
                    raise TimeoutError("episode deadline reached")
                raise _DeadlineExceeded("content deadline reached")
            if len(self.buf) > protocol.MAX_PHYSICAL_LINE:
                raise _ProtocolFailure(
                    "unterminated line exceeds %d bytes"
                    % protocol.MAX_PHYSICAL_LINE)
            # Cap the select wakeup by a due frame (Revision 3 correction 1),
            # so a throttled trailing frame is attempted on its own deadline
            # without a new wire record.  The wire bounds `remaining` itself
            # are never extended.
            wait = min(remaining, 1.0)
            due = self.spectate.next_due() if self.spectate is not None \
                else None
            if due is not None:
                wait = min(wait, max(0.0, due - time.monotonic()))
            r, _, _ = select.select([self.proc.stdout], [], [], wait)
            # Service a due display through a guarded flush capped by the
            # remaining wire bound; this touches only render state and never
            # resets the wire deadline, fabricates a record or becomes EOF.
            if self.spectate is not None:
                self._spectate_readline_flush(bound)
            if not r:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                if self.buf:
                    line, self.buf = self.buf, b""
                    return line
                return None
            self.buf += chunk
        idx = self.buf.index(b"\n")
        if idx > protocol.MAX_PHYSICAL_LINE:
            raise _ProtocolFailure("physical line exceeds %d bytes"
                                   % protocol.MAX_PHYSICAL_LINE)
        line, self.buf = self.buf[:idx], self.buf[idx + 1:]
        return line

    def _pump(self, deadline=None) -> bool:
        """Read and handle one physical line.  Returns False on EOF."""
        line = self._readline(deadline)
        if line is None:
            self.result.eof = True
            return False
        self.rec.record_wire(line)
        self._handle_line(line)
        self._note_recorder_health()
        return not self.closed

    def _handle_line(self, line: bytes):
        if len(line) > protocol.MAX_PHYSICAL_LINE:
            raise _ProtocolFailure("physical line exceeds %d bytes"
                                   % protocol.MAX_PHYSICAL_LINE)
        try:
            logical = self.asm.feed(line)
        except AssemblerLimit as exc:
            raise _ProtocolFailure("assembler retention limit: %s" % exc)
        except ChunkError as exc:
            raise _ProtocolFailure("chunk stream: %s" % exc)
        except (ValueError, KeyError, TypeError) as exc:
            raise _ProtocolFailure("malformed record: %s" % exc)
        for rec in logical:
            self._handle_record(rec)
        self._ack_chunk(line)

    def _handle_record(self, rec):
        if not isinstance(rec, dict):
            raise _ProtocolFailure("record is not an object")
        t = rec.get("type")
        # A validated hello must precede *every* non-hello record -- closed
        # included: a closed-only stream (or a page/invalid before the
        # handshake) is a protocol failure, not a completed episode.
        if t != "hello" and not self.hello_seen:
            raise _ProtocolFailure("%r record before hello" % (t,))
        if t == "hello":
            self._on_hello(rec)
        elif t == "obs":
            self._on_obs(rec)
        elif t == "page":
            self._on_page(rec)
        elif t == "invalid":
            self._on_invalid(rec)
        elif t == "closed":
            self._on_closed(rec)
        else:
            raise _ProtocolFailure("unknown record type %r" % (t,))

    def _ack_chunk(self, line: bytes):
        # chunk acknowledgement is cumulative and contiguous; ack each newly
        # seen (rid, i) once, in arrival order
        if b'"chunk"' not in line:
            return
        try:
            raw = json.loads(line)
        except ValueError:
            return
        if not isinstance(raw, dict) or raw.get("type") != "chunk":
            return
        key = (raw.get("rid"), raw.get("i"))
        if key in self.acked_chunks:
            return
        self.acked_chunks.add(key)
        self._emit("ack_chunk", protocol.make_ack_chunk(raw.get("rid"),
                                                        raw.get("i")))

    # -- records ---------------------------------------------------------
    def _on_hello(self, rec):
        if self.hello_seen:
            raise _ProtocolFailure("duplicate hello")
        reason = protocol.validate_hello(rec)
        if reason:
            raise _ProtocolFailure("incompatible hello: %s" % reason)
        self.hello_seen = True
        self.hello = rec

    def _on_obs(self, rec):
        seq = rec.get("seq")
        if not _is_int(seq):
            raise _ProtocolFailure("obs without an integer seq")
        if seq <= self.last_seq:
            raise _ProtocolFailure("non-monotonic seq %r after %r"
                                   % (seq, self.last_seq))
        self.last_seq = seq
        # A malformed full snapshot must end this episode, not the whole
        # campaign: expected decode/shape faults (a bad palette entry, map
        # triple, window, cursor or message) are converted here, at the
        # episode boundary, into a per-episode protocol failure.  Only
        # ProtocolError and the shape errors of well-typed-but-broken data
        # are caught -- never control-flow or system exceptions.
        try:
            self.snap.apply(rec)
            self.mem.observe(self.snap)
        except protocol.ProtocolError as exc:
            raise _ProtocolFailure("invalid snapshot: %s" % exc)
        except (IndexError, KeyError, TypeError, ValueError,
                AttributeError) as exc:
            raise _ProtocolFailure("malformed snapshot: %s" % exc)
        # Boundary detection runs once per applied snapshot, on public state
        # only.  An id is emitted once, so re-presenting the same snapshot
        # (or replaying history) yields no new events; simultaneous reasons
        # coalesce into the single pending strategy request.
        self._detect_boundaries()
        # Validate the *complete* need shape before any of it is stored on the
        # outstanding request: a malformed need must fail this episode here,
        # not raise later from pages_complete/next_page_request, which run
        # outside run()'s per-episode failure boundary.
        need = rec.get("need")
        if need is not None:
            reason = protocol.validate_need(need)
            if reason:
                raise _ProtocolFailure("malformed need: %s" % reason)
        self.req.begin(need, seq)
        self.pending = need is not None
        self.pending_need = need
        self.pending_seq = seq
        # The aggregate content deadline is anchored to the moment the need
        # first appeared.  An `invalid` retry must NOT restart it: every
        # retry consumes the same budget, so a peer that drip-feeds
        # rejections cannot buy a fresh content_deadline per retry.
        self.need_deadline = time.monotonic() + self.c.content_deadline
        self.force_fallback = False
        self.retries = 0
        if need is not None:
            self._needs += 1
            self.pending_key = NeedKey(self.result.index, seq, need.get("id"))
        else:
            self.pending_key = None
        # Render-only: capture the accepted presentation LAST, only after
        # apply, memory, boundaries, need validation and pending setup have
        # all succeeded.  A malformed snapshot or need raises above and never
        # reaches here, so the previous accepted candidate is retained and no
        # success marker is advanced.
        self._spectate_capture(seq, need)

    def _on_page(self, rec):
        if not self.pending or self.req.need is None:
            raise _ProtocolFailure("page delivered with no outstanding need")
        if rec.get("content") != self.req.content:
            raise _ProtocolFailure("page for unexpected content %r"
                                   % (rec.get("content"),))
        outstanding = self.req.in_flight
        if outstanding is None:
            raise _ProtocolFailure(
                "page delivered with no outstanding request")
        idx = rec.get("page")
        if idx != outstanding:
            raise _ProtocolFailure(
                "page %r is not the outstanding page %r"
                % (idx, outstanding))
        total = rec.get("pages")
        if total != self.req.pages_declared:
            raise _ProtocolFailure(
                "page declares %r pages but the need declares %r"
                % (total, self.req.pages_declared))
        self.req.note_page(rec)

    def _on_invalid(self, rec):
        code = rec.get("code")
        if code not in protocol.INVALID_CODES:
            raise _ProtocolFailure("unknown invalid code %r" % (code,))
        # `invalid` refers to the request the engine still holds outstanding;
        # that is true whether or not we have already sent an answer for it.
        if self.req.need is None or self.pending_need is None:
            raise _ProtocolFailure("invalid with no outstanding request")
        self._invalids.append(code)
        self.retries += 1
        self.ledger.reflex_invalid += 1
        # record the rejected attempt: a decision with no selected action,
        # carrying the rejection reason
        self.rec.record_decision(
            proposal=None, selected=None, provider="controller",
            reason="invalid:%s (attempt %d)" % (code, self.retries))
        if self.retries > self.c.max_retries:
            raise _ProtocolFailure(
                "request id %r rejected %d times (last code %r)"
                % (self.req.id, self.retries, code))
        if code == "incomplete":
            # pages were not all delivered: drop the request bookkeeping so
            # the page obligation is re-issued
            self.req.reset_delivery()
        else:
            self.force_fallback = True
        # the engine left the SAME request outstanding: re-arm it, but keep
        # the ORIGINAL need deadline -- the retry shares the first budget
        self.pending = True

    def _on_closed(self, rec):
        self.closed = True
        self.reflex_provider.on_closed()
        # one closed/postmortem boundary: detected once, never re-emitted
        self.need_boundaries = self.mem.boundary.check(self.mem.status,
                                                       closed=True)
        self.detected_boundaries = self.need_boundaries
        self.ledger.note_boundary("detected", len(self.need_boundaries))
        self._note_detected(self.need_boundaries, self.mem.status.dlvl)
        if self._pending_directives is not None:
            # an episode that ends before the next command boundary never
            # applies the pending set
            self.boundary_queue.finish(False, "closed-before-command")
            self._pending_directives = None

    # -- boundary detection ----------------------------------------------
    def _detect_boundaries(self):
        """Fold one applied snapshot into the boundary machinery."""
        st = self.mem.status
        detected = self.mem.boundary.check(
            st, classes=self.mem.visible_classes(),
            messages=self.mem.recent_messages(10),
            inventory_sig=self.mem.inventory_signature(),
            failed_food=self.mem.failed_food_count(),
            low_conf_streak=self.low_conf_streak,
            low_conf_threshold=self.c.config.low_confidence_needs)
        self.detected_boundaries = detected
        self.need_boundaries = detected
        self._note_detected(detected, st.dlvl)
        if detected:
            self.ledger.note_boundary("detected", len(detected))
            if self._strategy_live():
                self.boundary_queue.submit(detected, self.tick, st.dlvl)
        # The queue only ever receives this round's set, so a boundary left
        # unqueued here can never be queued later: finalise it now through the
        # incremental sink (terminal=None -- detected-only, no invented
        # terminal state) instead of holding it in EventLedger._open until the
        # end-of-episode flush.  This is the shipped default (strategy="off")
        # path, where nothing is ever queued.  Records that *were* queued are
        # untouched: they finalise on dispatch/settlement as before.
        self.event_ledger.flush_unqueued()

    def _note_detected(self, detected, level):
        """Record every detected boundary in the persisted lifecycle ledger.

        Done here -- not in the queue -- so a boundary that is detected while
        the strategy tier is off is still represented as *detected* (it is
        simply never queued).  The same hook appends a stable
        eid/reason/tick/level record to the bounded boundary history the
        strategy prompt renders, so the window is harness-owned rather than a
        recorder internal or an unbounded event log.
        """
        for b in detected:
            self.event_ledger.detect(b, self.tick, level or "")
            self._boundary_history.append(
                {"eid": b.eid, "reason": b.reason, "tick": self.tick,
                 "level": level or ""})

    def _strategy_live(self):
        """Paid strategy dispatch is allowed for this episode."""
        return self.strategy_enabled and not self.paid_disabled \
            and not isinstance(self.strategy_provider, NullStrategy)

    # -- strategy scheduling (never in the action path) -------------------
    def _service_strategy(self):
        """Advance the one bounded strategy call.  Never blocks the wire."""
        if not self._strategy_live():
            return
        if self._strategy_call is not None:
            if self._strategy_call.finished:
                self._finalize_strategy()
            return
        now = time.monotonic()
        pending = self.boundary_queue.ready(self.tick, now)
        if pending is None:
            return
        self._dispatch_strategy(pending, now)

    def _dispatch_strategy(self, pending, now):
        # Prepare first: the pure helper selects the retained history, renders
        # the new tail once and freezes the full payload, and the bound is
        # computed from *that* frozen request.  Reservation happens before any
        # process is spawned: a call that times out with no usage is still
        # billed, and the conservative bound (the complete request plus the
        # configured maximum output) must fit inside what is left of every
        # cap.  A request that does not fit is refused here, before a worker
        # exists, and neither mutates the conversation nor consumes a call.
        ctx = self._build_strategy_context(pending)
        prepared = prepare_strategy_request(self.c.config, ctx,
                                            self._conversation)
        ctx.prepared_request = prepared
        if not prepared.fits:
            self._strategy_prepared = None
            self.boundary_queue.suppress("strategy-context-too-large")
            return
        if not self.ledger.reserve_strategy(
                prompt_tokens=prepared.prompt_bound,
                completion_tokens=prepared.completion_bound):
            self._strategy_prepared = None
            self.boundary_queue.suppress("strategy-cap")
            return
        self._strategy_prepared = prepared
        self._strategy_pb = \
            self.boundary_queue.mark_dispatched(self.tick, now)
        # The level the advice was *produced* for, not the level at arrival:
        # advice for another level is stale and is discarded on activation.
        self._strategy_level = self.mem.status.dlvl
        deadline = now + self.c.strategy_deadline
        provider = self.strategy_provider
        self._strategy_call = _ReflexCall(
            lambda: provider.deliberate(ctx, deadline))
        self._strategy_call.start()

    def _finalize_strategy(self):
        call = self._strategy_call
        self._strategy_call = None
        res = None
        if call is not None and call.error is None:
            res = call.result
        self._settle_strategy(res, cancelled=False)

    def _settle_strategy(self, res, cancelled):
        """Exactly-once settlement of the one started strategy operation.

        Guarded by the in-flight boundary set: whichever path runs first
        consumes the reservation, terminates (or holds) the set and records
        the decision; a later call is a no-op.

        The reservation is *committed* whenever the call may have reached the
        wire -- a real usage report, a directive answer, a timeout or an HTTP
        error, or an ambiguous ``None`` result after thread start -- so a
        genuinely lost call keeps its conservative exposure.  A result that
        proves a *known local refusal* (no key, cooldown, spawn failure, or an
        oversize payload) never crossed the dispatch boundary and is
        *released*, matching the postmortem's treatment rather than booking
        phantom exposure.  ``res is None`` is never treated as proof of no
        dispatch.

        History is committed *transactionally*: the retained slice the frozen
        request carried plus the newly completed pair, and only when the
        result is ok, carries validated directives, and was not cancelled.  A
        failed call leaves the previous committed history intact.
        """
        pending = self._strategy_pb
        if pending is None:
            return False
        self._strategy_pb = None
        prepared = self._strategy_prepared
        self._strategy_prepared = None
        usage = res.usage if res is not None else None
        if res is None or _crossed_dispatch_boundary(res):
            self.ledger.commit_strategy(usage)
        else:
            self.ledger.release_strategy()
        if res is not None and res.ok and res.directives and not cancelled:
            self._pending_directives = res.directives[0]
            self._pending_directives_level = self._strategy_level
            self._commit_history(prepared, res)
        else:
            # a failed, discarded or cancelled call still terminates its set
            finish_reason = ("strategy-cancelled" if cancelled
                             else "strategy-failed")
            self.boundary_queue.finish(False, finish_reason)
        reason = res.reason if res is not None else "no result"
        provider = res.provider if res is not None else "strategy"
        prefix = "strategy (cancelled)" if cancelled else "strategy"
        self.rec.record_decision(
            proposal=None, selected=None, provider="strategy",
            reason="%s %s: %s" % (prefix, provider, reason),
            boundaries=list(pending.eids),
            usage=usage or {},
            directives=_directive_dicts(res.directives if res is not None
                                        else []))
        self._note_recorder_health()
        return True

    @staticmethod
    def _assistant_text(res) -> str:
        """The assistant text to commit for a validated response.

        The verbatim validated ``choices[0].message.content`` is preferred --
        canonicalizing it with ``to_dict()`` would change the generated
        prefix.  A dict-shaped or injected response without verbatim text
        falls back to a stable serialization of the validated set: still
        semantically correct, but not generated-prefix faithful.
        """
        verbatim = getattr(res, "assistant_content", "")
        if verbatim:
            return verbatim
        first = res.directives[0]
        to_dict = getattr(first, "to_dict", None)
        if to_dict is not None:
            return json.dumps(to_dict(), sort_keys=True)
        if isinstance(first, dict):
            return json.dumps(first, sort_keys=True)
        return ""

    def _commit_history(self, prepared, res):
        """Install the retained slice plus the new pair, capped to K."""
        if prepared is None:
            return
        self._conversation.install(
            prepared.retained,
            StrategyExchange(user=prepared.user_text,
                             assistant=self._assistant_text(res)))

    def _activate_pending_directives(self, need):
        """Activate a returned directive set at the next command boundary."""
        if self._pending_directives is None:
            return
        if need.get("kind") not in ("command", "key", "direction"):
            return
        dset = self._pending_directives
        self._pending_directives = None
        level = self.mem.status.dlvl
        dispatched_level = self._pending_directives_level
        if dispatched_level is not None and level is not None \
                and level != dispatched_level:
            self.boundary_queue.finish(False, "stale-level")
            return
        self.book.activate(dset, self.tick, level)
        self.boundary_queue.finish(True)

    def _remaining_budget(self):
        spendable = self.ledger.strategy_cap - self.ledger.postmortem_reserve
        spent = self.ledger.strategy_dispatched \
            + self.ledger.strategy_reserved
        return max(0, spendable - spent)

    def _build_strategy_context(self, pending):
        st = self.mem.status
        bits = []
        if st.hp is not None and st.hp_max:
            bits.append("HP %d/%d" % (st.hp, st.hp_max))
        if st.hunger:
            bits.append("Hunger %s" % st.hunger)
        if st.dlvl:
            bits.append("Dlvl %s" % st.dlvl)
        if st.level is not None:
            bits.append("XL %d" % st.level)
        inventory = [(r.get("text") or "") for r in self.mem.inventory.rows]
        # The applicable directive set follows the DirectiveBook's own
        # applicability rules (level/TTL/preconditions), not merely the last
        # response received; an advisory that is no longer applicable is not
        # reported as active.
        view = self.book.view(self.tick, st.dlvl, self._precondition_state())
        return StrategyContext(
            episode=self.result.index, tick=self.tick,
            summary={"hp": st.hp, "hp_max": st.hp_max, "dlvl": st.dlvl},
            boundaries=list(pending.eids) if pending is not None else [],
            map_text=render_map(self.mem),
            status_text=", ".join(bits),
            recent_messages=self.mem.recent_messages(6),
            inventory=inventory, goals=[],
            history=list(self._boundary_history),
            remaining_budget=self._remaining_budget(), level=st.dlvl,
            role=self.c.config.role,
            directives=[view.dset] if view.active else [])

    def _precondition_state(self):
        st = self.mem.status
        frac = None
        if st.hp is not None and st.hp_max:
            frac = st.hp / float(st.hp_max)
        fresh = self.mem.inventory.seen_tick is not None and \
            (self.tick - self.mem.inventory.seen_tick) <= INV_STALE_TICKS
        return PreconditionState(
            hero_known=self.mem.hero is not None, hp_known=frac is not None,
            hp_frac=frac, hungry=hunger_index(st.hunger) >= 0,
            inventory_fresh=fresh)

    def _note_low_conf(self, low: bool):
        """Track sustained low confidence from the FINAL selection outcome.

        Only the outcome that actually answered the need feeds the streak:
        a timeout, an unavailable or abstaining paid tier, a forced fallback,
        a missing result and a proposal that failed local validation all
        count.  An ordinary scripted decision never does -- the scripted
        score is a documented heuristic uncertainty measure, not a calibrated
        probability, so it is not compared to ``--confidence-threshold``.  A
        *paid* answer below the threshold counts on its own.
        """
        if not low:
            self.low_conf_streak = 0
            return
        self.low_conf_streak += 1
        self.ledger.reflex_low_confidence += 1

    def _closed_cleanly(self):
        """A validated ``closed`` transition with no outstanding request.

        Only a clean closure earns a postmortem: an EOF/protocol failure has
        no closure to reflect on, and a ``closed`` that arrived while a
        request was still unanswered is reported as a failure, not a
        completion, so it is not eligible either.
        """
        if not self.closed:
            return False
        if self.pending and self.pending_need is not None:
            return False
        return True

    def _maybe_postmortem(self):
        """One bounded, optional strategy call after a *clean* `closed`.

        Enabled only when a postmortem slot is actually reserved
        (``--postmortem-reserve > 0``); otherwise it is skipped and the whole
        cap is available during play.  It also requires a clean closure, a
        healthy recording (via :meth:`_strategy_live`) and enough remaining
        budget to cover the call's conservative bound.

        The postmortem runs through a **fresh provider lifecycle**.  The
        episode's gameplay provider has already been cancelled at this point
        (``_cancel_strategy``), and that cancellation is sticky, so reusing it
        would refuse the spawn and the reserved call would never reach the
        provider.  A new provider instance -- with its own bounded worker and
        deadline -- is constructed here instead, and the reservation is
        settled as *dispatched* only once work actually crossed the dispatch
        boundary (see :meth:`_settle_postmortem`).
        """
        if self.ledger.postmortem_reserve <= 0:
            return
        if not self._closed_cleanly():
            return
        if not self._strategy_live():
            return
        ctx = self._build_strategy_context(None)
        ctx.postmortem = True
        ctx.summary = self._postmortem_summary()
        ctx.boundaries = [b.eid for b in self.need_boundaries]
        # The postmortem is a *fresh*, empty conversation: it never carries
        # gameplay history, and it is never committed back to the gameplay
        # conversation.
        ctx.directives = []
        ctx.history = []
        prepared = prepare_strategy_request(self.c.config, ctx, retained=[])
        ctx.prepared_request = prepared
        if not prepared.fits:
            return
        if not self.ledger.reserve_strategy(
                postmortem=True, prompt_tokens=prepared.prompt_bound,
                completion_tokens=prepared.completion_bound):
            return
        provider = self._new_postmortem_provider()
        deadline = time.monotonic() + self.c.strategy_deadline
        # Once ``deliberate`` has been *invoked* the reserved call may have
        # reached the wire, so an exception or a missing result is *ambiguous*
        # -- exactly like the live/replay strategy paths -- and must keep its
        # conservative exposure rather than have it erased.  ``invoked`` is
        # set immediately before the call so a raise from inside the provider
        # is still recorded as "the call was made".
        invoked = False
        try:
            invoked = True
            res = provider.deliberate(ctx, deadline)
        except Exception:                    # noqa: BLE001 - bounded policy
            res = None
        finally:
            # bound and reap the postmortem's own worker so nothing it started
            # survives the episode, even if the call raised mid-flight
            _quench_provider(provider)
        self._settle_postmortem(res, ctx.boundaries, invoked=invoked)

    def _postmortem_summary(self):
        """An allowlisted, deterministic episode summary for the postmortem.

        Only finalised public state: outcome/stop reason, final tick, visible
        level and HP, action/invalid/boundary counts, the strategy dispatch
        count and the currently applicable advice.  No wall duration, no
        secrets, no raw transcript and no recorder metadata that has not yet
        been finalised.
        """
        st = self.mem.status
        r = self.result
        view = self.book.view(self.tick, st.dlvl, self._precondition_state())
        return {
            "outcome": r.outcome,
            "stop_reason": r.stop_reason,
            "ticks": self.tick,
            "level": st.dlvl or "",
            "hp": st.hp,
            "hp_max": st.hp_max,
            "actions": self.action_ordinal,
            "invalids": len(self._invalids),
            "boundaries": self.ledger.boundaries_detected,
            "strategy_calls": self.ledger.strategy_dispatched,
            "advice": list(view.dset.goals) if view.active else [],
        }

    def _new_postmortem_provider(self):
        """A fresh strategy provider owning the postmortem's own lifecycle.

        A new instance carries no cancellation left over from the episode's
        gameplay provider, so the reserved postmortem can still reach the
        provider.  The factory is :meth:`Controller._new_strategy_provider`,
        the same hook the per-episode provider is built from, so an injected
        test provider keeps working.
        """
        return self.c._new_strategy_provider()

    def _settle_postmortem(self, res, boundaries, invoked: bool = True):
        """Book the postmortem once it may have reached the wire.

        The reservation is consumed exactly once.  Once ``deliberate`` has
        been *invoked* the call may have crossed the dispatch boundary, so an
        exception or an absent result (``res is None``) is treated as
        *ambiguous* -- exactly like the live/replay strategy paths -- and the
        reserved conservative bound is committed as unknown exposure; a
        genuinely lost paid call must never have its exposure erased.

        Only a *structured* result that affirmatively represents a known local
        refusal -- ``dispatched`` False with neither success nor usage
        evidence (a cooldown, a missing key, a sticky cancellation, a spawn
        failure or an oversize payload, all of which the provider reports as a
        ``StrategyResult``) -- is released without booking anything, so the
        ledger never reports a phantom dispatch or a phantom billing exposure.
        """
        usage = res.usage if res is not None else None
        ambiguous = invoked and res is None
        if ambiguous or _crossed_dispatch_boundary(res):
            self.ledger.commit_strategy(usage, postmortem=True)
        else:
            self.ledger.release_strategy()
        self.rec.record_decision(
            proposal=None, selected=None, provider="strategy",
            reason="postmortem: %s" % (res.reason if res is not None
                                       else "no result"),
            boundaries=boundaries,
            usage=usage or {},
            latency=res.latency if res is not None else 0.0,
            directives=_directive_dicts(res.directives if res is not None
                                        else []))

    def _cancel_strategy(self):
        """Stop the strategy tier and settle everything it started.

        The in-flight call is committed exactly once (as *dispatched* with
        unknown usage when it never returned), the boundary set is
        terminated, and any accepted-but-unactivated directive set is
        expired -- so every dispatched EID ends in exactly one terminal
        state.
        """
        try:
            self.strategy_provider.cancel()
        except Exception:                    # noqa: BLE001 - teardown
            pass
        call = self._strategy_call
        self._strategy_call = None
        if call is not None:
            call.wait(1.0)
        res = None
        if call is not None and call.error is None:
            res = call.result
        self._settle_strategy(res, cancelled=True)
        # an accepted set that never reached an activation boundary expires
        if self._pending_directives is not None:
            self._pending_directives = None
            self._pending_directives_level = None
        self.boundary_queue.expire("episode-ended")

    # -- decisions -------------------------------------------------------
    def _request_page(self, deadline) -> bool:
        """Send the single owed get_page, then read one line."""
        preq = self.req.next_page_request()
        if preq is None:
            # a page response is outstanding: wait for it
            return self._pump(deadline)
        write_dl = time.monotonic() + self.c.answer_deadline
        if deadline is not None:
            write_dl = min(write_dl, deadline)
        self._emit("get_page", preq, need_key=self.pending_key,
                   write_deadline=write_dl)
        # mark requested only after the complete request line was written
        self.req.mark_page_requested(preq["page"])
        return self._pump(deadline)

    def _answer_now(self, deadline) -> bool:
        need = self.pending_need
        proposal, provider, reason, latency, usage, decided_low = \
            self._decide(need, deadline)
        now = time.monotonic()
        write_dl = now + self.c.answer_deadline
        if deadline is not None:
            # the answer must still go out even when the content deadline has
            # just passed, so the bounded write keeps a small floor
            write_dl = min(write_dl, max(deadline, now + 0.25))
        # Escalation is based on the FINAL selection outcome, not on an
        # intermediate provider result: a scripted proposal that then fails
        # local validation is a fallback, so the streak must not have been
        # reset by the (discarded) scripted score.
        low = decided_low
        if proposal is None:
            selected = self._safe_fallback(need)
            sel_reason = ("no proposal (%s): safe fallback"
                          % (reason or "none"))
            low = True
        else:
            err = protocol.validate_action(need, proposal)
            if err:
                selected = self._safe_fallback(need)
                sel_reason = "validation fallback: %s" % err
                low = True
            else:
                selected = proposal
                sel_reason = reason
        self._note_low_conf(low)
        self._activate_pending_directives(need)
        view = self.book.view(self.tick, self.mem.status.dlvl,
                              self._precondition_state())
        boundaries = [b.eid for b in self.need_boundaries]
        # The complete validated set is recorded, not just its goals: the
        # target, risk, TTL, preconditions and explanation round-trip.
        directives = [view.dset.to_dict()] if view.active else []
        self.rec.record_decision(
            proposal=proposal, selected=selected, provider=provider,
            reason=sel_reason, boundaries=boundaries, latency=latency,
            usage=usage, directives=directives)
        obj = protocol.make_act(self.pending_seq, need["id"], selected)
        self._emit("act", obj, need_key=self.pending_key,
                   write_deadline=write_dl)
        # requested/pending state mutates only after the complete send
        if need.get("kind") in ("command", "key", "direction"):
            self.tick += 1
        self.pending = False
        self.force_fallback = False
        return True

    def _decide(self, need, need_deadline=None):
        """Answer one need within an *absolute* reflex deadline.

        The deadline is handed to the provider in the context and the call is
        run on a bounded thread: a stuck provider is abandoned (a later wave
        runs it in a killable worker), and the controller always returns a
        bounded scripted fallback rather than hanging the wire.

        Returns ``(proposal, provider, reason, latency, usage, low_conf)``;
        ``low_conf`` is the *provider-side* contribution to escalation, which
        :meth:`_answer_now` combines with the final validation outcome.
        """
        if self.force_fallback:
            self.ledger.reflex_fallback += 1
            return (self._safe_fallback(need), "controller",
                    "forced fallback", 0.0, {}, True)
        reflex_dl = None
        if self.c.reflex_deadline > 0:
            reflex_dl = time.monotonic() + self.c.reflex_deadline
        if need_deadline is not None:
            reflex_dl = (need_deadline if reflex_dl is None
                         else min(reflex_dl, need_deadline))
        ctx = self._reflex_context(need, reflex_dl)
        t0 = time.monotonic()
        if self.reflex_provider.name == "scripted":
            return self._decide_scripted(ctx, reflex_dl, t0)
        return self._decide_jev(ctx, reflex_dl, t0)

    def _reflex_context(self, need, reflex_dl):
        st = self.mem.status
        view = self.book.view(self.tick, st.dlvl, self._precondition_state())
        return ReflexContext(
            episode=self.result.index, tick=self.tick, need=need,
            need_key=self.pending_key, snapshot=self.snap,
            pages=self.req.page_rows(), memory=self.mem,
            directives=[view] if view.active else [],
            deadline=reflex_dl or 0.0)

    def _decide_scripted(self, ctx, reflex_dl, t0):
        """The always-available tier, bounded by the reflex deadline.

        A scripted answer is never itself low confidence -- the score is a
        heuristic uncertainty measure, not a calibrated probability -- but a
        timeout, a provider error or a missing result is.
        """
        self.ledger.reflex_attempted += 1
        call = _ReflexCall(
            lambda: self.reflex_provider.decide(ctx, reflex_dl or 0.0))
        call.start()
        wait = None if reflex_dl is None else reflex_dl - time.monotonic()
        finished = call.wait(wait)
        latency = time.monotonic() - t0
        if not finished:
            self.reflex_timeouts += 1
            self.ledger.reflex_timeout += 1
            return (None, "scripted",
                    "reflex deadline exceeded (>%.2fs, provider still "
                    "running)" % self.c.reflex_deadline, latency, {}, True)
        if call.error is not None:
            if isinstance(call.error, ReflexTimeout):
                self.reflex_timeouts += 1
                self.ledger.reflex_timeout += 1
                return (None, "scripted", "reflex deadline exceeded: %s"
                        % call.error, latency, {}, True)
            if isinstance(call.error, Exception):
                self.ledger.reflex_fallback += 1
                return (None, "scripted", "reflex provider error: %s"
                        % call.error, latency, {}, True)
            raise call.error
        res = call.result
        if res is None:
            self.ledger.reflex_fallback += 1
            return (None, "controller", "reflex returned no result",
                    latency, {}, True)
        self.ledger.reflex_successful += 1
        return (res.action, res.provider or "scripted", res.reason, latency,
                res.usage, False)

    def _decide_jev(self, ctx, reflex_dl, t0):
        """Optional paid reflex: one immutable job, bounded, else scripted.

        Scripted safety is computed first and answers immediately if the paid
        tier is unavailable, capped or wrong -- never await a paid tier to
        answer a crisis.  Any paid result, accepted or not, contributes its
        *returned usage* to the episode's token/USD accounting, so a paid
        reflex cannot spend outside the budget.
        """
        scripted = self.reflex.fallback(ctx)
        fallback_action = scripted.action if scripted is not None else None
        if not self.rec_healthy:
            # A recorder that has already failed this episode disables all
            # paid dispatch (Wave-1 graceful-stop policy).  Checked *before*
            # the paid reservation so a failure observed by the event sink in
            # this same iteration cannot start a paid call.
            self.ledger.reflex_fallback += 1
            return (fallback_action, "scripted",
                    "jev disabled: recorder unhealthy", 0.0, {}, True)
        availability = self.reflex_provider.available(self.c.config)
        if not availability.enabled:
            self.ledger.reflex_fallback += 1
            return (fallback_action, "scripted",
                    "jev unavailable: %s" % availability.reason, 0.0, {},
                    True)
        if not self.ledger.reflex_paid_available():
            self.ledger.reflex_fallback += 1
            return (fallback_action, "scripted",
                    "jev paid-reflex cap reached",
                    0.0, {}, True)
        self.ledger.reserve_reflex_paid()
        self.ledger.reflex_attempted += 1
        call = _ReflexCall(
            lambda: self.reflex_provider.decide(ctx, reflex_dl or 0.0))
        call.start()
        wait = None if reflex_dl is None else reflex_dl - time.monotonic()
        finished = call.wait(wait)
        latency = time.monotonic() - t0
        res = call.result if (finished and call.error is None) else None
        if not finished:
            self.reflex_timeouts += 1
            self.ledger.reflex_timeout += 1
        usage = res.usage if res is not None else {}
        # exactly once: a paid body is billed whether or not it is accepted
        self.ledger.add_usage(usage)
        if res is None or res.action is None:
            self.ledger.reflex_fallback += 1
            why = getattr(self.reflex_provider, "last_error", "") \
                or (res.reason if res is not None else "") or "no answer"
            return (fallback_action, "scripted", "jev fallback: %s" % why,
                    latency, usage, True)
        self.ledger.reflex_successful += 1
        conf = res.confidence
        numeric = (isinstance(conf, (int, float))
                   and not isinstance(conf, bool))
        low = not (numeric
                   and conf >= self.c.config.confidence_threshold)
        return (res.action, res.provider, res.reason, latency, res.usage,
                low)

    def _safe_fallback(self, need) -> dict:
        """A structurally valid, non-blocking answer for any need kind.

        The command fallback is deliberately *non-resting*.  It is reached
        only when the reflex failed, timed out or proposed something invalid,
        and it is context-free by construction, so it cannot prove a rest
        safe.  The policy's own choice when rest cannot be proven safe is a
        search, which spends the turn without moving into unknown space or
        into an adjacent hazard -- so a fallback can never hold position
        beside a monster, while hungry, at low HP, or with the hero square
        unknown.
        """
        kind = need.get("kind")
        if kind in ("command", "key", "direction"):
            return {"key": protocol.KEY_SEARCH}
        if kind == "yn":
            return {"yn": protocol.KEY_ESC}
        if kind == "position":
            return {"key": protocol.KEY_ESC}
        if kind in ("line", "extcmd"):
            return {"cancel": True}
        if kind == "menu":
            return {"cancel": True}
        if kind == "ack":
            return {"ack": True}
        return {"key": protocol.KEY_ESC}


def _safe_config(config: ProviderConfig) -> dict:
    return {
        "reflex": config.reflex,
        "strategy": config.strategy,
        "role": config.role,
        "max_ticks": config.max_ticks,
        "confidence_threshold": config.confidence_threshold,
        "strategy_call_cap": config.strategy_call_cap,
        "deepseek_model": config.deepseek_model,
        "deepseek_base_url": config.deepseek_base_url,
        "reflex_deadline": getattr(config, "reflex_deadline", None),
        "answer_deadline": getattr(config, "answer_deadline", None),
        "content_deadline": getattr(config, "content_deadline", None),
        "strategy_deadline": getattr(config, "strategy_deadline", None),
        "postmortem_reserve": getattr(config, "postmortem_reserve", None),
        "deepseek_max_tokens": getattr(config, "deepseek_max_tokens", None),
        "deepseek_max_bytes": getattr(config, "deepseek_max_bytes", None),
        "deepseek_history_pairs": getattr(config,
                                          "deepseek_history_pairs", None),
        "deepseek_context_max_bytes": getattr(
            config, "deepseek_context_max_bytes", None),
        "deepseek_price_in": getattr(config, "deepseek_price_in", None),
        "deepseek_price_out": getattr(config, "deepseek_price_out", None),
        "deepseek_price_cache_hit": getattr(config,
                                            "deepseek_price_cache_hit", None),
        "boundary_cooldown_ticks": getattr(config,
                                           "boundary_cooldown_ticks", None),
        "boundary_cooldown_wall": getattr(config,
                                          "boundary_cooldown_wall", None),
        "low_confidence_needs": getattr(config, "low_confidence_needs", None),
        "reflex_call_cap": getattr(config, "reflex_call_cap", None),
        "jev_base_url": ("configured" if getattr(config, "jev_base_url", None)
                         else None),
        "jev_accept_terms": bool(getattr(config, "jev_accept_terms", False)),
        "usd_cap": getattr(config, "usd_cap", None),
        "token_cap": getattr(config, "token_cap", None),
    }


def episode_ok(r: EpisodeResult) -> bool:
    """The campaign success predicate for one episode.

    ``closed`` alone is best-effort evidence, not proof: success also requires
    a clean spawn, no forced kill or teardown failure, no unanswered request,
    no protocol/transport/deadline failure, a zero launcher exit status and a
    complete recording.  This is the single authority the CLI
    (``tools.agent.__main__``) re-exports, so the campaign summary and the CLI
    verdict cannot disagree.
    """
    return (r.spawn_ok and r.closed and not r.forced_kill and not r.eof
            and not r.unanswered and not r.teardown_failure
            and r.protocol_failure is None and r.failure_reason is None
            and r.returncode == 0 and r.recording_complete)


def _episode_summary(r: EpisodeResult) -> dict:
    usage = (r.budget or {}).get("usage", {}) if r.budget else {}
    return {
        "index": r.index,
        "ok": episode_ok(r),
        "stop_reason": r.stop_reason,
        "outcome": r.outcome,
        "closed": r.closed, "eof": r.eof, "forced_kill": r.forced_kill,
        "unanswered": r.unanswered,
        "recording_complete": r.recording_complete,
        "returncode": r.returncode,
        "protocol_failure": r.protocol_failure,
        "failure_reason": r.failure_reason,
        "ticks": r.ticks, "needs": r.needs, "actions": r.actions,
        "invalids": r.invalids,
        "boundaries": r.boundaries, "strategy_calls": r.strategy_calls,
        "directives_applied": r.directives_applied,
        # Reported usage, unknown-price calls and unknown *exposure* are three
        # distinct things: the first is asserted cost, the second is a real
        # answer whose price is unknown, the third is a call that reached the
        # wire but returned no usage at all.  They are never merged into a
        # single asserted figure.
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "estimated_usd": usage.get("estimated_usd", 0.0),
            "unknown_price_calls": usage.get("unknown_price_calls", 0),
            "unknown_exposure_calls": usage.get("unknown_exposure_calls", 0),
            "unknown_exposure_tokens": usage.get("unknown_exposure_tokens",
                                                 0),
            "unknown_exposure_usd": usage.get("unknown_exposure_usd", 0.0),
            # reported prompt-cache accounting: a hit rate over *classified*
            # tokens only, with the unclassified count carried separately so a
            # high rate over thin reporting coverage is not misread
            "cache_hit_tokens": usage.get("cache_hit_tokens", 0),
            "cache_miss_tokens": usage.get("cache_miss_tokens", 0),
            "cache_unclassified_tokens": usage.get(
                "cache_unclassified_tokens", 0),
            "cache_hit_rate": usage.get("cache_hit_rate"),
            "reasoning_tokens": usage.get("reasoning_tokens", 0),
        },
    }


def campaign_summary(results, config, episode_timeout: float) -> dict:
    """A compact, secret-free rollup of one campaign's episodes."""
    episodes = [_episode_summary(r) for r in results]
    totals = {"ticks": 0, "needs": 0, "actions": 0, "invalids": 0,
              "boundaries": 0, "strategy_calls": 0, "directives_applied": 0,
              "prompt_tokens": 0, "completion_tokens": 0,
              "estimated_usd": 0.0, "unknown_price_calls": 0,
              "unknown_exposure_calls": 0, "unknown_exposure_tokens": 0,
              "unknown_exposure_usd": 0.0,
              "cache_hit_tokens": 0, "cache_miss_tokens": 0,
              "cache_unclassified_tokens": 0, "reasoning_tokens": 0,
              "cache_hit_rate": None}
    for e in episodes:
        for key in ("ticks", "needs", "actions", "invalids", "boundaries",
                    "strategy_calls", "directives_applied"):
            totals[key] += e[key]
        u = e["usage"]
        totals["prompt_tokens"] += u["prompt_tokens"]
        totals["completion_tokens"] += u["completion_tokens"]
        totals["estimated_usd"] = round(
            totals["estimated_usd"] + u["estimated_usd"], 6)
        totals["unknown_price_calls"] += u["unknown_price_calls"]
        totals["unknown_exposure_calls"] += u["unknown_exposure_calls"]
        totals["unknown_exposure_tokens"] += u["unknown_exposure_tokens"]
        totals["unknown_exposure_usd"] = round(
            totals["unknown_exposure_usd"] + u["unknown_exposure_usd"], 6)
        totals["cache_hit_tokens"] += u["cache_hit_tokens"]
        totals["cache_miss_tokens"] += u["cache_miss_tokens"]
        totals["cache_unclassified_tokens"] += u["cache_unclassified_tokens"]
        totals["reasoning_tokens"] += u["reasoning_tokens"]
    # The campaign rate is computed from the *summed* H and M, never as an
    # average of per-episode percentages: a short episode must not weigh as
    # much as a long one.
    classified = totals["cache_hit_tokens"] + totals["cache_miss_tokens"]
    if classified > 0:
        totals["cache_hit_rate"] = round(
            totals["cache_hit_tokens"] / float(classified), 6)
    successes = sum(1 for e in episodes if e["ok"])
    return {
        "schema": 1,
        "episodes": len(episodes),
        "episodes_success": successes,
        "episodes_failed": len(episodes) - successes,
        "episode_timeout": episode_timeout,
        "config": _safe_config(config),
        "totals": totals,
        "results": episodes,
    }


def write_campaign_summary(output_dir: str, results, config,
                           episode_timeout: float) -> str:
    """Write ``campaign.json`` into *output_dir* at 0600; return its path."""
    ensure_private_dir(output_dir)
    path = os.path.join(output_dir, "campaign.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        pass
    with os.fdopen(fd, "w") as fh:
        json.dump(campaign_summary(results, config, episode_timeout), fh,
                  indent=2, sort_keys=True)
        fh.write("\n")
    return path
