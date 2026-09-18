"""Bounded, episode-local live-render transport for autoplay spectating.

The controller owns the wire; this module owns the *side channel* a live
spectator writes to.  It is deliberately synchronous and single-threaded --
no helper process, no rendering thread, no timer service -- so presentation
can never stall the wire, and a stall is bounded by one per-frame deadline.

Design contract (``doc/agent-auto-spectate-plan.md`` sections 5 and 6):

  * ``open_destination`` opens an independently owned descriptor, never the
    caller's, and never one whose status flags we alter.  fd 1 and detectable
    stdout aliases (a shared FIFO, regular file or socket) are rejected before
    a single frame is written; a ``/dev/tty`` that cannot be opened falls back
    to a duplicate of fd 2 with one best-effort note per campaign.
  * :meth:`RenderDestination.write` writes in chunks no larger than PIPE_BUF,
    select-gated before every write, against ONE absolute monotonic deadline
    that progress, EINTR and EAGAIN never extend.  A fully delivered payload
    returns True; an exhausted deadline returns False; anything else raises
    for the enclosing guard.
  * :class:`RenderStream` coalesces candidates (only the newest survives),
    rate-limits attempts by the interval, and disables itself after a fixed
    number of consecutive exhausted frames.  Drops are renderer-local: only
    ``frames_rendered`` and ``disabled_reason`` ever leave this module.
  * On a TTY the painter redraw is transactional: the displayed height commits
    only after a complete delivery, and a partially written frame forces an
    absolute resynchronization before the next ordinary frame.

Everything here is a pure library: it imports only the standard library and
``tools.agent.render`` (for the painter's transactional compose/commit split).
"""

import errno
import fcntl
import os
import select
import stat
import time

from .render import FramePainter

# One per-frame write budget, and the number of exhausted frames in a row
# after which the whole episode's rendering is disabled.
FRAME_WRITE_TIMEOUT = 0.25
CONSECUTIVE_STALL_LIMIT = 3
DEFAULT_INTERVAL = 0.15

# The absolute TTY resynchronization sequence: full clear + home.  Emitted
# before the next ordinary frame after a partial write left the cursor
# position unknown, so the frame is drawn from a known origin rather than
# continued in place.
_TTY_RESYNC = "\x1b[2J\x1b[H"

_DESTINATIONS = ("tty", "stderr", "none")


class SpectateError(Exception):
    """A spectate destination could not be prepared (disables the episode)."""


def validate_spectate(destination, interval):
    """Return None for a usable (destination, interval), else a reason.

    Rejected before any episode launches: an unknown destination, a
    non-numeric interval, and a negative, NaN or infinite interval.
    """
    if destination not in _DESTINATIONS:
        return ("spectate destination must be one of %s, not %r"
                % (", ".join(_DESTINATIONS), destination))
    try:
        value = float(interval)
    except (TypeError, ValueError):
        return "spectate interval must be a number, not %r" % (interval,)
    if value != value or value in (float("inf"), float("-inf")):
        return "spectate interval must be finite"
    if value < 0:
        return "spectate interval must not be negative"
    return None


# ---------------------------------------------------------- fd helpers

def _close_fd(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _isatty(fd):
    try:
        return os.isatty(fd)
    except OSError:
        return False


def _aliases_stdout(fd):
    """True when ``fd`` is a detectable alias of stdout (fd 1).

    Only a shared pipe, regular file or socket counts: two *independent* opens
    of the same character device (a terminal) are a legitimate separate sink,
    and framing that as an alias would reject the documented default.  The
    test is deliberately conservative (same st_dev/st_ino for one of those
    three kinds), so a separately opened regular file is also refused.
    """
    try:
        a = os.fstat(fd)
        b = os.fstat(1)
    except OSError:
        return False
    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
        return False
    mode = a.st_mode
    return (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)
            or stat.S_ISSOCK(mode))


def _relocate_low(fd):
    """Move an owned fd >= 3 if it landed on 0/1/2, leaving the low slot shut.

    ``os.dup``/``os.open`` return the lowest free descriptor, so a closed
    stdout can hand an owned render fd the value 1.  That descriptor must
    never be repurposed: it is relocated with ``F_DUPFD`` to >= 3 and the low
    slot is closed again, so fd 1 stays closed exactly as it was.
    """
    if fd >= 3:
        return fd
    new = fcntl.fcntl(fd, fcntl.F_DUPFD, 3)
    os.close(fd)
    return new


