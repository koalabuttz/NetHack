"""Wave-2 tests: boundaries, directives, budgets, workers, providers.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Every network test talks to a **fake HTTP endpoint on loopback** driven by
the real worker process, so the whole path -- spawn, bounded POST, size and
redirect guards, deadline, kill, reap -- is exercised without touching a
real provider.  The default configuration is asserted to be network-free.
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_auto import (BLANK, CLOSED, HELLO, WireHarness,  # noqa: E402
                       _FakeStderr, _FakeStdin, _FakeStdout, _line,
                       _parse_actions, _read_jsonl, _wait_gone, ack_need,
                       hello)
from tools.agent import (budget, controller, directives, events,  # noqa
                         policy, protocol, providers, recording, state,
                         worker)
from tools.agent.providers import (Availability, ProviderConfig,  # noqa
                                   ReflexContext, ReflexResult,
                                   StrategyContext, StrategyResult)

DSEV = directives


# ------------------------------------------------------------- fake endpoint

def _chat_body(content, prompt_tokens=10, completion_tokens=5):
    if not isinstance(content, str):
        content = json.dumps(content)
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }).encode("utf-8")


def _ok_directives(goals=("survive",), ttl=50):
    return {"schema_version": 1, "goals": list(goals), "ttl": ttl,
            "explanation": "fake"}


class FakeEndpoint(object):
    """A loopback HTTP endpoint with controllable timing and responses."""

    def __init__(self, responder=None):
        self.responder = responder or (
            lambda path, body: (200, _chat_body(_ok_directives())))
        self.requests = []
        self.delay = 0.0
        self.drip = 0.0
        self.hang = 0.0
        handler = _make_handler(self)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base_url = "http://127.0.0.1:%d" % self.port
        self._t = threading.Thread(target=self.server.serve_forever,
                                   daemon=True)
        self._t.start()

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:                    # noqa: BLE001
            pass


def _make_handler(owner):
    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            owner.requests.append({
                "path": self.path, "body": body,
                "auth": self.headers.get("Authorization")})
            if owner.hang:
                time.sleep(owner.hang)
                return
            if owner.delay:
                time.sleep(owner.delay)
            status, payload = owner.responder(self.path, body)
            if not isinstance(payload, bytes):
                payload = payload.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if owner.drip:
                    for i in range(0, len(payload), 16):
                        self.wfile.write(payload[i:i + 16])
                        self.wfile.flush()
                        time.sleep(owner.drip)
                else:
                    self.wfile.write(payload)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, *a):
            pass

    return _H


# ------------------------------------------------------------- fake strategy

class FakeStrategy(providers.StrategyProvider):
    """A deterministic, in-process strategy provider (no network)."""

    name = "fake"

    def __init__(self, payload=None, available=True, delay=0.0, ok=True,
                 gate=None):
        self.payload = payload
        self._available = available
        self._delay = delay
        self._ok = ok
        self.gate = gate
        self.calls = []
        self.cancelled = 0

    def available(self, config):
        return Availability(self._available,
                            "fake" if self._available else "fake disabled")

    def deliberate(self, context, deadline=0.0):
        self.calls.append(context)
        if self.gate is not None:
            self.gate.wait(5.0)
        if self._delay:
            time.sleep(self._delay)
        if not self._ok or self.payload is None:
            return StrategyResult(provider="fake", reason="no plan", ok=False)
        dset = self.payload
        if not isinstance(dset, DSEV.DirectiveSet):
            dset, why = DSEV.validate_directive_set(dset)
            if dset is None:
                return StrategyResult(provider="fake",
                                      reason="invalid: %s" % why, ok=False)
        return StrategyResult(directives=[dset], provider="fake",
                              reason="directives", ok=True,
                              usage={"prompt_tokens": 5,
                                     "completion_tokens": 3,
                                     "reported": True})

    def cancel(self):
        self.cancelled += 1


class PacedProc(object):
    """A process-shaped peer that feeds records with gaps between them.

    The controller's loop only services the strategy tier at the top of each
    iteration, so pacing the wire is what makes the dispatch/finalize/activate
    sequence deterministic: a gap after a boundary gives the (instant, fake)
    strategy call time to complete before the next record is read.
    """

    def wait(self, timeout=None):
        self._t.join(timeout=timeout or 5)
        return self.returncode

    def kill(self):
        self.killed = True

    def terminate(self):
        self.killed = True

    def close(self):
        wr = getattr(self, "_wr", None)
        if wr is not None and not wr.closed:
            try:
                wr.close()
            except OSError:
                pass
        try:
            os.close(self.r)
        except OSError:
            pass


def paced(records, gaps, eof=True):
    proc = PacedProc.__new__(PacedProc)
    proc.r, proc.w = os.pipe()
    proc.stdin = _FakeStdin()
    proc.stdout = _FakeStdout(proc.r)
    proc.stderr = _FakeStderr()
    proc.returncode = 0
    proc.killed = False
    proc._records = [_line(r) for r in records]
    proc._gaps = list(gaps)
    proc._eof = eof
    proc._t = threading.Thread(target=_feed_paced, args=(proc,), daemon=True)
    proc._t.start()
    return proc


def _feed_paced(proc):
    try:
        wr = os.fdopen(proc.w, "wb", buffering=0)
        # keep the write end referenced (and open) when eof is False: the
        # controller must see a live, silent peer rather than a closed pipe
        proc._wr = wr
        for rec in proc._records:
            wr.write(rec)
            time.sleep(proc._gaps.pop(0) if proc._gaps else 0.0)
        if proc._eof:
            wr.close()
    except (OSError, IndexError):
        pass


def st_obs(seq, need=None, dlvl=None, hp=None, hp_max=None, hunger=None,
           msg=(), map_=None, xl=None):
    s = {}
    if hp is not None:
        s["hitpoints"] = {"text": str(hp)}
    if hp_max is not None:
        s["hitpoints-max"] = {"text": str(hp_max)}
    if hunger is not None:
        s["hunger"] = {"text": hunger}
    if dlvl is not None:
        s["dungeon-level"] = {"text": dlvl}
    if xl is not None:
        s["experience-level"] = {"text": str(xl)}
    return {"v": 1, "ch": "player", "type": "obs", "d": seq, "seq": seq,
            "base": None, "s": s, "cond": [], "pal": BLANK,
            "map": map_ or [], "cur": None, "msg": list(msg), "hist": [],
            "windows": [], "need": need}


def command_need(i):
    return {"id": i, "kind": "command", "prompt": ""}


# ============================================================ directives

class TestDirectiveValidation(unittest.TestCase):
    def test_a_valid_set_is_accepted(self):
        dset, why = DSEV.validate_directive_set({
            "schema_version": 1, "goals": ["survive", "acquire_food"],
            "target": [10, 5], "risk": 0.25, "ttl": 30,
            "preconditions": ["hero_known"], "explanation": "eat then run"})
        self.assertEqual(why, "")
        self.assertEqual(dset.goals, ("survive", "acquire_food"))
        self.assertEqual(dset.target, (10, 5))
        self.assertEqual(dset.ttl, 30)

    def test_minimal_set_defaults(self):
        dset, why = DSEV.validate_directive_set({"goals": ["recover"]})
        self.assertEqual(why, "")
        self.assertEqual(dset.schema_version, 1)
        self.assertEqual(dset.risk, 0.0)
        self.assertEqual(dset.ttl, 1)

    def test_rejections(self):
        bad = [
            ({"goals": ["fly"]}, "unknown goal"),
            ({"goals": []}, "non-empty"),
            ({"goals": ["survive"], "key": 255}, "looks like wire content"),
            ({"goals": ["survive"], "menu": "m1"}, "looks like wire content"),
            ({"goals": ["survive"], "action": "x"},
             "looks like wire content"),
            ({"goals": ["survive"], "extra": 1}, "unexpected field"),
            ({"goals": ["survive"], "risk": 2}, "[0,1]"),
            ({"goals": ["survive"], "risk": float("nan")}, "[0,1]"),
            ({"goals": ["survive"], "ttl": 0}, "ttl"),
            ({"goals": ["survive"], "ttl": 99999}, "ttl"),
            ({"goals": ["survive"], "target": [0, 5]}, "outside"),
            ({"goals": ["survive"], "target": [1]}, "[x,y]"),
            ({"goals": ["survive"], "preconditions": ["immortal"]},
             "unknown precondition"),
            ({"goals": ["survive"], "explanation": "x" * 500}, "too long"),
            ({"goals": ["survive"], "schema_version": 2}, "schema_version"),
            ({"survive": True}, "unexpected field"),
            ({"goals": ["survive", "survive"]}, "duplicate"),
            ("nope", "not an object"),
        ]
        for obj, needle in bad:
            with self.subTest(obj=obj):
                dset, why = DSEV.validate_directive_set(obj)
                self.assertIsNone(dset)
                self.assertIn(needle, why)

    def test_preconditions_are_evaluated(self):
        dset, _ = DSEV.validate_directive_set(
            {"goals": ["survive"], "preconditions": ["hp_below_half"]})
        st = DSEV.PreconditionState(hero_known=True, hp_known=True,
                                    hp_frac=0.4)
        self.assertTrue(DSEV.preconditions_met(dset, st))
        st.hp_frac = 0.9
        self.assertFalse(DSEV.preconditions_met(dset, st))


class TestDirectiveBook(unittest.TestCase):
    def _dset(self, goals=("survive",), ttl=5):
        dset, why = DSEV.validate_directive_set(
            {"goals": list(goals), "ttl": ttl})
        self.assertEqual(why, "")
        return dset

    def _fresh(self):
        return DSEV.PreconditionState(hero_known=True, inventory_fresh=True)

    def test_activate_and_read(self):
        book = DSEV.DirectiveBook()
        book.activate(self._dset(), tick=3, level="Dlvl:1")
        active = book.active(tick=4, level="Dlvl:1", st=self._fresh())
        self.assertIsNotNone(active)

    def test_level_change_expires(self):
        book = DSEV.DirectiveBook()
        book.activate(self._dset(), tick=3, level="Dlvl:1")
        self.assertIsNone(book.active(tick=4, level="Dlvl:2",
                                      st=self._fresh()))
        self.assertIn("expired", [e["state"] for e in book.events])

    def test_ttl_expires(self):
        book = DSEV.DirectiveBook()
        book.activate(self._dset(ttl=3), tick=0, level="Dlvl:1")
        self.assertIsNotNone(book.active(tick=3, level="Dlvl:1",
                                         st=self._fresh()))
        self.assertIsNone(book.active(tick=4, level="Dlvl:1",
                                      st=self._fresh()))

    def test_precondition_failure_expires(self):
        dset, _ = DSEV.validate_directive_set(
            {"goals": ["survive"], "ttl": 10,
             "preconditions": ["hungry"]})
        book = DSEV.DirectiveBook()
        book.activate(dset, tick=0, level="Dlvl:1")
        st = self._fresh()
        st.hungry = False
        self.assertIsNone(book.active(tick=1, level="Dlvl:1", st=st))

    def test_view_predicates(self):
        dset, _ = DSEV.validate_directive_set(
            {"goals": ["explore_frontier", "acquire_food"]})
        view = DSEV.DirectiveView(dset, 1)
        self.assertTrue(view.prefers_frontier())
        self.assertFalse(view.prefers_stairs())
        self.assertTrue(view.wants_food())
        self.assertEqual(view.top_goal(), "explore_frontier")
        empty = DSEV.DirectiveView(None)
        self.assertFalse(empty.active)
        self.assertEqual(empty.goals, ())


# ============================================================ boundaries

class TestBoundaryDetector(unittest.TestCase):
    def setUp(self):
        self.d = events.BoundaryDetector()

    def check(self, st=None, **kw):
        return self.d.check(st if st is not None else St(), **kw)

    def test_initial_level_then_change_then_return(self):
        b = self.check(st=St(dlvl="Dlvl:1"))
        self.assertEqual([x.reason for x in b], ["initial-level"])
        b = self.check(st=St(dlvl="Dlvl:2"))
        self.assertEqual([x.reason for x in b], ["level-change"])
        b = self.check(st=St(dlvl="Dlvl:1"))
        self.assertEqual([x.reason for x in b], ["level-change"])
        # distinct ids per occurrence
        self.assertEqual(len({x.eid for x in b}), 1)

    def test_repeated_snapshot_yields_no_events(self):
        first = self.check(st=St(dlvl="Dlvl:1", hp=10, hp_max=20,
                                 hunger="Hungry"),
                           classes={"d"}, messages=["You see here a scroll."])
        self.assertGreater(len(first), 0)
        for _ in range(5):
            self.assertEqual(self.check(
                st=St(dlvl="Dlvl:1", hp=10, hp_max=20, hunger="Hungry"),
                classes={"d"}, messages=["You see here a scroll."]), [])
        # a replay of the same history is likewise not new
        self.assertEqual(self.check(st=St(dlvl="Dlvl:1")), [])

    def test_hp_crisis_hysteresis(self):
        self.check(st=St(hp=80, hp_max=100))
        crisis = self.check(st=St(hp=29, hp_max=100))
        self.assertEqual([x.reason for x in crisis], ["hp-crisis"])
        self.assertTrue(crisis[0].severe)
        # staying low does not re-fire
        self.assertEqual(self.check(st=St(hp=25, hp_max=100)), [])
        # rearm above 50% is silent
        self.assertEqual(self.check(st=St(hp=60, hp_max=100)), [])
        second = self.check(st=St(hp=10, hp_max=100))
        self.assertEqual([x.reason for x in second], ["hp-crisis"])
        self.assertNotEqual(crisis[0].eid, second[0].eid)

    def test_hunger_worsens_only(self):
        self.assertEqual([x.reason for x in
                          self.check(st=St(hunger="Hungry"))],
                         ["hunger-hungry"])
        with self.subTest(state="repeat"):
            self.assertEqual(self.check(st=St(hunger="Hungry")), [])
        weak = self.check(st=St(hunger="Weak"))
        self.assertEqual([x.reason for x in weak], ["hunger-weak"])
        faint = self.check(st=St(hunger="Fainting"))
        self.assertEqual([x.reason for x in faint], ["hunger-fainting"])
        self.assertTrue(faint[0].severe)
        # recovery then re-worsening to an already-seen stage is not worse
        self.assertEqual(self.check(st=St(hunger="")), [])
        self.assertEqual(self.check(st=St(hunger="Weak")), [])

    def test_novelty_class_once(self):
        b = self.check(classes={"d", "f"})
        self.assertEqual(sorted(x.reason for x in b),
                         ["novelty-class", "novelty-class"])
        self.assertEqual(len({x.eid for x in b}), 2)
        self.assertEqual(self.check(classes={"d"}), [])
        more = self.check(classes={"d", "f", "&"})
        self.assertEqual([x.eid for x in more], ["class:&"])

    def test_novelty_item_once(self):
        b = self.check(messages=["You see here a scroll.",
                                 "You see here a potion."])
        self.assertEqual(len(b), 2)   # two distinct descriptions
        kinds = [x.reason for x in b]
        self.assertEqual(kinds, ["novelty-item", "novelty-item"])
        self.assertEqual(self.check(messages=["You see here a scroll.",
                                              "You see here a potion."]), [])

    def test_inventory_baseline_then_change(self):
        self.assertEqual(self.check(inventory_sig=("a food ration",)), [])
        self.assertEqual(self.check(inventory_sig=("a food ration",)), [])
        b = self.check(inventory_sig=("a food ration", "a dagger"))
        self.assertEqual([x.reason for x in b], ["inventory-change"])

    def test_failed_food_buckets(self):
        self.assertEqual(self.check(failed_food=1), [])
        first = self.check(failed_food=2)
        self.assertEqual([x.eid for x in first], ["food-failed:1"])
        self.assertEqual(self.check(failed_food=3), [])
        second = self.check(failed_food=4)
        self.assertEqual([x.eid for x in second], ["food-failed:2"])

    def test_low_confidence_buckets(self):
        self.assertEqual(self.check(low_conf_streak=2,
                                    low_conf_threshold=3), [])
        b = self.check(low_conf_streak=3, low_conf_threshold=3)
        self.assertEqual([x.eid for x in b], ["low-confidence:1"])
        self.assertEqual(self.check(low_conf_streak=5,
                                    low_conf_threshold=3), [])
        b = self.check(low_conf_streak=6, low_conf_threshold=3)
        self.assertEqual([x.eid for x in b], ["low-confidence:2"])

    def test_closed_once(self):
        self.assertEqual([x.reason for x in self.check(closed=True)],
                         ["closed"])
        self.assertEqual(self.check(closed=True), [])


class St(object):
    def __init__(self, hp=None, hp_max=None, hunger="", dlvl="", time=None,
                 gold=None, level=None):
        self.hp = hp
        self.hp_max = hp_max
        self.hunger = hunger
        self.dlvl = dlvl
        self.time = time
        self.gold = gold
        self.level = level


# ============================================================ queue

class TestBoundaryQueue(unittest.TestCase):
    def setUp(self):
        self.led = budget.BudgetLedger()
        self.q = events.BoundaryQueue(cooldown_ticks=50, cooldown_wall=5.0,
                                      emergency_wall=2.0, ledger=self.led)

    def b(self, reason, eid, severe=False):
        return events.Boundary(reason, eid, severe)

    def test_coalescing_one_pending_set(self):
        self.q.submit([self.b("hp-crisis", "hp-crisis:1", True),
                       self.b("hunger-weak", "hunger:Weak", True)], 1)
        pending = self.q.ready(1, 100.0)
        self.assertIsNotNone(pending)
        self.assertEqual(sorted(pending.eids),
                         ["hp-crisis:1", "hunger:Weak"])
        self.assertTrue(pending.severe)
        self.assertEqual(self.led.boundaries_queued, 2)

    def test_normal_cooldown_needs_ticks_and_wall(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.assertIsNotNone(self.q.ready(0, 100.0))
        self.q.mark_dispatched(0, 100.0)
        self.q.finish(True)
        self.q.submit([self.b("level-change", "level:2:1")], 10)
        # only 10 ticks elapsed: still on cooldown
        self.assertIsNone(self.q.ready(10, 106.0))
        # enough ticks but not enough wall time
        self.assertIsNone(self.q.ready(60, 101.0))
        self.assertIsNotNone(self.q.ready(60, 106.0))

    def test_severe_bypasses_normal_cooldown(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.mark_dispatched(0, 100.0)
        self.q.finish(True)
        self.q.submit([self.b("hp-crisis", "hp-crisis:1", True)], 1)
        # emergency cooldown still applies
        self.assertIsNone(self.q.ready(1, 100.5))
        self.assertIsNotNone(self.q.ready(1, 102.5))

    def test_suppress_and_expire_states(self):
        self.q.submit([self.b("hunger-hungry", "hunger:Hungry")], 0)
        self.assertEqual(self.q.suppress("cap"), ["hunger:Hungry"])
        self.assertEqual(self.led.boundaries_suppressed, 1)
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.expire("level-changed")
        self.assertEqual(self.led.boundaries_expired, 1)
        states = self.q.states()
        self.assertIn("suppressed", states)
        self.assertIn("expired", states)

    def test_only_one_call_in_flight(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.mark_dispatched(0, 0.0)
        self.q.submit([self.b("hunger-hungry", "hunger:Hungry")], 1)
        self.assertIsNone(self.q.ready(100, 100.0))   # in flight

    def test_applied_and_expired_accounting(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.mark_dispatched(0, 0.0)
        self.q.finish(True)
        self.assertEqual(self.led.boundaries_applied, 1)
        self.q.submit([self.b("hunger-hungry", "hunger:Hungry")], 1)
        self.q.mark_dispatched(1, 1.0)
        self.q.finish(False)
        self.assertEqual(self.led.boundaries_expired, 1)


# ============================================================ budget

class TestBudgetLedger(unittest.TestCase):
    def test_postmortem_reserve_limits_play_to_seven(self):
        led = budget.BudgetLedger(strategy_cap=8, postmortem_reserve=1)
        for _ in range(7):
            self.assertTrue(led.reserve_strategy())
            led.commit_strategy({})
        self.assertFalse(led.strategy_available())
        self.assertFalse(led.reserve_strategy())
        # the reserved slot is still spendable for the postmortem
        self.assertTrue(led.strategy_available(postmortem=True))
        self.assertTrue(led.reserve_strategy(postmortem=True))
        led.commit_strategy({}, postmortem=True)
        self.assertEqual(led.strategy_dispatched, 8)
        self.assertEqual(led.postmortem_dispatched, 1)
        self.assertFalse(led.strategy_available(postmortem=True))

    def test_timeout_is_billed(self):
        led = budget.BudgetLedger(strategy_cap=8, postmortem_reserve=1)
        led.reserve_strategy()
        led.commit_strategy(None)         # no usage returned
        self.assertEqual(led.strategy_dispatched, 1)
        self.assertEqual(led.strategy_reserved, 0)

    def test_release_drops_an_undelivered_reservation(self):
        led = budget.BudgetLedger()
        led.reserve_strategy()
        led.release_strategy()
        self.assertEqual(led.strategy_reserved, 0)
        self.assertEqual(led.strategy_dispatched, 0)

    def test_tariff_prices_reported_usage(self):
        led = budget.BudgetLedger(
            tariff=budget.Tariff(prompt_per_mtok=1.0,
                                 completion_per_mtok=2.0))
        led.add_usage({"prompt_tokens": 1000000, "completion_tokens": 500000,
                       "reported": True})
        self.assertAlmostEqual(led.estimated_usd, 1.0 + 1.0)

    def test_no_tariff_is_counted_as_unknown_price(self):
        led = budget.BudgetLedger()
        led.add_usage({"prompt_tokens": 10, "reported": True})
        self.assertEqual(led.estimated_usd, 0.0)
        self.assertEqual(led.unknown_price_calls, 1)

    def test_usd_cap_disables_dispatch(self):
        led = budget.BudgetLedger(
            strategy_cap=100, postmortem_reserve=0, usd_cap=0.5,
            tariff=budget.Tariff(1.0, 1.0))
        led.add_usage({"prompt_tokens": 1000000, "completion_tokens": 0,
                       "reported": True})
        self.assertFalse(led.strategy_available())

    def test_token_bound_is_refused_before_dispatch(self):
        # usage one unit below the cap: a request whose conservative bound
        # exceeds the remainder is refused *before* anything is charged
        led = budget.BudgetLedger(strategy_cap=10, postmortem_reserve=0,
                                  token_cap=100)
        led.add_usage({"prompt_tokens": 99, "completion_tokens": 0,
                       "reported": True})
        self.assertFalse(led.reserve_strategy(prompt_tokens=5,
                                              completion_tokens=0))
        self.assertEqual(led.strategy_reserved, 0)
        self.assertTrue(led.reserve_strategy(prompt_tokens=1,
                                             completion_tokens=0))
        led.commit_strategy({})
        self.assertEqual(led.strategy_dispatched, 1)

    def test_usd_bound_is_refused_before_dispatch(self):
        led = budget.BudgetLedger(
            strategy_cap=10, postmortem_reserve=0, usd_cap=0.5,
            tariff=budget.Tariff(1.0, 1.0))
        led.add_usage({"prompt_tokens": 400000, "completion_tokens": 0,
                       "reported": True})            # $0.40 of the $0.50 cap
        # a $0.20 bound cannot be covered by the $0.10 remainder
        self.assertFalse(led.reserve_strategy(prompt_tokens=200000,
                                              completion_tokens=0))
        self.assertTrue(led.reserve_strategy(prompt_tokens=90000,
                                             completion_tokens=0))
        led.commit_strategy({})

    def test_timeout_keeps_its_exposure(self):
        led = budget.BudgetLedger(strategy_cap=10, postmortem_reserve=0,
                                  token_cap=100)
        self.assertTrue(led.reserve_strategy(prompt_tokens=60,
                                             completion_tokens=30))
        led.commit_strategy(None)                     # no usage returned
        self.assertEqual(led.strategy_dispatched, 1)
        self.assertEqual(led.strategy_reserved, 0)
        self.assertEqual(led.unknown_exposure_calls, 1)
        self.assertEqual(led.unknown_exposure_tokens, 90)
        # the exposure is still represented, so a further call cannot fit
        self.assertFalse(led.reserve_strategy(prompt_tokens=20,
                                              completion_tokens=0))

    def test_negative_postmortem_reserve_cannot_enlarge_play(self):
        led = budget.BudgetLedger(strategy_cap=4, postmortem_reserve=-1)
        self.assertEqual(led.postmortem_reserve, 0)
        for _ in range(4):
            self.assertTrue(led.reserve_strategy())
            led.commit_strategy({})
        self.assertFalse(led.strategy_available())

    def test_reflex_paid_bound_is_separate(self):
        led = budget.BudgetLedger(reflex_cap=2)
        self.assertTrue(led.reserve_reflex_paid())
        self.assertTrue(led.reflex_paid_available())
        led.reserve_reflex_paid()
        self.assertFalse(led.reserve_reflex_paid())

    def test_report_shape(self):
        led = budget.BudgetLedger()
        led.note_boundary("detected", 3)
        led.note_boundary("queued", 2)
        out = led.as_dict()
        self.assertEqual(out["boundaries"]["detected"], 3)
        self.assertEqual(out["boundaries"]["queued"], 2)
        self.assertIn("usage", out)


# ============================================================ worker module

class TestWorkerModule(unittest.TestCase):
    def test_url_allowlist(self):
        self.assertEqual(worker.validate_url("https://api.example.com/x"),
                         "https://api.example.com/x")
        self.assertEqual(worker.validate_url("http://127.0.0.1:9/x"),
                         "http://127.0.0.1:9/x")
        for bad in ("http://api.example.com/x",
                    "ftp://api.example.com/x",
                    "https://user:pw@api.example.com/x",
                    "https://api.example.com/x?k=v",
                    ""):
            with self.subTest(url=bad):
                with self.assertRaises(worker.UrlError):
                    worker.validate_url(bad)

    def test_run_line_on_garbage(self):
        out = json.loads(worker.run_line("}{ not json"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad-job")

    def test_main_requires_the_private_marker(self):
        with mock.patch("sys.stdin"), mock.patch("sys.stdout") as out, \
                mock.patch("sys.stderr"):
            rc = worker.main([])
            self.assertEqual(rc, 2)
            self.assertFalse(out.write.called)

    def test_bad_url_reported_without_raising(self):
        out = json.loads(worker.run_line(json.dumps(
            {"url": "http://evil.example.com/", "payload": {}})))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad-url")


# ============================================================ supervisor

class TestWorkerSupervisor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sup-test.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _script(self, body):
        path = os.path.join(self.dir, "fake_worker.py")
        with open(path, "w") as fh:
            fh.write(body)
        return [sys.executable, path]

    def test_hanging_worker_is_killed_and_reaped(self):
        argv = self._script("import time\ntime.sleep(30)\n")
        sup = providers._WorkerSupervisor(argv)
        t0 = time.monotonic()
        sup.start({"v": 1}, time.monotonic() + 0.3)
        self.assertTrue(sup.wait(2.0))
        res = sup.poll()
        self.assertIsNotNone(res)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.error, "timeout")
        self.assertLess(time.monotonic() - t0, 3.0)
        # the process is reaped, not left running
        self.assertIsNotNone(sup.proc.poll())
        sup.reap()

    def test_drip_worker_times_out(self):
        argv = self._script(
            "import sys, time\n"
            "sys.stdout.write('{\"v\":1,')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n")
        sup = providers._WorkerSupervisor(argv)
        sup.start({"v": 1}, time.monotonic() + 0.3)
        self.assertTrue(sup.wait(2.0))
        res = sup.poll()
        self.assertTrue(res.timed_out)
        sup.reap()

    def test_worker_result_is_parsed(self):
        argv = self._script(
            "import sys\n"
            "sys.stdout.write('{\"v\":1,\"ok\":true,\"status\":200,"
            "\"json\":{\"a\":1}}\\n')\n")
        sup = providers._WorkerSupervisor(argv)
        sup.start({"v": 1}, time.monotonic() + 2.0)
        self.assertTrue(sup.wait(3.0))
        res = sup.poll()
        self.assertTrue(res.ok)
        self.assertEqual(res.json, {"a": 1})
        sup.reap()

    def test_worker_env_is_minimal_and_secret_free(self):
        with mock.patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "sentinel-ds",
                "JEV_API_KEY": "sentinel-jev",
                "OPENAI_API_KEY": "sentinel-oa",
                "HOME": "/home/tester"}, clear=False):
            env = providers.worker_env()
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("JEV_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["HOME"], "/home/tester")
        self.assertIn("PATH", env)

    def test_worker_process_does_not_inherit_credentials(self):
        dump = os.path.join(self.dir, "worker-env.json")
        argv = self._script(
            "import json, os, sys\n"
            "json.dump(dict(os.environ), open(%r, 'w'))\n"
            "sys.stdout.write('{\"v\":1,\"ok\":true,\"json\":{}}\\n')\n"
            % dump)
        with mock.patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "sentinel-ds",
                "JEV_API_KEY": "sentinel-jev"}, clear=False):
            sup = providers._WorkerSupervisor(argv)
            sup.start({"v": 1, "api_key": "unused-here"},
                      time.monotonic() + 3.0)
            self.assertTrue(sup.wait(4.0))
            sup.poll()
            sup.reap()
        with open(dump) as fh:
            child = json.load(fh)
        self.assertNotIn("DEEPSEEK_API_KEY", child)
        self.assertNotIn("JEV_API_KEY", child)
        self.assertIn("PATH", child)

    def test_job_key_travels_on_stdin_not_argv(self):
        argv = self._script(
            "import json, sys\n"
            "job = json.loads(sys.stdin.readline())\n"
            "sys.stdout.write(json.dumps({'v': 1, 'ok': True,\n"
            "  'json': {'seen': job.get('api_key'),\n"
            "            'argv': list(sys.argv)}}) + '\\n')\n")
        sup = providers._WorkerSupervisor(argv)
        sup.start({"v": 1, "api_key": "sk-stdin-secret"},
                  time.monotonic() + 3.0)
        self.assertTrue(sup.wait(4.0))
        res = sup.poll()
        sup.reap()
        self.assertTrue(res.ok)
        self.assertEqual(res.json["seen"], "sk-stdin-secret")
        self.assertNotIn("sk-stdin-secret", " ".join(res.json["argv"]))

    def test_group_is_killed_after_the_leader_exits_on_term(self):
        # Low 5: a worker that forks a TERM-ignoring child and then exits on
        # TERM leaves no leader to resolve the group from; escalation must
        # assess the *group* and KILL the survivor.
        pidfile = os.path.join(self.dir, "worker-child.pid")
        argv = self._script(
            "import os, signal, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    while True:\n"
            "        time.sleep(0.5)\n"
            "open(%r, 'w').write(str(pid))\n"
            "while True:\n"
            "    time.sleep(0.5)\n" % pidfile)
        sup = providers._WorkerSupervisor(argv, grace=0.3)
        sup.start({"v": 1}, time.monotonic() + 0.5)
        child = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and child is None:
            if os.path.exists(pidfile):
                with open(pidfile) as fh:
                    child = int(fh.read().strip())
            else:
                time.sleep(0.05)
        self.assertIsNotNone(child, "the worker never forked a child")
        self.assertTrue(sup.wait(5.0))
        sup.poll()
        sup.reap()
        self.assertTrue(_wait_gone(child, 3.0),
                        "the TERM-ignoring descendant survived teardown")
        self.assertTrue(_wait_gone(sup.proc.pid, 3.0))


# ============================================================ DeepSeek

class TestDeepSeekWorkerSupervised(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds-test.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)
        self.env = mock.patch.dict(os.environ,
                                   {"DEEPSEEK_API_KEY": "sk-test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cfg(self, **over):
        base = dict(strategy="deepseek", deepseek_base_url=self.ep.base_url,
                    strategy_deadline=3.0, deepseek_max_bytes=1024)
        base.update(over)
        return ProviderConfig(**base)

    def ctx(self):
        return StrategyContext(episode=1, tick=5, status_text="HP 10/20",
                               level="Dlvl:1")

    def test_valid_directives_round_trip(self):
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertTrue(res.ok)
        self.assertEqual(res.directives[0].goals, ("survive",))
        self.assertEqual(res.usage.get("prompt_tokens"), 10)
        self.assertEqual(len(self.ep.requests), 1)
        # the *real* key reaches the wire: Secret redacts only its repr
        self.assertEqual(self.ep.requests[0]["auth"],
                         "Bearer sk-test-secret")
        prov.reap()

    def test_http_401_discards_the_plan(self):
        self.ep.responder = lambda p, b: (401, b'{"error":"nope"}')
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("http-401", res.reason)
        prov.reap()

    def test_http_429_and_5xx_apply_cooldown(self):
        for status, tag in ((429, "http-429"), (503, "http-5xx")):
            with self.subTest(status=status):
                self.ep.responder = lambda p, b, s=status: (s, b"{}")
                prov = providers.DeepSeekStrategy(self.cfg())
                res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
                self.assertIn(tag, res.reason)
                self.assertGreater(prov.cooldown_until, prov.now())
                prov.reap()

    def test_malformed_json(self):
        self.ep.responder = lambda p, b: (200, b"this is not json")
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("malformed-json", res.reason)
        prov.reap()

    def test_oversized_response(self):
        big = json.dumps({"choices": [{"message": {"content": "x" * 5000}}]})
        self.ep.responder = lambda p, b: (200, big.encode())
        prov = providers.DeepSeekStrategy(self.cfg(deepseek_max_bytes=256))
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("oversized", res.reason)
        prov.reap()

    def test_invalid_directive_json_is_discarded(self):
        self.ep.responder = lambda p, b: (
            200, _chat_body({"goals": ["teleport"], "key": 7}))
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("invalid-directives", res.reason)
        prov.reap()

    def test_hanging_endpoint_hits_the_deadline_and_is_killed(self):
        self.ep.hang = 30.0
        prov = providers.DeepSeekStrategy(self.cfg(strategy_deadline=0.6))
        t0 = time.monotonic()
        res = prov.deliberate(self.ctx(), time.monotonic() + 0.6)
        elapsed = time.monotonic() - t0
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "timeout")
        self.assertLess(elapsed, 3.0)
        self.assertIsNone(prov._sup)      # reaped
        prov.reap()

    def test_slow_but_within_deadline_succeeds(self):
        self.ep.delay = 0.3
        prov = providers.DeepSeekStrategy(self.cfg(strategy_deadline=3.0))
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertTrue(res.ok)
        prov.reap()

    def test_no_key_is_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            prov = providers.DeepSeekStrategy(
                ProviderConfig(strategy="deepseek"))
            self.assertFalse(prov.available(prov.config).enabled)

    def test_key_file_must_be_0600(self):
        path = os.path.join(self.dir, "key")
        with open(path, "w") as fh:
            fh.write("sk-file-secret\n")
        os.chmod(path, 0o644)
        with self.assertRaises(providers.SecretError):
            providers.load_secret(path, "DEEPSEEK_API_KEY")
        os.chmod(path, 0o600)
        secret = providers.load_secret(path, "DEEPSEEK_API_KEY")
        self.assertEqual(str(secret), "<redacted>")
        self.assertEqual(repr(secret), "<redacted>")

    def test_secret_repr_is_redacted(self):
        secret = providers.Secret("sk-abc123")
        self.assertNotIn("sk-abc123", repr(secret))
        self.assertNotIn("sk-abc123", str(secret))

    def test_chat_url_is_joined_once(self):
        self.assertEqual(
            providers._chat_url("https://api.deepseek.com"),
            "https://api.deepseek.com/chat/completions")
        self.assertEqual(
            providers._chat_url("https://api.deepseek.com/chat/completions"),
            "https://api.deepseek.com/chat/completions")

    def test_payload_carries_the_model_and_untrusted_state(self):
        payload = providers.deepseek_payload("m1", self.ctx(), 123)
        self.assertEqual(payload["model"], "m1")
        self.assertEqual(payload["max_tokens"], 123)
        self.assertFalse(payload["stream"])
        joined = json.dumps(payload)
        self.assertIn("untrusted", joined.lower())


# ============================================================ Jev

class TestJevAdapter(unittest.TestCase):
    def setUp(self):
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)
        self.env = mock.patch.dict(os.environ,
                                   {"JEV_API_KEY": "jev-test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cfg(self, **over):
        base = dict(reflex="jev", jev_base_url=self.ep.base_url,
                    jev_accept_terms=True, reflex_deadline=2.0,
                    confidence_threshold=0.8)
        base.update(over)
        return ProviderConfig(**base)

    def ctx(self, need):
        return ReflexContext(episode=1, tick=1, need=need,
                             need_key=protocol.NeedKey(1, 1, need.get("id")),
                             snapshot=protocol.Snapshot(), pages=[],
                             memory=state.EpisodeMemory())

    def test_ships_disabled_without_terms(self):
        prov = providers.JevReflex(ProviderConfig(reflex="jev"))
        self.assertFalse(prov.available(prov.config).enabled)
        self.assertIn("terms", prov.available(prov.config).reason)

    def test_disabled_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            prov = providers.JevReflex(self.cfg())
            self.assertFalse(prov.available(prov.config).enabled)

    def test_disabled_without_endpoint(self):
        prov = providers.JevReflex(self.cfg(jev_base_url=None))
        self.assertFalse(prov.available(prov.config).enabled)

    def test_enabled_only_with_terms_key_and_endpoint(self):
        prov = providers.JevReflex(self.cfg(reflex_call_cap=1))
        self.assertTrue(prov.available(prov.config).enabled)

    def test_valid_choice(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 1, "confidence": 0.9}).encode())
        prov = providers.JevReflex(self.cfg())
        res = prov.decide(self.ctx(command_need(1)), time.monotonic() + 2.0)
        self.assertIsNotNone(res)
        self.assertEqual(res.confidence, 0.9)
        self.assertEqual(res.action,
                         {"key": providers.KEY_CHOICES[1][1]})
        prov.cancel()

    def test_confidence_nan_and_out_of_range_fall_back(self):
        # a rejected answer still carries its usage: the call was paid
        for conf in (float("nan"), 1.5, -0.1, "high", None):
            with self.subTest(conf=conf):
                self.ep.responder = lambda p, b, c=conf: (
                    200, json.dumps({"option": 0, "confidence": c,
                                     "usage": {"prompt_tokens": 7,
                                               "completion_tokens": 2}})
                    .encode())
                prov = providers.JevReflex(self.cfg())
                res = prov.decide(self.ctx(command_need(1)),
                                  time.monotonic() + 2.0)
                self.assertIsNotNone(res)
                self.assertIsNone(res.action)
                self.assertIn(res.reason,
                              ("invalid-confidence", "low-confidence"))
                self.assertEqual(res.usage, {"prompt_tokens": 7,
                                             "completion_tokens": 2})
                prov.cancel()

    def test_low_confidence_falls_back(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 0, "confidence": 0.2,
                             "usage": {"prompt_tokens": 3}}).encode())
        prov = providers.JevReflex(self.cfg())
        res = prov.decide(self.ctx(command_need(1)),
                          time.monotonic() + 2.0)
        self.assertIsNotNone(res)
        self.assertIsNone(res.action)
        self.assertEqual(res.reason, "low-confidence")
        self.assertEqual(res.usage.get("prompt_tokens"), 3)
        prov.cancel()

    def test_unknown_option_falls_back(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 999, "confidence": 0.99,
                             "usage": {"prompt_tokens": 4}}).encode())
        prov = providers.JevReflex(self.cfg())
        res = prov.decide(self.ctx(command_need(1)),
                          time.monotonic() + 2.0)
        self.assertIsNotNone(res)
        self.assertIsNone(res.action)
        self.assertEqual(res.reason, "invalid-option")
        self.assertEqual(res.usage.get("prompt_tokens"), 4)
        prov.cancel()

    def test_abstain_carries_usage(self):
        # a paid abstention is still a paid call: its usage is preserved
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": None, "confidence": 0.99,
                             "usage": {"prompt_tokens": 5,
                                       "completion_tokens": 1}}).encode())
        prov = providers.JevReflex(self.cfg())
        res = prov.decide(self.ctx(command_need(1)),
                          time.monotonic() + 2.0)
        self.assertIsNotNone(res)
        self.assertIsNone(res.action)
        self.assertEqual(res.reason, "abstain")
        self.assertEqual(res.usage, {"prompt_tokens": 5,
                                     "completion_tokens": 1})
        prov.cancel()

    def test_unsupported_needs_never_call_jev(self):
        prov = providers.JevReflex(self.cfg())
        for kind in ("line", "extcmd", "position"):
            need = {"id": 1, "kind": kind, "prompt": ""}
            if kind == "position":
                need.update({"x0": 1, "y0": 0, "x1": 2, "y1": 1})
            with self.subTest(kind=kind):
                self.assertIsNone(prov.decide(self.ctx(need),
                                              time.monotonic() + 2.0))
        self.assertEqual(self.ep.requests, [])
        prov.cancel()

    def test_oversized_menu_is_scripted(self):
        prov = providers.JevReflex(self.cfg())
        need = {"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
                "content": "c1", "pages": 1}
        ctx = self.ctx(need)
        ctx.pages = [{"r": i, "text": "row", "selectable": True}
                     for i in range(200)]
        self.assertIsNone(prov.decide(ctx, time.monotonic() + 2.0))
        self.assertEqual(self.ep.requests, [])
        prov.cancel()

    def test_small_menu_choice_maps_to_a_row(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 2, "confidence": 0.95}).encode())
        prov = providers.JevReflex(self.cfg())
        need = {"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
                "content": "c1", "pages": 1}
        ctx = self.ctx(need)
        ctx.pages = [{"r": 10 + i, "text": "row %d" % i, "selectable": True}
                     for i in range(4)]
        res = prov.decide(ctx, time.monotonic() + 2.0)
        self.assertEqual(res.action, {"menu": "m1", "commit": [[12, -1]]})
        prov.cancel()


# ============================================================ integration

class TestStrategyIntegration(WireHarness):
    """End-to-end controller behaviour with a local fake strategy tier."""

    def _controller_with(self, fake, config=None, **kw):
        config = config or ProviderConfig(max_ticks=200)
        ctl = controller.Controller(
            config, controller.ControllerPaths(
                worker="w", runner="r", data="d", sysconf="s"),
            self.dir, episode_timeout=kw.pop("timeout", 10.0))
        ctl._new_strategy_provider = lambda: fake
        return ctl

    def test_boundary_counts_per_kind_and_a_single_call(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)))
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(3, command_need(3), dlvl="Dlvl:2", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertTrue(result.closed)
        # the strategy is not polled per command: 3 commands, 1 boundary call
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(result.strategy_calls, 1)
        # boundaries: initial-level + level-change detected (plus closed)
        self.assertGreaterEqual(result.boundaries, 2)

    def test_postmortem_uses_the_reserved_slot(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, strategy_call_cap=2,
                             postmortem_reserve=1, low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:2", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        # one play call plus exactly one postmortem
        budget = result.budget["strategy"]
        self.assertEqual(budget["postmortem_dispatched"], 1)
        self.assertEqual(result.strategy_calls, 2)
        self.assertTrue(fake.calls[-1].postmortem)

    def test_repeated_snapshots_produce_no_new_boundaries(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        same = dict(dlvl="Dlvl:1", hp=10, hp_max=20)
        records = [hello()]
        for seq in range(1, 6):
            records.append(st_obs(seq, command_need(seq), **same))
        records.append(CLOSED)
        proc = paced(records, [0.05] * len(records))
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        # five identical snapshots -> one initial-level event, plus the one
        # closed event; nothing else
        self.assertEqual(result.boundaries, 2)

    def test_strategy_never_emits_a_wire_action(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)))
        ctl = self._controller_with(fake)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:1", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        acts = [a for a in _parse_actions(proc.stdin.data)
                if a.get("type") == "act"]
        proc.close()
        self.assertEqual(len(acts), 2)         # one per need, no extras
        for a in acts:
            self.assertIsNone(protocol.validate_action(
                {"kind": "command", "id": a["id"]}, a["action"]))
        # the directive set carries goals only -- no keys, no text
        for call in fake.calls:
            self.assertNotIn("key", json.dumps(call.summary))

    def test_stale_level_advice_is_discarded(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)))
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [
            hello(),
            st_obs(1, None, dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:2", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.6, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(len(fake.calls), 1)
        # the level changed between dispatch and the command boundary
        self.assertEqual(result.directives_applied, 0)
        self.assertGreaterEqual(
            result.budget["boundaries"]["expired"], 1)

    def test_cap_suppresses_further_dispatch(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, strategy_call_cap=1,
                             postmortem_reserve=0, boundary_cooldown_ticks=0,
                             boundary_cooldown_wall=0.0)
        ctl = self._controller_with(fake, config=cfg)
        records = [hello()]
        for seq in range(1, 6):
            records.append(st_obs(seq, command_need(seq),
                                  dlvl="Dlvl:%d" % seq, hp=10, hp_max=20))
        records.append(CLOSED)
        proc = paced(records, [0.05] + [0.35] * len(records))
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(result.strategy_calls, 1)
        self.assertGreaterEqual(
            result.budget["boundaries"]["suppressed"], 1)

    def test_repeated_identical_snapshot_has_no_boundary(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [hello(), st_obs(1, command_need(1), dlvl="Dlvl:1"),
                   st_obs(2, command_need(2), dlvl="Dlvl:1"),
                   CLOSED]
        proc = paced(records, [0.05, 0.2, 0.2, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(len(fake.calls), 1)

    def test_recorder_failure_disables_paid_dispatch(self):
        class _FailRec(object):
            failed = True
            wire_bytes = 0

            def record_wire(self, line):
                pass

            def record_action(self, *a, **k):
                pass

            def record_decision(self, *a, **k):
                pass

            def record_event(self, obj):
                pass

        fake = FakeStrategy(_ok_directives())
        ctl = self._controller_with(fake)
        result = controller.EpisodeResult(index=1)
        proc = paced([hello()], [0.0])
        runner = controller._EpisodeRunner(ctl, proc, _FailRec(), result)
        proc.close()
        self.assertTrue(runner.rec_healthy)
        runner._note_recorder_health()
        self.assertFalse(runner.rec_healthy)
        self.assertTrue(runner.paid_disabled)
        self.assertFalse(runner._strategy_live())
        self.assertEqual(fake.cancelled, 1)

    def test_default_config_makes_no_calls(self):
        # NullStrategy: the shipped default strategy tier is a no-op
        ctl = controller.Controller(
            ProviderConfig(strategy="off"),
            controller.ControllerPaths(worker="w", runner="r", data="d",
                                       sysconf="s"),
            self.dir, episode_timeout=5.0)
        records = [hello(), st_obs(1, command_need(1), dlvl="Dlvl:1"),
                   CLOSED]
        proc = paced(records, [0.05, 0.1, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(result.strategy_calls, 0)


class TestReflexConsultsDirectives(unittest.TestCase):
    def ctx(self, goals=None, tick=5):
        ctx_dirs = []
        if goals:
            dset, why = DSEV.validate_directive_set({"goals": list(goals)})
            self.assertEqual(why, "")
            ctx_dirs = [DSEV.DirectiveView(dset, 1)]
        mem = state.EpisodeMemory()
        return ReflexContext(
            episode=1, tick=tick, need={"id": 1, "kind": "command"},
            need_key=protocol.NeedKey(1, tick, 1),
            snapshot=protocol.Snapshot(), pages=[], memory=mem,
            directives=ctx_dirs)

    def test_disengage_raises_the_flee_threshold(self):
        reflex = policy.ScriptedReflex(
            providers.ProviderConfig(max_ticks=200))
        mem = state.EpisodeMemory()
        mem.status.hp, mem.status.hp_max = 40, 100   # 40%: not normally low
        self.assertFalse(reflex._low_hp(mem.status))
        ctx = self.ctx(["disengage"])
        ctx.memory = mem
        reflex.decide(ctx)
        self.assertTrue(reflex._low_hp(mem.status))

    def test_acquire_food_inspects_the_inventory(self):
        reflex = policy.ScriptedReflex(providers.ProviderConfig())
        mem = state.EpisodeMemory()
        mem.hero = (3, 10)
        mem.grid[(3, 10)] = ("@", "", 0, "")
        ctx = self.ctx(["acquire_food"], tick=1000)
        ctx.memory = mem
        res = reflex.decide(ctx)
        self.assertEqual(res.action, {"key": protocol.KEY_INV})
        self.assertIn("acquire_food", res.reason)

    def test_no_directive_keeps_the_default(self):
        reflex = policy.ScriptedReflex(providers.ProviderConfig())
        ctx = self.ctx(None)
        res = reflex.decide(ctx)
        self.assertIsNotNone(res.action)
        self.assertFalse(reflex.directives.active)

    def test_directive_never_becomes_a_key(self):
        reflex = policy.ScriptedReflex(providers.ProviderConfig())
        mem = state.EpisodeMemory()
        mem.hero = (3, 10)
        mem.grid[(3, 10)] = ("@", "", 0, "")
        ctx = self.ctx(["explore_frontier"])
        ctx.memory = mem
        res = reflex.decide(ctx)
        # whatever it returns must be a legal action for the need
        self.assertIsNone(protocol.validate_action(ctx.need, res.action))


class TestSecretsInArtifacts(WireHarness):
    def test_no_key_material_in_recordings(self):
        ep = FakeEndpoint(lambda p, b: (200, _chat_body(_ok_directives())))
        self.addCleanup(ep.close)
        secret = "sk-live-DO-NOT-LEAK-0123456789"
        config = ProviderConfig(strategy="deepseek",
                                deepseek_base_url=ep.base_url,
                                strategy_deadline=3.0,
                                postmortem_reserve=0,
                                low_confidence_needs=1000)
        ctl = controller.Controller(
            config, controller.ControllerPaths(worker="w", runner="r",
                                               data="d", sysconf="s"),
            self.dir, episode_timeout=8.0)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:1", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.8, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(result.strategy_calls, 1)
        leaks = []
        for name in os.listdir(self.dir):
            if name.startswith("ep-"):
                with open(os.path.join(self.dir, name), "rb") as fh:
                    data = fh.read()
                if b"sk-" in data:
                    leaks.append(name)
        self.assertEqual(leaks, [])


# ============================================================ settlement

class TestStrategySettlement(WireHarness):
    """Medium 1: exactly-once settlement of a started strategy operation."""

    def _runner(self, fake, config=None):
        cfg = config or ProviderConfig(max_ticks=200, postmortem_reserve=0,
                                       low_confidence_needs=1000)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        ctl._new_strategy_provider = lambda: fake
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        return runner, rec, result

    def _dispatch(self, runner):
        b = events.Boundary("initial-level", "level:Dlvl:1:1")
        runner.event_ledger.detect(b, 0, "Dlvl:1")
        runner.boundary_queue.submit([b], 0, "Dlvl:1")
        runner._dispatch_strategy(
            runner.boundary_queue.ready(0, time.monotonic()),
            time.monotonic())
        return b

    def _end(self, ending, runner):
        if ending == "closed":
            runner.closed = True
        elif ending == "protocol-failure":
            runner.result.protocol_failure = "boom"
        elif ending == "episode-timeout":
            runner.result.timed_out = True
        elif ending == "content-deadline":
            runner.result.failure_reason = "content deadline expired"
        elif ending == "recorder-failure":
            runner.rec._decs.error = "disk full"
            runner._note_recorder_health()

    def test_every_ending_settles_the_call_exactly_once(self):
        for ending in ("closed", "protocol-failure", "episode-timeout",
                       "content-deadline", "recorder-failure"):
            with self.subTest(ending=ending):
                fake = FakeStrategy(_ok_directives())
                runner, rec, result = self._runner(fake)
                eid = self._dispatch(runner).eid
                self.assertEqual(runner.ledger.strategy_reserved, 1)
                self._end(ending, runner)
                runner._cancel_strategy()
                runner._maybe_postmortem()
                runner._finish()
                rec.finalize({})
                self.assertEqual(result.budget["strategy"]["reserved"], 0)
                self.assertEqual(result.budget["strategy"]["dispatched"], 1)
                # the completed result is preserved, never released
                self.assertEqual(result.budget["usage"]["prompt_tokens"], 5)
                recs = [r for r in runner.event_ledger.as_list()
                        if r["eid"] == eid]
                self.assertEqual(len(recs), 1)
                self.assertIsNotNone(recs[0]["terminal"])
                # a later settlement attempt is a guarded no-op
                runner._cancel_strategy()
                self.assertEqual(runner.ledger.strategy_dispatched, 1)
                self.assertEqual(runner.ledger.strategy_reserved, 0)

    def test_blocking_call_is_committed_with_unknown_usage(self):
        gate = threading.Event()
        fake = FakeStrategy(_ok_directives(), gate=gate)
        runner, rec, result = self._runner(fake)
        eid = self._dispatch(runner).eid
        self.assertEqual(runner.ledger.strategy_reserved, 1)
        t0 = time.monotonic()
        runner._cancel_strategy()
        self.assertLess(time.monotonic() - t0, 3.0)
        gate.set()                       # release the abandoned worker thread
        runner._finish()
        rec.finalize({})
        self.assertEqual(result.budget["strategy"]["reserved"], 0)
        self.assertEqual(result.budget["strategy"]["dispatched"], 1)
        # no usage was returned, so the exposure is carried, not dropped
        self.assertEqual(result.budget["usage"]["unknown_exposure_calls"], 1)
        self.assertEqual(result.budget["usage"]["prompt_tokens"], 0)
        recs = [r for r in runner.event_ledger.as_list() if r["eid"] == eid]
        self.assertEqual(recs[0]["terminal"]["state"], "expired")


# ============================================================ provenance

class TestRecordingProvenance(WireHarness):
    """Medium 4: decisions, directive sets and the event ledger are exact."""

    def _controller_with(self, fake, config=None):
        config = config or ProviderConfig(max_ticks=200, postmortem_reserve=0,
                                          low_confidence_needs=1000)
        ctl = controller.Controller(
            config, controller.ControllerPaths(worker="w", runner="r",
                                               data="d", sysconf="s"),
            self.dir, episode_timeout=10.0)
        ctl._new_strategy_provider = lambda: fake
        return ctl

    def _decisions(self):
        return _read_jsonl(os.path.join(self.dir, "ep-1.decisions.jsonl"))

    def _events(self):
        return _read_jsonl(os.path.join(self.dir, "ep-1.events.jsonl"))

    def test_decision_records_only_the_dispatched_eids(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)),
                            delay=0.25)
        ctl = self._controller_with(fake)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:2", hp=10, hp_max=20),
            st_obs(3, command_need(3), dlvl="Dlvl:2", hp=10, hp_max=20,
                   msg=("You see here a gold piece.",)),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.05, 0.6, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        # A was dispatched for the initial level; B (the level change) and C
        # (the novelty item) coalesced into the pending set while A ran
        strat = [d for d in self._decisions()
                 if d["provider"] == "strategy"
                 and d["reason"].startswith("strategy")]
        self.assertEqual(len(strat), 1)
        self.assertEqual(strat[0]["boundaries"], ["level:Dlvl:1:1"])
        # ... and the coalesced set terminates in exactly one state
        ledger = [r for r in self._events() if r.get("record") == "boundary"]
        queued = [r for r in ledger if r["queued"] is not None]
        self.assertTrue(queued)
        for rec in queued:
            self.assertIsNotNone(rec["terminal"], rec["eid"])
        # B was never dispatched: one strategy call, and only A has one
        self.assertEqual(result.strategy_calls, 1)
        self.assertTrue(all(r["dispatched"] is None
                            for r in ledger if r["eid"] != "level:Dlvl:1:1"))

    def test_directive_set_round_trips_through_the_decision(self):
        payload = {"schema_version": 1, "goals": ["survive", "acquire_food"],
                   "target": [10, 5], "risk": 0.25, "ttl": 30,
                   "preconditions": ["hero_known"],
                   "explanation": "eat then run"}
        fake = FakeStrategy(payload)
        ctl = self._controller_with(fake)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:1", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(result.directives_applied, 1)
        applied = [d for d in self._decisions() if d["directives"]]
        self.assertTrue(applied)
        rec = applied[-1]["directives"][0]
        self.assertEqual(rec["goals"], ["survive", "acquire_food"])
        self.assertEqual(rec["target"], [10, 5])
        self.assertEqual(rec["risk"], 0.25)
        self.assertEqual(rec["ttl"], 30)
        self.assertEqual(rec["preconditions"], ["hero_known"])
        self.assertEqual(rec["explanation"], "eat then run")
        dset, why = DSEV.validate_directive_set(rec)
        self.assertEqual(why, "")
        self.assertEqual(dset.goals, ("survive", "acquire_food"))
        # the directive lifecycle is ledgered too
        directives = [r for r in self._events()
                      if r.get("record") == "directive"]
        self.assertTrue(any(r["state"] == "applied" for r in directives))

    def test_event_ledger_has_exactly_one_terminal_per_queued_eid(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)))
        ctl = self._controller_with(fake)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:2", hp=10, hp_max=20),
            st_obs(3, command_need(3), dlvl="Dlvl:3", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.15, 0.15, 0.05])
        ctl._spawn = lambda priv: proc
        ctl.run_episode(1)
        proc.close()
        ledger = [r for r in self._events() if r.get("record") == "boundary"]
        self.assertTrue(ledger)
        for rec in ledger:
            self.assertEqual(rec["schema"], 1)
            if rec["queued"] is not None:
                self.assertIsNotNone(rec["terminal"], rec["eid"])
        closed = [r for r in ledger if r["eid"] == "closed"]
        self.assertEqual(len(closed), 1)
        # the closed boundary is *detected* only: it is never dispatched
        self.assertIsNone(closed[0]["terminal"])

    def test_postmortem_usage_reaches_decisions_and_totals(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, strategy_call_cap=4,
                             postmortem_reserve=1, low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:2", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        pm = [d for d in self._decisions()
              if d["reason"].startswith("postmortem")]
        self.assertEqual(len(pm), 1)
        self.assertEqual(pm[0]["usage"].get("prompt_tokens"), 5)
        # play call (5) + postmortem (5) both reach the episode totals
        self.assertEqual(result.budget["usage"]["prompt_tokens"], 10)


# ============================================================ postmortem

class TestPostmortemEligibility(WireHarness):
    """Medium 3: a postmortem requires a clean, validated closure."""

    def _run(self, records, gaps, config=None, eof=True, timeout=10.0):
        fake = FakeStrategy(_ok_directives())
        cfg = config or ProviderConfig(max_ticks=200, strategy_call_cap=4,
                                       postmortem_reserve=1,
                                       low_confidence_needs=1000)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=timeout)
        ctl._new_strategy_provider = lambda: fake
        proc = paced(records, gaps, eof=eof)
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        return fake, result

    def _postmortems(self, fake):
        return len([c for c in fake.calls if getattr(c, "postmortem", False)])

    def test_clean_closed_runs_exactly_one_postmortem(self):
        fake, result = self._run(
            [hello(),
             st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
             CLOSED],
            [0.05, 0.2, 0.05])
        self.assertTrue(result.closed)
        self.assertFalse(result.unanswered)
        self.assertEqual(self._postmortems(fake), 1)
        self.assertEqual(
            result.budget["strategy"]["postmortem_dispatched"], 1)

    def test_eof_runs_no_postmortem(self):
        fake, result = self._run(
            [hello(),
             st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20)],
            [0.05, 0.2], eof=True)
        self.assertFalse(result.closed)
        self.assertEqual(result.stop_reason, "transport-failure-eof")
        self.assertEqual(self._postmortems(fake), 0)

    def test_protocol_failure_runs_no_postmortem(self):
        bad = dict(CLOSED)
        bad["type"] = "not-a-record"
        fake, result = self._run([hello(), bad], [0.05, 0.05])
        self.assertEqual(result.stop_reason, "protocol-failure")
        self.assertEqual(self._postmortems(fake), 0)

    def test_episode_timeout_runs_no_postmortem(self):
        fake, result = self._run(
            [hello(),
             st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20)],
            [0.05, 0.05], eof=False, timeout=0.4)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.stop_reason, "episode-timeout")
        self.assertEqual(self._postmortems(fake), 0)

    def test_content_deadline_runs_no_postmortem(self):
        cfg = ProviderConfig(max_ticks=200, strategy_call_cap=4,
                             postmortem_reserve=1, content_deadline=0.3,
                             low_confidence_needs=1000)
        paged = {"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
                 "content": "c1", "pages": 2}
        fake, result = self._run(
            [hello(), st_obs(1, paged, dlvl="Dlvl:1", hp=10, hp_max=20)],
            [0.05, 0.05], config=cfg, eof=False, timeout=3.0)
        self.assertEqual(result.stop_reason, "content-deadline")
        self.assertEqual(self._postmortems(fake), 0)

    def test_closed_unanswered_runs_no_postmortem(self):
        paged = {"id": 1, "kind": "menu", "menu": "m9", "mode": "one",
                 "content": "c9", "pages": 2}
        fake, result = self._run(
            [hello(), st_obs(1, paged, dlvl="Dlvl:1", hp=10, hp_max=20),
             CLOSED],
            [0.05, 0.2, 0.05])
        self.assertTrue(result.closed)
        self.assertTrue(result.unanswered)
        self.assertEqual(result.stop_reason, "closed-unanswered")
        self.assertEqual(self._postmortems(fake), 0)


# ============================================================ low conf

class _FakeJev(object):
    """A typed-choice reflex double that never opens a socket."""

    name = "jev"
    version = "fake/1"
    last_error = ""

    def __init__(self, action=None, confidence=0.9, usage=None):
        self.action = action or {"key": protocol.KEY_SEARCH}
        self.confidence = confidence
        self.usage = usage or {}
        self.cancelled = 0

    def available(self, config):
        return Availability(True, "fake jev")

    def decide(self, ctx, deadline=0.0):
        return ReflexResult(action=self.action, confidence=self.confidence,
                            provider="jev", reason="fake", usage=self.usage)

    def fallback(self, ctx):
        return None

    def on_closed(self):
        pass

    def cancel(self):
        self.cancelled += 1


class TestLowConfidenceEscalation(WireHarness):
    """Low 6: escalation follows the FINAL selection outcome."""

    def _runner(self, decide=None, config=None):
        cfg = config or ProviderConfig(max_ticks=200, postmortem_reserve=0,
                                       low_confidence_needs=3)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        runner.pending_key = protocol.NeedKey(1, 1, 1)
        runner.pending_seq = 1
        runner.pending_need = {"kind": "command", "id": 1}
        if decide is not None:
            runner._decide = decide
        return runner, rec

    def _answer(self, runner, n=1):
        for _ in range(n):
            runner.pending_need = {"kind": "command", "id": 1}
            runner._answer_now(None)

    def test_ordinary_scripted_decisions_never_escalate(self):
        runner, rec = self._runner(
            decide=lambda need, dl: ({"key": protocol.KEY_WAIT}, "scripted",
                                     "ordinary", 0.0, {}, False))
        self._answer(runner, 3)
        rec.finalize({})
        self.assertEqual(runner.low_conf_streak, 0)
        runner._detect_boundaries()
        self.assertFalse(any(b.reason == "low-confidence"
                             for b in runner.detected_boundaries))

    def test_three_forced_fallbacks_escalate(self):
        runner, rec = self._runner()
        for _ in range(3):
            runner.force_fallback = True
            runner._answer_now(None)
        rec.finalize({})
        self.assertEqual(runner.low_conf_streak, 3)
        self.assertEqual(runner.ledger.reflex_low_confidence, 3)
        runner._detect_boundaries()
        self.assertEqual([b.reason for b in runner.detected_boundaries],
                         ["low-confidence"])

    def test_three_invalid_proposals_escalate(self):
        # a scripted proposal that then fails local validation is a fallback:
        # the streak must NOT have been reset by the discarded scripted score
        runner, rec = self._runner(
            decide=lambda need, dl: ({"bogus": 1}, "scripted", "r", 0.0,
                                     {}, False))
        self._answer(runner, 3)
        rec.finalize({})
        self.assertEqual(runner.low_conf_streak, 3)
        runner._detect_boundaries()
        self.assertEqual([b.reason for b in runner.detected_boundaries],
                         ["low-confidence"])

    def test_paid_usage_enters_the_budget(self):
        cfg = ProviderConfig(max_ticks=200, reflex="jev",
                             reflex_call_cap=5, postmortem_reserve=0,
                             deepseek_price_in=1.0, deepseek_price_out=1.0)
        usage = {"prompt_tokens": 1000000, "completion_tokens": 0,
                 "reported": True}
        fake = _FakeJev(usage=usage)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        ctl._new_reflex_provider = lambda reflex: fake
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        runner.pending_key = protocol.NeedKey(1, 1, 1)
        runner.pending_need = {"kind": "command", "id": 1}
        proposal, provider, reason, latency, jusage, low = runner._decide(
            runner.pending_need)
        rec.finalize({})
        self.assertEqual(provider, "jev")
        self.assertEqual(runner.ledger.prompt_tokens, 1000000)
        self.assertAlmostEqual(runner.ledger.estimated_usd, 1.0)
        self.assertEqual(runner.ledger.reflex_paid_dispatched, 1)


# ====================================================== token bound (M1)

class TestStrategyTokenBound(WireHarness):
    """Medium 1: the prompt bound counts UTF-8 bytes, not characters."""

    def _ctx(self, **over):
        base = dict(episode=1, tick=1, level="Dlvl:1",
                    status_text="HP 10/20", map_text="", recent_messages=[],
                    inventory=[])
        base.update(over)
        return StrategyContext(**base)

    def _rendered(self, ctx):
        return (providers._SYSTEM_PROMPT + "\n"
                + providers._render_strategy_prompt(ctx))

    def test_bound_is_the_byte_count_plus_framing(self):
        ctx = self._ctx(status_text="HP 10/20 饥饿 空腹 \U0001f600!!!")
        text = self._rendered(ctx)
        prompt, completion = providers.strategy_token_bound(
            ProviderConfig(), ctx)
        self.assertEqual(prompt, len(text.encode("utf-8"))
                         + providers._CHAT_FRAMING_TOKENS)
        self.assertEqual(completion, ProviderConfig().deepseek_max_tokens)

    def test_cjk_and_emoji_break_the_chars_over_four_estimate(self):
        ctx = self._ctx(
            status_text="HP 1/1" + "、" * 40,
            map_text="\n".join("界" * 79 for _ in range(21)),
            recent_messages=["You see a 金塊。"] * 6,
            inventory=["50 金貨 (gold piece)"])
        text = self._rendered(ctx)
        nbytes = len(text.encode("utf-8"))
        prompt, _ = providers.strategy_token_bound(ProviderConfig(), ctx)
        # tokens <= bytes for any byte-level tokenizer, so the bound covers
        # the pathological one-token-per-byte case too
        self.assertGreaterEqual(prompt, nbytes)
        # the old chars/4 estimate under-reserves for exactly this text
        chars_over_four = (len(text) + 3) // 4 + 16
        self.assertLess(chars_over_four, nbytes)

    def test_cjk_context_refuses_dispatch_under_a_tight_cap(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        ctl._new_strategy_provider = lambda: fake
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        runner.mem.status.dlvl = "Dlvl:1"
        runner.mem.messages = ["You see a 金塊。" * 40]
        b = events.Boundary("initial-level", "level:Dlvl:1:1")
        runner.event_ledger.detect(b, 0, "Dlvl:1")
        runner.boundary_queue.submit([b], 0, "Dlvl:1")
        pending = runner.boundary_queue.pending
        ctx = runner._build_strategy_context(pending)
        text = self._rendered(ctx)
        prompt, completion = providers.strategy_token_bound(
            runner.c.config, ctx)
        # the chars/4 estimate with the REAL framing constant: this is the
        # discriminator -- under a chars/4 bound the dispatch below fits the
        # cap; under the byte bound it must be refused
        old_prompt = len(text) // 4 + providers._CHAT_FRAMING_TOKENS
        self.assertLess(old_prompt, prompt)      # cjk inflates the bound
        # a cap the chars/4 estimate would have fitted inside...
        runner.ledger.token_cap = old_prompt + completion
        self.assertTrue(runner.ledger.strategy_available(
            prompt_tokens=old_prompt, completion_tokens=completion))
        runner._dispatch_strategy(pending, time.monotonic())
        # ...but the true byte bound cannot: refused before any work
        self.assertIsNone(runner._strategy_call)
        self.assertEqual(runner.ledger.boundaries_suppressed, 1)
        self.assertEqual(fake.calls, [])
        rec.finalize({})


# ================================================= config validation (M2)

class TestProviderConfigValidation(unittest.TestCase):
    """Medium 2: one validation authority shared by CLI and Controller."""

    def test_default_config_is_valid(self):
        self.assertIsNone(ProviderConfig().validate())

    def test_usd_cap_requires_a_complete_tariff(self):
        self.assertIn("complete tariff",
                      ProviderConfig(usd_cap=1.0).validate())
        self.assertIn("complete tariff", ProviderConfig(
            usd_cap=1.0, deepseek_price_in=1.0).validate())
        self.assertIsNone(ProviderConfig(
            usd_cap=1.0, deepseek_price_in=1.0,
            deepseek_price_out=2.0).validate())

    def test_non_finite_and_out_of_range_are_rejected(self):
        self.assertIn("finite", ProviderConfig(usd_cap=float("nan"))
                      .validate())
        self.assertIn("nonnegative", ProviderConfig(
            deepseek_price_out=-1.0).validate())
        self.assertIn("confidence-threshold", ProviderConfig(
            confidence_threshold=2.0).validate())
        self.assertIn("strategy-deadline", ProviderConfig(
            strategy_deadline=-2.0).validate())

    def test_max_tokens_must_be_at_least_one(self):
        for bad in (0, -5):
            with self.subTest(bad=bad):
                self.assertIn("deepseek-max-tokens", ProviderConfig(
                    deepseek_max_tokens=bad).validate())

    def test_reserve_larger_than_the_cap_is_rejected(self):
        cfg = ProviderConfig(strategy_call_cap=2, postmortem_reserve=3)
        self.assertIn("cannot exceed", cfg.validate())

    def test_campaign_fields_run_through_the_same_routine(self):
        cfg = ProviderConfig()
        self.assertIn("episodes", cfg.validate(episodes=0))
        self.assertIn("episode-timeout",
                      cfg.validate(episode_timeout=-1.0))
        self.assertIsNone(cfg.validate(episodes=2, episode_timeout=300.0))


class TestControllerConstructionValidation(WireHarness):
    """Medium 2: a programmatic config cannot bypass the CLI's checks."""

    def _build(self, config):
        return controller.Controller(
            config, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)

    def test_incomplete_tariff_with_usd_cap_fails_loudly(self):
        with self.assertRaises(ValueError) as cm:
            self._build(ProviderConfig(usd_cap=1.0))
        self.assertIn("complete tariff", str(cm.exception))

    def test_nan_price_fails_loudly(self):
        with self.assertRaises(ValueError):
            self._build(ProviderConfig(deepseek_price_in=float("nan")))

    def test_negative_max_tokens_fails_loudly(self):
        with self.assertRaises(ValueError):
            self._build(ProviderConfig(deepseek_max_tokens=-1))

    def test_reserve_above_cap_fails_loudly(self):
        with self.assertRaises(ValueError):
            self._build(ProviderConfig(strategy_call_cap=1,
                                       postmortem_reserve=2))

    def test_valid_config_still_constructs(self):
        self.assertIsNotNone(self._build(ProviderConfig()))


