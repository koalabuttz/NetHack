/* agent_protocol.h -- bounded JSON framing, durable commits, session state.
 *
 * Engine-free.  Includes only the public agent headers plus system headers.
 * I/O goes through explicit read/write callbacks, never ambient stdout.
 *
 * Action acceptance is two-phase.  agent_receive() frames a line, parses it,
 * and applies every protocol-level check that needs no engine knowledge, but
 * it does NOT record the action as accepted.  The caller then runs its own
 * semantic validation (agent_menu_validate, native yn semantics, and so on)
 * and calls agent_accept() to record acceptance.  A semantically rejected
 * action therefore leaves the request outstanding and can be resubmitted with
 * the same request id.
 */

#ifndef AGENT_PROTOCOL_H
#define AGENT_PROTOCOL_H

#include "agent_types.h"
#include "agent_menu.h"

/* read returns >0 bytes read, 0 for EOF, -1 for error.
 * write returns >0 bytes written (may be short), <=0 for error. */
typedef long (*agent_read_fn)(void *ctx, char *buf, size_t cap);
typedef long (*agent_write_fn)(void *ctx, const char *buf, size_t len);

/* transport auxiliary records (strict control allowlist) */
enum agent_aux_kind {
    AG_AUX_NONE = 0,
    AG_AUX_ACK_SEQ,
    AG_AUX_ACK_CHUNK,
    AG_AUX_GET_PAGE
};

struct agent_aux {
    enum agent_aux_kind kind;
    uint64_t id;      /* get_page request id */
    uint64_t seq;     /* ack_seq */
    uint64_t rid;     /* ack_chunk logical record id */
    long i;           /* ack_chunk index */
    long page;        /* get_page index */
    char content[AG_ID_STR_MAX];
    enum agent_invalid_code code;
};

struct agent_session {
    agent_read_fn read;
    agent_write_fn write;
    void *io;

    char inbuf[AG_MAX_LINE_BYTES];
    size_t inlen;
    bool eof;

    /* physical record budget; production is AG_MAX_LINE_BYTES.  A smaller
     * value forces the chunk path with identical logical content. */
    size_t limit_line;
    /* force the chunk path even when the record would fit one line, so the
     * same logical record can be produced both ways */
    bool force_chunk;

    uint64_t next_delivery; /* d */
    uint64_t next_seq;      /* durable seq */
    uint64_t next_event;    /* message/event id */
    uint64_t next_content;  /* "cN" */
    uint64_t next_window;   /* "wN" */
    uint64_t next_menu;     /* "mN" */
    uint64_t acked_seq;
    uint64_t acked_chunk;   /* highest contiguous chunk index acknowledged */
    uint64_t last_rid;    /* rid of the most recent chunk stream, 0 none */
    long last_chunk_count;  /* physical chunks emitted for last_rid */

    bool hello_sent;        /* hello is emitted at most once */
    bool closed_sent;       /* closed is emitted at most once */
    bool closed;

    /* every outstanding-request constraint, persisted for later validation */
    uint64_t outstanding_id;
    enum agent_need_kind outstanding_kind;
    char need_content[AG_ID_STR_MAX];
    char need_menu[AG_ID_STR_MAX];   /* menu generation the request pins */
    int need_x0, need_y0, need_x1, need_y1; /* legal position rectangle */
    int need_pages;
    unsigned char pages_done[AG_PAGES_BITMAP_BYTES];
    int pages_delivered;    /* count of distinct delivered page indices */

    /* exact accepted-action identity (bytes, plus a hash pre-filter) */
    bool have_action;
    uint64_t action_id;
    uint32_t action_hash;
    char *action_text;
    size_t action_len;
    size_t action_cap;

    /* the complete immutable logical response stream, for exact replay */
    char *reply;
    size_t reply_len;
    size_t reply_cap;
    bool have_reply;

    /* last physical record, for identical retry */
    char last_line[AG_MAX_LINE_BYTES];
    size_t last_line_len;
    uint64_t last_line_d;

    /* the raw line behind the action returned by the last agent_receive */
    char pending_line[AG_MAX_LINE_BYTES];
    size_t pending_len;
    uint32_t pending_hash;

    unsigned replays;
    unsigned invalids;
    enum agent_invalid_code last_code;
};

void agent_session_init(struct agent_session *s, agent_read_fn rd,
                        agent_write_fn wr, void *io);
void agent_session_free(struct agent_session *s);

/* Emit hello once, before any player payload.  A second call is rejected. */
enum agent_result agent_write_hello(struct agent_session *s);

/* Freeze and deliver the durable presentation, retaining the response for the
 * last accepted action.  need may be NULL for a final boundary. */
enum agent_result agent_commit(struct agent_session *s,
                               const struct agent_view *v,
                               const struct agent_need *need);

/* Read until a fresh, protocol-valid action is available.  Transport
 * auxiliaries and identical retries are consumed internally.  On a rejected
 * input returns AG_BAD_INPUT without consuming the outstanding request and
 * without recording acceptance. */
enum agent_result agent_receive(struct agent_session *s,
                                struct agent_action *out);

/* Record acceptance of an action previously returned by agent_receive, after
 * the caller's semantic validation has succeeded. */
enum agent_result agent_accept(struct agent_session *s,
                               const struct agent_action *a);

/* Emit the exact bare terminal closure.  A second call is rejected. */
enum agent_result agent_write_closed(struct agent_session *s);

/* Emit an invalid control record for the given public code. */
enum agent_result agent_write_invalid(struct agent_session *s,
                                      enum agent_invalid_code code);

/* Re-send the retained last physical record with its original d and bytes. */
enum agent_result agent_retry_last(struct agent_session *s);

/* Strict parse of one act record.  The caller presets
 * caller to receive menu commit rows. */
enum agent_result agent_parse_action(const char *buf, size_t len,
                                     struct agent_action *out);

/* Strict parse of one transport auxiliary record. */
enum agent_result agent_parse_aux(const char *buf, size_t len,
                                  struct agent_aux *out);

/* Deterministic chunk planning: group an ordered part-length list into chunks
 * that each fit budget, never splitting a part.  Returns the number of chunks
 * used, or 0 if the parts cannot be packed within max_chunks. */
size_t agent_chunk_plan(const size_t part_len[], size_t nparts, size_t budget,
                        size_t chunk_of[], size_t max_chunks);

#endif /* AGENT_PROTOCOL_H */
