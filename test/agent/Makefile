# test/agent/Makefile -- engine-free P1 fixtures for the NetHack agent port.
#
#   make check       regenerate/verify the profile manifest, build and run
#                    every fixture
#   make sanitize    rebuild with AddressSanitizer + UndefinedBehaviorSanitizer
#   make depcheck    prove no fixture reaches an engine header
#   make clean
#
# Nothing here builds the game, runs setup.sh, or touches the main build.

CC ?= cc
AGENT = ../../win/agent
DOC = ../../doc
CFLAGS = -std=c99 -Wall -Wextra -Werror -I$(AGENT) -I.
SAN = -fsanitize=address,undefined -fno-omit-frame-pointer -g

AGENT_SRC = $(AGENT)/agent_view.c $(AGENT)/agent_menu.c \
            $(AGENT)/agent_protocol.c
AGENT_OBJS = agent_view.o agent_menu.o agent_protocol.o
REF_OBJS = reference_state.o
TEST_SRC = test_view.c test_menu.c test_protocol.c test_state.c
REF_SRC = reference_state.c

TESTS = test_view test_menu test_protocol test_state

# every C translation unit this milestone builds
ALL_TU = $(AGENT_SRC) $(REF_SRC) $(TEST_SRC)

all: $(TESTS)

agent_view.o: $(AGENT)/agent_view.c $(AGENT)/agent_view.h $(AGENT)/agent_types.h
	$(CC) $(CFLAGS) -c $(AGENT)/agent_view.c -o $@

agent_menu.o: $(AGENT)/agent_menu.c $(AGENT)/agent_menu.h $(AGENT)/agent_types.h
	$(CC) $(CFLAGS) -c $(AGENT)/agent_menu.c -o $@

agent_protocol.o: $(AGENT)/agent_protocol.c $(AGENT)/agent_protocol.h \
                  $(AGENT)/agent_menu.h $(AGENT)/agent_types.h
	$(CC) $(CFLAGS) -c $(AGENT)/agent_protocol.c -o $@

reference_state.o: reference_state.c reference_state.h
	$(CC) $(CFLAGS) -c reference_state.c -o $@

test_view: test_view.c agent_view.o
	$(CC) $(CFLAGS) -o $@ test_view.c agent_view.o

test_menu: test_menu.c agent_menu.o
	$(CC) $(CFLAGS) -o $@ test_menu.c agent_menu.o

test_protocol: test_protocol.c $(AGENT_OBJS)
	$(CC) $(CFLAGS) -o $@ test_protocol.c $(AGENT_OBJS)

test_state: test_state.c $(REF_OBJS) $(AGENT_OBJS)
	$(CC) $(CFLAGS) -o $@ test_state.c $(REF_OBJS) $(AGENT_OBJS)

# The frozen profile manifest is generated from include/optlist.h.  Regenerating
# it here means a drifted or hand-edited manifest fails the gate.
manifest-check:
	@python3 gen_profile.py > .profile.tmp
	@if diff -q .profile.tmp $(DOC)/agent-profile-v1.tsv > /dev/null; then \
	    echo "profile manifest: up to date"; \
	    rm -f .profile.tmp; \
	else \
	    echo "PROFILE MANIFEST DRIFTED:"; \
	    diff .profile.tmp $(DOC)/agent-profile-v1.tsv | head -40; \
	    rm -f .profile.tmp; \
	    exit 1; \
	fi

check: manifest-check header-check schema-check formatter-check $(TESTS)
	@fail=0; for t in $(TESTS); do \
	    printf '%s: ' "$$t"; ./$$t || fail=1; \
	done; \
	if [ $$fail -ne 0 ]; then echo "CHECK FAILED"; exit 1; fi; \
	echo "all P1 fixtures pass"

# Validate real encoder output and the built-in grammar vectors against
# doc/agent-v1.schema.json.
schema-check: test_protocol
	@./test_protocol --dump | python3 schema_check.py --stdin
	@python3 schema_check.py

# The canonical formatter must accept real encoder output and must reject a
# hostile chunk stream; its own vectors cover retries, gaps and bad slices.
formatter-check: test_protocol
	@./test_protocol --dump | python3 format_obs.py --json > /dev/null
	@python3 format_obs.py --selftest