class TestLedgerInvariants(unittest.TestCase):
    """Medium 2: the ledger refuses what it cannot enforce."""

    def test_usd_cap_without_a_tariff_is_rejected(self):
        with self.assertRaises(ValueError):
            budget.BudgetLedger(usd_cap=1.0, tariff=None)

    def test_invalid_tariff_is_rejected(self):
        for t in (budget.Tariff(float("nan"), 1.0),
                  budget.Tariff(-1.0, 1.0),
                  budget.Tariff(None, 1.0)):
            with self.subTest(tariff=t):
                with self.assertRaises(ValueError):
                    budget.BudgetLedger(tariff=t)

    def test_negative_caps_are_rejected_not_clamped(self):
        with self.assertRaises(ValueError):
            budget.BudgetLedger(token_cap=-1)
        with self.assertRaises(ValueError):
            budget.BudgetLedger(strategy_cap=-1)
        with self.assertRaises(ValueError):
            budget.BudgetLedger(reflex_cap=-1)

    def test_negative_or_nan_bound_is_rejected(self):
        led = budget.BudgetLedger()
        with self.assertRaises(ValueError):
            led.reserve_strategy(prompt_tokens=-1)
        with self.assertRaises(ValueError):
            led.reserve_strategy(completion_tokens=float("nan"))

    def test_negative_postmortem_reserve_still_clamps(self):
        # the one documented clamp: defence in depth, not a semantic change
        led = budget.BudgetLedger(strategy_cap=4, postmortem_reserve=-1)
        self.assertEqual(led.postmortem_reserve, 0)


