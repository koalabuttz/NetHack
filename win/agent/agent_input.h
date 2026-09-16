/* agent_input.h -- engine-facing native input primitives for the agent port.
 *
 * This header includes engine types (`hack.h`), so it is engine-facing and
 * must only be included from the agent port implementation.  The engine-free
 * public headers under win/agent/ must never reach it.
 *
 * Each primitive freezes the working presentation into a full durable
 * snapshot carrying the outstanding request, reads one accepted action
 * through the frozen protocol sequence (agent_receive -> semantic validation
 * -> agent_accept), and translates it back to the native return convention of
 * the corresponding window callback.  A stale or invalid input emits an
 * `invalid` control record and leaves the request outstanding.
 */

#ifndef AGENT_INPUT_H
#define AGENT_INPUT_H

#include "hack.h"
#include "agent_protocol.h"

/* Provided by winagent.c. */
struct agent_session *agent_port_session(void);
enum agent_result agent_port_commit_need(const struct agent_need *need);
uint64_t agent_port_alloc_request(void);
void agent_port_fatal(const char *msg) __attribute__((noreturn));
void agent_port_diag(const char *msg);

/* Names the native interaction an input failure occurred in (provided by
 * agent_input.c; the message prefix stays private). */
void agent_port_set_input_context(const char *ctx);

/* Terminate privately with the current input context as a message prefix. */
void agent_port_fatal_ctx(const char *msg) __attribute__((noreturn));

/* nhgetch(): a raw key request.  kind is AG_NEED_COMMAND or AG_NEED_KEY. */
int agent_input_key(enum agent_need_kind kind, const char *prompt);

/* nh_poskey(): key byte, or an explicit position primitive (returns 0 with
 * the native coordinates and modifier set).  in_getpos selects the position
 * request context. */
int agent_input_poskey(coordxy *x, coordxy *y, int *mod);

/* yn_function(): hidden suffix stays private; native default/Escape/numeric
 * semantics are reproduced. */
int agent_input_yn(const char *query, const char *choices, char def);

/* The request kind for the current yn_function context: AG_NEED_DIRECTION in
 * the getdir() input context, else AG_NEED_YN. */
enum agent_need_kind agent_input_yn_kind(void);

/* getlin(): bounded line request; cancellation yields the native Escape
 * result in buf.  cap is the native destination capacity in bytes. */
void agent_input_line(const char *query, char *buf, size_t cap);

/* get_ext_cmd(): text answer resolved privately with the native exact
 * matcher; returns the sole native index or the native no-match (-1). */
int agent_input_extcmd(void);

#endif /* AGENT_INPUT_H */