# The generated enforcement header consumed by the agent-only build
# (win/agent/agent_profile.h) is emitted by the same generator with --emit-c.
# Checking it here means the committed artifact cannot drift from its source of
# truth (`python3 test/agent/gen_profile.py --emit-c`).
header-check:
	@python3 gen_profile.py --emit-c > .profile.h.tmp
	@if diff -q .profile.h.tmp $(AGENT)/agent_profile.h > /dev/null; then \
	    echo "agent_profile.h: up to date"; \
	    rm -f .profile.h.tmp; \
	else \
	    echo "AGENT_PROFILE.H DRIFTED:"; \
	    diff .profile.h.tmp $(AGENT)/agent_profile.h | head -40; \
	    rm -f .profile.h.tmp; \
	    exit 1; \
	fi

# The hostile-startup matrix (the M1 exit gate) needs a built agent-only worker
# and the trusted launcher, so it is not part of `check` (which stays
# engine-free).  Stage data with `make agent-test-data`, then:
#   make -C test/agent matrix WORKER=../../src/nethack \
#       RUNNER=../../src/nethack-agent DATA=/tmp/data SYSCONF=/tmp/data/sysconf
#
# `matrix` is the SMOKE run: the impossible-worker cases are reported as
# SKIPPED (and the exit status is still 0).  `matrix-full` is the gate: it
# REQUIRES the impossible worker (a build with AGENT_TEST_IMPOSSIBLE) and
# fails if it is missing, so it cannot pass while the runtime-policy or
# decision-callback contracts go unexercised.
matrix:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" || \
	    { echo "set WORKER, RUNNER, DATA (and optionally SYSCONF,"; \
	      echo "IMPOSSIBLE_WORKER) for the hostile matrix"; exit 2; }
	python3 hostile_matrix.py --worker "$(WORKER)" --runner "$(RUNNER)" \
	    --data "$(DATA)" $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    $(if $(PRIVATE_ROOT),--private-root "$(PRIVATE_ROOT)") \
	    $(if $(IMPOSSIBLE_WORKER),--impossible-worker "$(IMPOSSIBLE_WORKER)")

matrix-full:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" \
	    -a -n "$(IMPOSSIBLE_WORKER)" || \
	    { echo "matrix-full requires WORKER, RUNNER, DATA and"; \
	      echo "IMPOSSIBLE_WORKER (a build with AGENT_TEST_IMPOSSIBLE)"; \
	      exit 2; }
	python3 hostile_matrix.py --worker "$(WORKER)" --runner "$(RUNNER)" \
	    --data "$(DATA)" --require-impossible \
	    $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    $(if $(PRIVATE_ROOT),--private-root "$(PRIVATE_ROOT)") \
	    --impossible-worker "$(IMPOSSIBLE_WORKER)"

# The native episode tests need a built agent worker and launcher, so they are
# opt-in like `matrix`.  `episode` proves the startup boundary still holds;
# `play` creates a game, selects a character through the native role menus,
# moves the hero, and quits through the native command path.
episode:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" || \
	    { echo "set WORKER, RUNNER, DATA (and optionally SYSCONF,"; \
	      echo "PRIVATE_ROOT) for the native episode test"; exit 2; }
	python3 driver.py episode --runner "$(RUNNER)" --worker "$(WORKER)" \
	    --data "$(DATA)" $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    --private-root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-episode)"

play:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" || \
	    { echo "set WORKER, RUNNER, DATA (and optionally SYSCONF,"; \
	      echo "PRIVATE_ROOT) for the scripted gameplay test"; exit 2; }
	rm -rf "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-play)"
	mkdir -p "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-play)"
	python3 driver.py play --runner "$(RUNNER)" --worker "$(WORKER)" \
	    --data "$(DATA)" $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    --private-root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-play)"

# Interaction breadth on real engine menus (inventory, farlook, help with text
# paging, annotation, native quit confirmations).  Needs a built agent worker
# and launcher, so it is opt-in like `play`.
breadth:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" || \
	    { echo "set WORKER, RUNNER, DATA (and optionally SYSCONF,"; \
	      echo "PRIVATE_ROOT) for the interaction-breadth test"; exit 2; }
	rm -rf "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-breadth)"
	mkdir -p "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-breadth)"
	python3 driver.py breadth --runner "$(RUNNER)" --worker "$(WORKER)" \
	    --data "$(DATA)" $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    --private-root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-breadth)"