# ================================================ Jev fallback usage (M3)

class TestJevFallbackUsage(WireHarness):
    """Medium 3: a paid Jev answer is billed whether or not it is used."""

    def _runner(self, fake):
        cfg = ProviderConfig(max_ticks=200, reflex="jev", reflex_call_cap=5,
                             postmortem_reserve=0, deepseek_price_in=1.0,
                             deepseek_price_out=1.0)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        ctl._new_reflex_provider = lambda reflex: fake
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        runner.pending_key = protocol.NeedKey(1, 1, 1)
        runner.pending_seq = 1
        runner.pending_need = {"kind": "command", "id": 1}
        return runner, rec

    def test_rejected_answer_is_billed_once_and_falls_back(self):
        usage = {"prompt_tokens": 1000000, "completion_tokens": 0,
                 "reported": True}
        fake = _FakeJev(usage=usage)
        fake.action = None                   # low confidence / abstain shape
        runner, rec = self._runner(fake)
        proposal, provider, reason, latency, u, low = runner._decide(
            runner.pending_need)
        rec.finalize({})
        self.assertEqual(provider, "scripted")   # fell back
        self.assertTrue(low)
        self.assertEqual(runner.ledger.reflex_fallback, 1)
        self.assertEqual(runner.ledger.reflex_successful, 0)
        # exactly once: one full prompt is billed, not two
        self.assertEqual(runner.ledger.prompt_tokens, 1000000)
        self.assertAlmostEqual(runner.ledger.estimated_usd, 1.0)

    def test_accepted_answer_is_billed_once(self):
        usage = {"prompt_tokens": 1000000, "completion_tokens": 0,
                 "reported": True}
        fake = _FakeJev(usage=usage)          # action set, confidence 0.9
        runner, rec = self._runner(fake)
        proposal, provider, reason, latency, u, low = runner._decide(
            runner.pending_need)
        rec.finalize({})
        self.assertEqual(provider, "jev")
        self.assertFalse(low)
        self.assertEqual(runner.ledger.reflex_successful, 1)
        self.assertEqual(runner.ledger.prompt_tokens, 1000000)
        self.assertAlmostEqual(runner.ledger.estimated_usd, 1.0)

    def test_each_rejection_shape_carries_usage(self):
        prov = providers.JevReflex(ProviderConfig(reflex="jev"))
        for reason in ("low-confidence", "abstain", "invalid-option",
                       "invalid-action", "invalid-confidence"):
            with self.subTest(reason=reason):
                res = prov._rejected(reason, {"prompt_tokens": 9}, 0.01)
                self.assertIsNone(res.action)
                self.assertEqual(res.reason, reason)
                self.assertEqual(res.usage, {"prompt_tokens": 9})


