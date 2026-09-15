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
	episode play matrix-full sanitize depcheck clean