def _set_nonblock(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _pipe_buf(fd):
    """The safe atomic chunk bound: PIPE_BUF via fpathconf, else the const."""
    try:
        value = int(os.fpathconf(fd, "PC_PIPE_BUF"))
    except (OSError, ValueError):
        return select.PIPE_BUF
    return value if value > 0 else select.PIPE_BUF


# ---------------------------------------------------------- destination

class RenderDestination(object):
    """One owned descriptor for rendered frames, written under a deadline.

    ``fd`` is None for the ``none`` destination, which opens, dups and probes
    nothing and does no clock work.  ``owned`` says we opened (or dup'd) and
    must close it; ``independent`` says it has its own open-file description
    (so setting O_NONBLOCK on it cannot affect anyone else's fd).
    """

    def __init__(self, fd, *, owned=True, independent=False, tty=False,
                 label="fd", note=None, clock=time.monotonic,
                 select_fn=None, write_fn=None, chunk_limit=None):
        self.fd = fd
        self.owned = owned
        self.independent = independent
        self.tty = tty
        self.label = label
        self.note = note
        self.bytes_written = 0
        self.chunk_limit = chunk_limit if chunk_limit is not None \
            else (_pipe_buf(fd) if fd is not None else select.PIPE_BUF)
        self.clock = clock
        self._select = select_fn or select.select
        self._write = write_fn or os.write
        self._closed = False

    def write(self, data, *, deadline):
        """Write the whole payload before ``deadline``; True iff complete.

        Returns False the moment the one absolute deadline is exhausted
        (progress, EINTR and EAGAIN never reset it), and raises any other
        write error for the enclosing guard.
        """
        if self.fd is None:
            return True
        payload = memoryview(bytes(data))
        while len(payload):
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            try:
                _, writable, _ = self._select([], [self.fd], [], remaining)
            except InterruptedError:
                continue
            if not writable:
                continue                 # select timed out; recheck remaining
            if self.clock() >= deadline:
                return False
            chunk = payload[:min(self.chunk_limit, len(payload))]
            try:
                written = self._write(self.fd, chunk)
            except InterruptedError:
                continue
            except BlockingIOError:
                continue                 # EAGAIN: back to select
            if written <= 0:
                raise OSError(errno.EIO, "destination accepted no data")
            self.bytes_written += written
            payload = payload[written:]
        return True

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.owned and self.fd is not None:
            _close_fd(self.fd)


def _open_stderr_destination(clock=time.monotonic, **kw):
    """Own a duplicate of OS fd 2; never touch its status flags."""
    try:
        fd = os.dup(2)
    except OSError as exc:
        raise SpectateError("stderr (fd 2) is not usable: %s" % exc)
    # Relocate first: if a closed stdout handed this dup the value 1, move it
    # to >= 3 and leave fd 1 closed before deciding anything else.
    fd = _relocate_low(fd)
    if _aliases_stdout(fd):
        _close_fd(fd)
        raise SpectateError(
            "stderr (fd 2) is an alias of stdout (fd 1): spectating there "
            "would corrupt the wire")
    # A duplicate shares fd 2's open-file description, so its flags are never
    # altered; it is normally blocking.
    return RenderDestination(fd, owned=True, independent=False,
                             tty=_isatty(fd), label="fd 2", clock=clock, **kw)


def _open_tty_destination(clock=time.monotonic, **kw):
    try:
        fd = os.open("/dev/tty", os.O_WRONLY)
    except OSError:
        fallback = _open_stderr_destination(clock=clock, **kw)
        fallback.note = ("no controlling terminal: frames fall back to fd 2")
        return fallback
    fd = _relocate_low(fd)
    _set_nonblock(fd)                    # own open-file description: safe
    return RenderDestination(fd, owned=True, independent=True, tty=True,
                             label="/dev/tty", clock=clock, **kw)


def open_destination(destination, clock=time.monotonic, **kw):
    """Prepare the frame side channel for ``destination``.

    ``none`` returns an inert descriptor that opens and probes nothing.
    Raises :class:`SpectateError` when a real destination cannot be prepared
    safely (an alias of stdout, an unusable fd 2), which disables spectating
    for the episode without touching spawn or gameplay.
    """
    if destination == "none":
        return RenderDestination(None, owned=False, label="none", clock=clock,
                                 **kw)
    if destination == "tty":
        return _open_tty_destination(clock=clock, **kw)
    if destination == "stderr":
        return _open_stderr_destination(clock=clock, **kw)
    raise SpectateError("unknown spectate destination %r" % (destination,))


# ------------------------------------------------------------------ stream

def _sanitize_note(text):
    out = []
    for ch in str(text):
        o = ord(ch)
        out.append(ch if 32 <= o <= 126 else " ")
    return "".join(out)[:400]


class RenderStream(object):
    """Episode-local scheduler and transaction around a RenderDestination.

    No thread, no clock service: ``offer`` records the newest candidate and
    flushes it when due; the controller's own select loop calls ``flush`` (or
    ``next_due``) at the run boundary and at its existing wire select site.
    """

    def __init__(self, destination, interval, *, clock=time.monotonic,
                 write_timeout=FRAME_WRITE_TIMEOUT, diagnostic=None):
        self.destination = destination
        self.interval = float(interval)
        self.clock = clock
        self.write_timeout = write_timeout
        self.diagnostic = diagnostic
        self.frames_rendered = 0
        self.frames_dropped = 0
        self.disabled_reason = None
        self._coalesced = 0            # private: candidates replaced unseen
        self._pending = None
        self._last_attempt = None      # completion time of the last attempt
        self._stalls = 0
        self._height = 0               # committed TTY displayed height
        self._pos_unknown = False
        self._closed = False
        self.painter = FramePainter(color=False, tty=destination.tty)

    # -- read-only view --------------------------------------------------
    @property
    def disabled(self):
        return self.disabled_reason is not None

    def next_due(self):
        """The absolute monotonic time a pending frame is due, or None.

        None when there is no pending candidate or the stream is disabled.
        The first attempt is due immediately, so a fresh candidate is never
        delayed by the interval.
        """
        if self.disabled or self._closed or self._pending is None:
            return None
        if self._last_attempt is None:
            return self.clock()
        return self._last_attempt + self.interval

    def _due(self):
        if self._last_attempt is None:
            return True
        return self.clock() - self._last_attempt >= self.interval

    # -- candidates ------------------------------------------------------
    def offer(self, lines):
        """Detach ``lines`` as the sole pending candidate and flush if due."""
        if self.disabled or self._closed:
            return
        if self._pending is not None:
            self._coalesced += 1
        self._pending = list(lines)
        self.flush()

    def flush(self, force=False, deadline_cap=None):
        """Attempt the pending frame when due (or forced), then clear it.

        ``deadline_cap`` is an absolute monotonic bound (the remaining wire
        deadline) that further caps the per-frame write deadline, so a render
        wake can never extend the wire's own timeout.

        Returns True when a frame was actually serviced (attempted or
        dropped), False when nothing was due or the stream is off -- so the
        controller can tell a real render wake from an idle select return.
        """
        if self.disabled or self._closed or self._pending is None:
            return False
        if not force and not self._due():
            return False
        lines = self._pending
        self._pending = None
        self._attempt(lines, deadline_cap=deadline_cap)
        return True

    def finish(self, lines=None):
        """Force one final frame (fresh ``lines`` or the pending candidate).

        Always a fresh composition when ``lines`` is given; never a replay of
        a prior candidate's bytes.  Clears any pending candidate.
        """
        if self.disabled or self._closed:
            return
        if lines is not None:
            self._pending = list(lines)
        if self._pending is None:
            return
        lines = self._pending
        self._pending = None
        self._attempt(lines)

    def disable(self, reason, message=None):
        """Disable rendering for the rest of the episode, once."""
        if self.disabled_reason is not None:
            return
        self.disabled_reason = reason
        self._pending = None
        self._diagnose(message or ("spectate disabled: %s" % reason))

    def close(self):
        """Idempotent teardown: drop the pending frame and close the sink."""
        if self._closed:
            return
        self._closed = True
        self._pending = None
        if self.destination is not None:
            self.destination.close()

    # -- internals -------------------------------------------------------
    def _encode(self, lines):
        if not self.destination.tty:
            return "".join(line + "\n" for line in lines).encode(
                "utf-8", "replace")
        height = 0 if self._pos_unknown else self._height
        data = self.painter.compose(lines, height)
        if self._pos_unknown:
            data = _TTY_RESYNC.encode("ascii") + data
        return data

    def _attempt(self, lines, deadline_cap=None):
        now = self.clock()
        deadline = now + self.write_timeout
        if deadline_cap is not None:
            deadline = min(deadline, deadline_cap)
        if deadline <= now:
            # The wire bound is already spent: drop the frame rather than
            # grant a fresh allowance that would extend that timeout.
            self._last_attempt = now
            self._drop()
            return
        payload = self._encode(lines)
        dest = self.destination
        before = getattr(dest, "bytes_written", 0)
        ok = dest.write(payload, deadline=deadline)
        after = getattr(dest, "bytes_written", 0)
        self._last_attempt = self.clock()
        if ok:
            self.frames_rendered += 1
            self._stalls = 0
            if dest.tty:
                self._height = len(lines)
                self._pos_unknown = False
            return
        if dest.tty and after > before:
            # A partial frame left the cursor position unknown; the next
            # ordinary frame must resynchronize absolutely.
            self._pos_unknown = True
        self._drop()

    def _drop(self):
        self.frames_dropped += 1
        self._stalls += 1
        if self._stalls >= CONSECUTIVE_STALL_LIMIT:
            self.disable(
                "write-deadline",
                "spectate disabled: write-deadline "
                "(%d consecutive frame stalls)" % self._stalls)

    def _diagnose(self, text):
        """Best-effort, sanitized, attempted once.

        The injected ``diagnostic`` sink owns any actual write (with the same
        fd safety and deadline discipline).  With no safe sink the reason is
        simply retained in ``disabled_reason`` for the recording meta rather
        than waiting on a stalled destination.
        """
        if self.diagnostic is None:
            return
        try:
            self.diagnostic(_sanitize_note(text))
        except Exception:                    # noqa: BLE001 - best effort
            pass