# Native save -> trusted artifact transfer -> restore into a fresh worker:
# asserts the save artifact is retrievable, restore starts with a full seq-1
# snapshot with restored history tagged hist, the same map is reproduced, and
# play continues; every episode closes generically and cleans its root.
# The restore assertions also prove hist/msg exclusivity: a known restored
# line appears in hist and never in the live message window.
lifecycle:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" || \
	    { echo "set WORKER, RUNNER, DATA (and optionally SYSCONF,"; \
	      echo "PRIVATE_ROOT) for the lifecycle test"; exit 2; }
	rm -rf "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-lifecycle)"
	mkdir -p "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-lifecycle)"
	python3 driver.py lifecycle --runner "$(RUNNER)" --worker "$(WORKER)" \
	    --data "$(DATA)" $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    --private-root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-lifecycle)/unused" \
	    --root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-lifecycle)"

# Two-episode contamination sentinels: episode A annotates its level with a
# fixed unique marker and saves; a FRESH episode must contain the marker
# nowhere, an episode RESTORED from A's artifact must contain it.
sentinel:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" || \
	    { echo "set WORKER, RUNNER, DATA (and optionally SYSCONF,"; \
	      echo "PRIVATE_ROOT) for the sentinel test"; exit 2; }
	rm -rf "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-sentinel)"
	mkdir -p "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-sentinel)"
	python3 driver.py sentinel --runner "$(RUNNER)" --worker "$(WORKER)" \
	    --data "$(DATA)" $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    --private-root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-sentinel)/unused" \
	    --root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-sentinel)"

# 100 sequential episodes, each reset at a varying point (prompt, mid-move,
# mid-menu, quit confirmation): reaping, descriptor/process plateau, and a
# fresh public namespace per episode.
plateau:
	@test -n "$(WORKER)" -a -n "$(RUNNER)" -a -n "$(DATA)" || \
	    { echo "set WORKER, RUNNER, DATA (and optionally SYSCONF,"; \
	      echo "PRIVATE_ROOT, COUNT) for the plateau test"; exit 2; }
	rm -rf "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-plateau)"
	mkdir -p "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-plateau)"
	python3 driver.py plateau --runner "$(RUNNER)" --worker "$(WORKER)" \
	    --data "$(DATA)" $(if $(SYSCONF),--sysconf "$(SYSCONF)") \
	    --private-root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-plateau)" \
	    --count "$(if $(COUNT),$(COUNT),100)"

# The launcher's transport teardown under a deadline that fires while the
# worker still has output to deliver: every complete line the worker wrote
# before it was reaped must reach the channel, and a line it died in the
# middle of must never.  Uses a synthetic transport peer (no game build needed
# beyond the launcher), so the case that used to fail once in a few hundred
# episodes fails deterministically here.
drain:
	@test -n "$(RUNNER)" || \
	    { echo "set RUNNER (and optionally PRIVATE_ROOT, DEADLINE) for the"; \
	      echo "launcher drain regression"; exit 2; }
	rm -rf "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-drain)"
	mkdir -p "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-drain)"
	python3 driver.py drain --runner "$(RUNNER)" \
	    --deadline "$(if $(DEADLINE),$(DEADLINE),2)" \
	    --private-root "$(if $(PRIVATE_ROOT),$(PRIVATE_ROOT),/tmp/agent-drain)"

