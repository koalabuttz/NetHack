/* winagent.h -- engine-facing declarations for the agent window port.
 *
 * This header is for the game and the port implementation only.  It includes
 * engine types, so it must never be included from the engine-free public
 * headers under win/agent/ or from the standalone launcher.
 *
 * The agent port is registered like any other port through winchoices[], but
 * it is a *trusted-bootstrap* port: it runs only when the process carries the
 * private, non-restorable agent latch established before early_init().  The
 * latch is process-lifetime memory that save/restore data cannot reach.
 */

#ifndef WINAGENT_H
#define WINAGENT_H

#include <stdarg.h>

struct window_procs;

extern struct window_procs agent_procs;

/* ---- private agent-mode latch and bootstrap (agent_bootstrap.c) ---- */

/* Establish agent mode from the trusted launcher if the process carries a
 * valid locator and handshake.  Called at the very top of main(), before
 * early_init().  It may only touch OS resources, never engine globals. */
void agent_bootstrap_probe(int *argc, char ***argv);

/* True once a valid handshake has been consumed.  Backed by private static
 * storage, not by flags/iflags, so neither configuration nor a save file can
 * set or clear it. */
int agent_mode(void);

/* Post-early_init setup: private prefixes, diagnostic routing, and the
 * runtime policy phase.  Called after early_init() has reset the globals. */
void agent_bootstrap_after_globals(void);

/* The inherited transport descriptor, or -1 when not latched.  The port uses
 * it for all player-facing I/O. */
int agent_bootstrap_fd(void);

/* Enforce the Wave A argument policy before ordinary argument parsing: a
 * latched worker rejects options that would let it choose the frontend,
 * the configuration, symbols, or hooks. */
void agent_bootstrap_argv_policy(int argc, char **argv);

/* Refuse a window choice that is inconsistent with the latch.  Called by
 * choose_windows() before its selection loop.  Returns the window name that
 * must actually be used, or terminates the process privately. */
const char *agent_enforce_window_choice(const char *requested);

/* Low-level private termination: writes only to the private diagnostic sink
 * and never returns.  It must not call exit_nhwindows(), pline(), panic(), or
 * anything that could emit a player-facing byte. */
void agent_private_fatal(const char *msg) __attribute__((noreturn));

/* Diagnostic producer isolation for impossible()/panic paths: consume
 * the formatted text into a bounded private sink and terminate low-level. */
void agent_impossible_fatal(const char *fmt, va_list ap)
    __attribute__((noreturn));

/* Private diagnostic sink used by the port and the fatal paths. */
void agent_private_diag(const char *msg);

/* Trusted launch roots carried by the handshake, or NULL when not latched.
 * Wave A prerequisite for the handshake gate: a headless worker must read the
 * launcher-approved immutable data and configuration roots rather than the
 * compiled-in playground, otherwise startup cannot complete without writing
 * into a human playground. */
const char *agent_trusted_data_root(void);
const char *agent_trusted_writable_root(void);
const char *agent_trusted_config_root(void);
const char *agent_trusted_sysconf(void);

#endif /* WINAGENT_H */