# ================================================== cancellation races (M4)

class _BarrierSup(providers._WorkerSupervisor):
    """A supervisor whose construction blocks, exposing the install window."""

    constructed = threading.Event()
    proceed = threading.Event()
    starts = 0

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).constructed.set()
        type(self).proceed.wait(5.0)

    def start(self, job, deadline):
        type(self).starts += 1
        super().start(job, deadline)

    @classmethod
    def reset(cls):
        cls.constructed = threading.Event()
        cls.proceed = threading.Event()
        cls.starts = 0


class _GatedReaderSup(providers._WorkerSupervisor):
    """Reader that drains the pipe but delays publishing its output.

    This is the exact window in which an unconditional ``cancel()`` would
    signal completion while ``_out`` is still empty and lose the completed
    result (and its usage).
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.drained = threading.Event()
        self.release = threading.Event()

    def _read(self):
        out = b""
        try:
            while len(out) <= self.max_bytes:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                out += chunk
        except (OSError, ValueError):
            pass
        self.drained.set()              # pipe drained, NOT yet published
        self.release.wait(5.0)
        self._out = out                 # publish
        self._close_pipes()
        self._done.set()


class TestCancellationRace(WireHarness):
    """Medium 4: cancellation races supervisor startup / reader drain."""

    def _script(self, body):
        path = os.path.join(self.dir, "fake_worker.py")
        with open(path, "w") as fh:
            fh.write(body)
        return [sys.executable, path]

    def _ctx(self):
        return providers.StrategyContext(episode=1, tick=1, level="Dlvl:1",
                                         status_text="HP 10/20")

    def _strategy(self, argv):
        env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"})
        env.start()
        self.addCleanup(env.stop)
        return providers.DeepSeekStrategy(
            ProviderConfig(strategy="deepseek"), worker_argv=argv)

    def test_cancel_before_install_spawns_no_worker(self):
        _BarrierSup.reset()
        real = providers._WorkerSupervisor
        providers._WorkerSupervisor = _BarrierSup
        self.addCleanup(setattr, providers, "_WorkerSupervisor", real)
        prov = self._strategy(self._script("import time\ntime.sleep(30)\n"))
        call = controller._ReflexCall(
            lambda: prov.deliberate(self._ctx(), time.monotonic() + 5.0))
        call.start()
        self.assertTrue(_BarrierSup.constructed.wait(3.0))
        # cancel arrives exactly before the supervisor would be installed
        prov.cancel()
        _BarrierSup.proceed.set()
        self.assertTrue(call.wait(3.0))
        self.assertEqual(_BarrierSup.starts, 0)      # nothing was spawned
        self.assertFalse(call.result.ok)
        self.assertEqual(call.result.reason, "cancelled")
        prov.reap()

    def test_cancel_after_install_reaps_the_running_worker(self):
        prov = self._strategy(self._script("import time\ntime.sleep(30)\n"))
        call = controller._ReflexCall(
            lambda: prov.deliberate(self._ctx(), time.monotonic() + 10.0))
        call.start()
        sup = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            sup = prov._sup
            if sup is not None and sup.proc is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(sup)
        pid = sup.proc.pid
        t0 = time.monotonic()
        prov.cancel()
        self.assertTrue(call.wait(6.0))          # thread terminates boundedly
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertTrue(_wait_gone(pid, 3.0))    # no survivor
        prov.reap()

    def test_cancel_preserves_a_completed_result_during_drain(self):
        argv = self._script(
            "import sys\n"
            "sys.stdout.write('{\"v\":1,\"ok\":true,\"status\":200,"
            "\"json\":{\"n\":5}}\\n')\n")
        sup = _GatedReaderSup(argv)
        sup.start({"v": 1}, time.monotonic() + 5.0)
        self.assertTrue(sup.drained.wait(3.0))
        self.assertFalse(sup._done.is_set())
        t = threading.Thread(target=sup.cancel)
        t.start()
        time.sleep(0.2)
        sup.release.set()                    # the reader publishes + drains
        t.join(5.0)
        self.assertFalse(t.is_alive())
        res = sup.poll()
        self.assertIsNotNone(res)
        self.assertTrue(res.ok, res.error)   # preserved, not unknown exposure
        self.assertEqual(res.json, {"n": 5})
        sup.reap()


# ============================================ lifecycle persistence (L5)

class TestLifecyclePersistence(WireHarness):
    """Low 5: lifecycle records persist incrementally and stay bounded."""

    def test_large_lifecycle_burst_is_bounded_and_complete(self):
        rec = recording.EpisodeRecorder(self.dir, 1)
        led = events.EventLedger(sink=rec.record_event, cap=4096)
        n = 4200
        for i in range(n):
            b = events.Boundary("novelty-class", "class:%d" % i)
            led.detect(b, i, "Dlvl:1")
            led.transition("queued", b.eid, "queued", i, "Dlvl:1")
            led.transition("dispatched", b.eid, "dispatch", i, "Dlvl:1")
            led.transition("applied", b.eid, "applied", i, "Dlvl:1")
        led.flush()
        rec.finalize({})
        self.assertLessEqual(len(led.as_list()), 4096)   # bounded memory
        self.assertGreater(led.collapsed, 0)             # detail collapsed
        self.assertFalse(rec.incomplete)                 # no burst loss
        rows = _read_jsonl(os.path.join(self.dir, "ep-1.events.jsonl"))
        boundary = [r for r in rows if r.get("record") == "boundary"]
        self.assertEqual(len(boundary), n)               # every record kept
        self.assertTrue(all(r["terminal"] is not None for r in boundary))

    def test_incremental_flush_emits_each_eid_exactly_once(self):
        scen = (_line(hello())
                + _line(st_obs(1, command_need(1), dlvl="Dlvl:1",
                               hp=10, hp_max=20))
                + _line(st_obs(2, command_need(2), dlvl="Dlvl:2",
                               hp=10, hp_max=20))
                + _line(CLOSED))
        result, _ = self.run_scenario(
            scen, config=ProviderConfig(max_ticks=50))
        self.assertTrue(result.closed)
        rows = _read_jsonl(os.path.join(self.dir, "ep-1.events.jsonl"))
        eids = [r["eid"] for r in rows if r.get("record") == "boundary"]
        self.assertEqual(len(eids), len(set(eids)))      # no duplicates
        self.assertIn("closed", eids)

    def test_detected_only_records_finalise_each_round(self):
        # The shipped default (strategy="off") never queues a boundary, so a
        # detected record must still be emitted incrementally -- not held in
        # EventLedger._open until the end-of-episode flush.
        cfg = ProviderConfig(max_ticks=2000, strategy="off",
                             postmortem_reserve=0)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        self.assertFalse(runner._strategy_live())
        rounds, per_round = 8, 600           # 4800 > the 4096 retain cap
        seen = []
        for r in range(rounds):
            batch = [events.Boundary("novelty-class",
                                     "class:%d:%d" % (r, i))
                     for i in range(per_round)]
            seen.extend(b.eid for b in batch)
            runner.mem.boundary.check = lambda *a, **k: list(batch)
            runner.tick = r
            runner._detect_boundaries()
            # every detected record finalises in the round that produced it
            self.assertEqual(runner.event_ledger._open, {})
            self.assertLessEqual(len(runner.event_ledger._retained), 4096)
        rec.finalize({})
        self.assertGreater(runner.event_ledger.collapsed, 0)
        recs = [r for r in _read_jsonl(
                os.path.join(self.dir, "ep-1.events.jsonl"))
                if r.get("record") == "boundary"]
        eids = [r["eid"] for r in recs]
        self.assertEqual(len(eids), rounds * per_round)
        self.assertEqual(set(eids), set(seen))       # each eid exactly once
        # detected-only semantics: no invented terminal state
        self.assertTrue(all(r["terminal"] is None for r in recs))


# ========================================== coalescing provenance (L6)

class TestCoalescingProvenance(unittest.TestCase):
    """Low 6: coalescing is stamped from the complete pending set."""

    def test_later_submission_restamps_every_member(self):
        led = events.EventLedger()
        q = events.BoundaryQueue(event_ledger=led)
        a = events.Boundary("initial-level", "level:Dlvl:1:1")
        b = events.Boundary("hunger-weak", "hunger:Weak")
        q.submit([a], 0, "Dlvl:1")
        q.submit([b], 1, "Dlvl:1")          # B joins A's pending set
        pending = q.pending
        self.assertEqual(sorted(pending.eids),
                         ["hunger:Weak", "level:Dlvl:1:1"])
        q.mark_dispatched(1, 100.0)
        q.finish(True)
        recs = {r["eid"]: r for r in led.as_list()}
        self.assertEqual(recs["level:Dlvl:1:1"]["coalesced_with"],
                         ["hunger:Weak"])
        self.assertEqual(recs["hunger:Weak"]["coalesced_with"],
                         ["level:Dlvl:1:1"])
        # both members name the same dispatched set
        for eid in pending.eids:
            self.assertEqual(sorted(recs[eid]["coalesced_with"]
                                    + [eid]), sorted(pending.eids))


if __name__ == "__main__":
    unittest.main()
