/* agent_protocol.h -- bounded JSON framing, durable commits, session state.
 *
 * Engine-free.  Includes only the public agent headers plus system headers.
 * I/O goes through explicit read/write callbacks, never ambient stdout.
 */

#ifndef AGENT_PROTOCOL_H
#define AGENT_PROTOCOL_H

#include "agent_types.h"
#include "agent_menu.h"

/* read returns >0 bytes read, 0 for EOF, -1 for error.
 * write returns >0 bytes written (may be short), <=0 for error. */
typedef long (*agent_read_fn)(void *ctx, char *buf, size_t cap);
typedef long (*agent_write_fn)(void *ctx, const char *buf, size_t len);

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

    uint64_t next_delivery; /* d */
    uint64_t next_seq;      /* durable seq */
    uint64_t next_event;    /* message/event id */
    uint64_t next_content;  /* "cN" */
    uint64_t next_window;   /* "wN" */
    uint64_t next_menu;     /* "mN" */
    uint64_t acked_seq;
    uint64_t acked_chunk;   /* highest contiguous chunk index acknowledged */
    uint64_t last_rid;      /* rid of the most recent chunk stream, 0 = none */

    /* outstanding gameplay request */
    uint64_t outstanding_id;
    enum agent_need_kind outstanding_kind;
    int need_pages;
    int pages_sent;
    const char *need_content;

    /* last accepted action identity and its retained response */
    bool have_action;
    uint64_t action_id;
    uint32_t action_hash;
    char last_reply[AG_MAX_LINE_BYTES];
    size_t last_reply_len;
    bool have_reply;

    /* last physical record, for identical retry */
    char last_line[AG_MAX_LINE_BYTES];
    size_t last_line_len;
    uint64_t last_line_d;

    unsigned replays;
    unsigned invalids;
    bool closed;
    enum agent_invalid_code last_code;
};

void agent_session_init(struct agent_session *s, agent_read_fn rd,
                        agent_write_fn wr, void *io);

/* Emit hello once, before any player payload. */
enum agent_result agent_write_hello(struct agent_session *s);

/* Freeze and deliver the durable presentation, retaining the response for the
 * last accepted action.  need may be NULL for a final boundary. */
enum agent_result agent_commit(struct agent_session *s, const struct agent_view *v,
                               const struct agent_need *need);

/* Read until a fresh executable action is available.  Transport auxiliaries
 * and identical retries are consumed internally.  On a rejected input returns
 * AG_BAD_INPUT without consuming the outstanding request. */
enum agent_result agent_receive(struct agent_session *s, struct agent_action *out);

/* Emit the exact bare terminal closure and mark the session closed. */
enum agent_result agent_write_closed(struct agent_session *s);

/* Emit an invalid control record for the given public code. */
enum agent_result agent_write_invalid(struct agent_session *s,
                                      enum agent_invalid_code code);

/* Re-send the retained last physical record with its original d and bytes. */
enum agent_result agent_retry_last(struct agent_session *s);

/* Strict parse of one act record.  out->commit/commit_cap must be preset by the
 * caller to receive menu commit rows. */
enum agent_result agent_parse_action(const char *buf, size_t len,
                                     struct agent_action *out);

/* Deterministic chunk planning: group an ordered part-length list into chunks
 * that each fit budget, never splitting a part.  Returns the number of chunks
 * used, or 0 if the parts cannot be packed within max_chunks. */
size_t agent_chunk_plan(const size_t part_len[], size_t nparts, size_t budget,
                        size_t chunk_of[], size_t max_chunks);

#endif /* AGENT_PROTOCOL_H */