# Menu ownership hardening under AddressSanitizer + UndefinedBehaviorSanitizer.
#
# The menu sidecar lives in the engine-linked window port, so this drives the
# real worker (built with `make USE_ASAN=1 USE_UBSAN=1 WANT_WIN_AGENT=1
# WANT_DEFAULT=agent WANT_AGENT_STRICT=1 all`) through the play and breadth
# flows.  Under ASan a use-after-free or heap overflow aborts the worker, so
# the launcher relays a nonzero status and the driver fails here; a completed
# run with a clean exit is evidence the menu sidecar lifecycle (capture,
# selection, cancel, repeated select_menu, window destroy, exit) is sound.
# The engine-free menu model and protocol peers are covered by `sanitize`, and
# the real-callback menu probes are exercised under the sanitizer by running
# `matrix`/`matrix-full` with an ASan impossible worker.
asan-lifecycle:
	@test -n "$(ASAN_WORKER)" -a -n "$(ASAN_RUNNER)" -a -n "$(ASAN_DATA)" || \
	    { echo "set ASAN_WORKER, ASAN_RUNNER, ASAN_DATA (and optionally"; \
	      echo "ASAN_SYSCONF, ASAN_PRIVATE_ROOT) for the sanitizer run"; exit 2; }
	rm -rf "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)"
	mkdir -p "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)"
	python3 driver.py play --runner "$(ASAN_RUNNER)" --worker "$(ASAN_WORKER)" \
	    --data "$(ASAN_DATA)" $(if $(ASAN_SYSCONF),--sysconf "$(ASAN_SYSCONF)") \
	    --deadline 120 --timeout 240 \
	    --private-root "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/play"
	python3 driver.py breadth --runner "$(ASAN_RUNNER)" --worker "$(ASAN_WORKER)" \
	    --data "$(ASAN_DATA)" $(if $(ASAN_SYSCONF),--sysconf "$(ASAN_SYSCONF)") \
	    --deadline 120 --timeout 240 \
	    --private-root "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/breadth"
	rm -rf "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/lc"
	mkdir -p "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/lc"
	python3 driver.py lifecycle --runner "$(ASAN_RUNNER)" --worker "$(ASAN_WORKER)" \
	    --data "$(ASAN_DATA)" $(if $(ASAN_SYSCONF),--sysconf "$(ASAN_SYSCONF)") \
	    --deadline 120 --timeout 240 \
	    --private-root "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/lc/unused" \
	    --root "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/lc"
	rm -rf "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/sn"
	mkdir -p "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/sn"
	python3 driver.py sentinel --runner "$(ASAN_RUNNER)" --worker "$(ASAN_WORKER)" \
	    --data "$(ASAN_DATA)" $(if $(ASAN_SYSCONF),--sysconf "$(ASAN_SYSCONF)") \
	    --deadline 120 --timeout 240 \
	    --private-root "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/sn/unused" \
	    --root "$(if $(ASAN_PRIVATE_ROOT),$(ASAN_PRIVATE_ROOT),/tmp/agent-asan)/sn"
	@echo "asan-lifecycle: play, breadth, save/restore and the sentinel completed with a clean exit under the sanitizer worker"

# ASan/UBSan build, if the compiler supports the flags.
sanitize: clean
	@$(CC) $(CFLAGS) $(SAN) -o test_view test_view.c $(AGENT)/agent_view.c \
	    && $(CC) $(CFLAGS) $(SAN) -o test_menu test_menu.c $(AGENT)/agent_menu.c \
	    && $(CC) $(CFLAGS) $(SAN) -o test_protocol test_protocol.c \
	       $(AGENT)/agent_view.c $(AGENT)/agent_menu.c $(AGENT)/agent_protocol.c \
	    && $(CC) $(CFLAGS) $(SAN) -o test_state test_state.c reference_state.c \
	       $(AGENT)/agent_view.c $(AGENT)/agent_menu.c $(AGENT)/agent_protocol.c \
	    && echo "sanitizer build ok" \
	    || { echo "sanitizer unavailable for this compiler"; exit 0; }
	@fail=0; for t in $(TESTS); do \
	    printf '%s: ' "$$t"; ./$$t || fail=1; \
	done; \
	if [ $$fail -ne 0 ]; then echo "SANITIZE FAILED"; exit 1; fi; \
	echo "all P1 fixtures pass under ASan/UBSan"

# Use the compiler's dependency output, not grep, to prove that no unit of this
# milestone reaches include/ or src/.  Every translation unit built by P1 is
# covered, including the fixtures themselves.
depcheck:
	@fail=0; for f in $(ALL_TU); do \
	    base=$$(basename $$f); \
	    $(CC) $(CFLAGS) -MD -MF .dep.$$base.d -c $$f -o /dev/null || exit 1; \
	    if grep -E '(^|[[:space:]])(\.\./)*(include|src)/[A-Za-z_]+\.h([[:space:]]|$$)' \
	           .dep.$$base.d > /dev/null; then \
	        echo "ENGINE HEADER reached by $$f:"; \
	        grep -E '(^|[[:space:]])(\.\./)*(include|src)/[A-Za-z_]+\.h' \
	             .dep.$$base.d; \
	        fail=1; \
	    fi; \
	done; \
	rm -f .dep.*.d; \
	if [ $$fail -ne 0 ]; then echo "DEPCHECK FAILED"; exit 1; fi; \
	echo "no engine headers in any fixture translation unit"

clean:
	rm -f $(TESTS) $(AGENT_OBJS) $(REF_OBJS) .dep.*.d .profile.tmp .profile.h.tmp

.PHONY: all check manifest-check header-check schema-check matrix \
	episode play breadth matrix-full sanitize asan-lifecycle depcheck clean \
	sentinel drain
