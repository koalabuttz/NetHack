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

/* One row of the content behind the outstanding request, registered by the
 * adapter so that get_page can return real slices.  A menu row carries the
 * full public row (r >= 1); a plain text line uses r == 0 and only
 * text/style.
 * The adapter owns the storage and must keep it valid until the request
 * completes (a new commit or receive supersedes it). */
struct agent_content_row {
    long r;              /* 0 = plain text line, else 1-based menu row id */
    const char *text;
    bool selectable;
    int key;             /* advisory accelerator byte, 0 = null */
    int group;           /* advisory group byte, 0 = null */
    bool has_initial;
    long initial;        /* -1 native all/default, else explicit */
    uint8_t style;
    uint8_t color;
    bool has_icon;
    struct agent_cell icon;
};

/* Rows per stable content page.  The adapter computes the declared page count
 * with agent_content_pages() so the request and the page records agree. */
#define AG_CONTENT_ROWS_PER_PAGE AG_PAGE_MAX_ROWS

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
    bool have_chunk_ack;    /* false = "none": no index acknowledged yet */
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
    int need_max;           /* advertised line/extcmd byte budget, 0..255 */
    unsigned char pages_done[AG_PAGES_BITMAP_BYTES];
    int pages_delivered;    /* count of distinct delivered page indices */

    /* adapter-registered rows behind the outstanding content; borrowed, not
     * owned, and superseded by the next commit/receive */
    const struct agent_content_row *content_rows;
    size_t content_nrows;

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

    /* the raw line behind the action returned by the last agent_receive, and
     * its session-owned identity; agent_accept() consumes only these */
    char pending_line[AG_MAX_LINE_BYTES];
    size_t pending_len;
    uint64_t pending_id;
    enum agent_action_kind pending_kind;
    uint32_t pending_hash;

    unsigned replays;
    unsigned invalids;
    enum agent_invalid_code last_code;
};

/* Allocate and initialize a session.
 *
 * The session OWNS heap allocations (the retained response stream and the
 * accepted-action bytes).  Every session MUST be released with
 * agent_session_free() on shutdown and BEFORE it is reinitialized; calling
 * agent_session_init() on a live session leaks those buffers. */
void agent_session_init(struct agent_session *s, agent_read_fn rd,
                        agent_write_fn wr, void *io);
void agent_session_free(struct agent_session *s);

/* Number of stable content pages for the given content rows: pages are
 * planned deterministically by BOTH the row count and the encoded byte
 * size, so the request's declared page count, a window descriptor, and
 * get_page emission all agree (0 for no content).  The adapter declares this
 * as the request's page count so get_page and the page records agree. */
size_t agent_content_pages(const struct agent_content_row *rows,
                           size_t nrows);

/* Register the rows behind the outstanding content so get_page can emit real
 * page slices.  rows is borrowed and must stay valid until the request
 * completes; passing NULL/0 clears it. */
void agent_session_set_content(struct agent_session *s,
                               const struct agent_content_row *rows,
                               size_t nrows);

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

/* Record acceptance of the action previously returned by agent_receive, after
 * the caller's semantic validation has succeeded.
 *
 * Acceptance operates SOLELY on the session-owned pending identity that
 * agent_receive captured: there is no action argument, so an unrelated
 * object cannot be bound to the pending bytes, and a zero request id can
 * never be accepted (the parser never produces one).  The pending identity
 * is consumed exactly once; a second call without an intervening
 * agent_receive fails. */
enum agent_result agent_accept(struct agent_session *s);

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
