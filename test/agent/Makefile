# test/agent/Makefile -- engine-free P1 fixtures for the NetHack agent port.
#
#   make check       build and run every fixture
#   make sanitize    rebuild with AddressSanitizer + UndefinedBehaviorSanitizer
#   make depcheck    prove no fixture reaches an engine header
#   make clean
#
# Nothing here builds the game, runs setup.sh, or touches the main build.

CC ?= cc
AGENT = ../../win/agent
CFLAGS = -std=c99 -Wall -Wextra -Werror -I$(AGENT) -I.
SAN = -fsanitize=address,undefined -fno-omit-frame-pointer -g

AGENT_OBJS = agent_view.o agent_menu.o agent_protocol.o
REF_OBJS = reference_state.o

TESTS = test_view test_menu test_protocol test_state

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

check: $(TESTS)
	@fail=0; for t in $(TESTS); do \
	    printf '%s: ' "$$t"; ./$$t || fail=1; \
	done; \
	if [ $$fail -ne 0 ]; then echo "CHECK FAILED"; exit 1; fi; \
	echo "all P1 fixtures pass"

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

# Use the compiler's dependency output, not grep, to prove that no unit
# reaches include/ or src/.
depcheck:
	@fail=0; for f in $(AGENT)/agent_view.c $(AGENT)/agent_menu.c \
	                   $(AGENT)/agent_protocol.c reference_state.c; do \
	    $(CC) $(CFLAGS) -MD -MF .dep.$$(basename $$f).d -c $$f -o /dev/null \
	        || exit 1; \
	    if grep -E '(^|[[:space:]])(\.\./)*(include|src)/[A-Za-z_]+\.h([[:space:]]|$$)' \
	           .dep.$$(basename $$f).d >/dev/null; then \
	        echo "ENGINE HEADER reached by $$f:"; \
	        grep -E '(^|[[:space:]])(\.\./)*(include|src)/[A-Za-z_]+\.h' \
	             .dep.$$(basename $$f).d; \
	        fail=1; \
	    fi; \
	done; \
	rm -f .dep.*.d; \
	if [ $$fail -ne 0 ]; then echo "DEPCHECK FAILED"; exit 1; fi; \
	echo "no engine headers in any fixture translation unit"

clean:
	rm -f $(TESTS) $(AGENT_OBJS) $(REF_OBJS) .dep.*.d

.PHONY: all check sanitize depcheck clean
