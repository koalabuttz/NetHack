/* reference_state.h -- engine-free durable/audit reference model.
 *
 * This is the executable statement of doc/agent-interface.md sections 9 and
 * 10.  It is a model, not production code: it tracks exactly the invariants
 * the P1 gate names, and it is driven by test_state.c.
 */

#ifndef AGENT_REFERENCE_STATE_H
#define AGENT_REFERENCE_STATE_H

#include "agent_types.h"
#include "agent_protocol.h"

enum agent_lifecycle {
    AG_BOOT = 0,
    AG_QUARANTINE,
    AG_READY,
    AG_WAIT_INPUT,
    AG_EXECUTING,
    AG_DRAINING,
    AG_CLOSED
};

enum agent_delivery {
    AG_TX_IDLE = 0,
    AG_TX_RECORD,
    AG_TX_WAIT_CHUNK_ACK,
    AG_TX_WAIT_SEQ_ACK
};

enum agent_audit {
    AG_AUDIT_IDLE = 0,
    AG_AUDIT_OPEN,
    AG_AUDIT_ENDED
};

#define AG_MODEL_MAX_PAL 64
#define AG_MODEL_MAX_FRAMES 128
#define AG_MODEL_MAX_FRAME_CELLS 8
#define AG_MODEL_MAX_PARTS 32

struct agent_model_frame {
    uint64_t interval;
    int k;
    int ncells;
    struct {
        int x, y;
        struct agent_cell cell;
    } cells[AG_MODEL_MAX_FRAME_CELLS];
};

struct agent_model {
    enum agent_lifecycle lc;
    enum agent_delivery tx;
    enum agent_audit audit;

    uint64_t durable_seq;
    uint64_t next_delivery;

    /* working renderer W and frozen durable D, as normalized cells */
    struct agent_cell wmap[AG_MAP_ROWS][AG_MAP_COLS];
    struct agent_cell dmap[AG_MAP_ROWS][AG_MAP_COLS];
    struct agent_cell dpal[AG_MODEL_MAX_PAL];
    size_t ndpal;

    /* previous durable map, to decide whether a commit changed anything */
    struct agent_cell prevmap[AG_MAP_ROWS][AG_MAP_COLS];

    /* audit scratch T and the previous frame's view */
    struct agent_cell tmap[AG_MAP_ROWS][AG_MAP_COLS];
    struct agent_cell lastview[AG_MAP_ROWS][AG_MAP_COLS];
    bool scratch_active;

    uint64_t interval;
    int last_k;

    struct agent_model_frame ledger[AG_MODEL_MAX_FRAMES];
    size_t nledger;
    unsigned frames_displayed; /* deduped by (interval,k) */
    unsigned suppressed;       /* identical normalized frames */
    unsigned resync_replays;

    /* chunk delivery ledger */
    uint64_t last_rid;
    int last_nchunks;
    size_t last_chunk_of[AG_MODEL_MAX_PARTS];
    unsigned retries;
    char last_chunk_bytes[AG_MODEL_MAX_PARTS][64];
    size_t last_chunk_len[AG_MODEL_MAX_PARTS];

    /* observations for the tests */
    bool durable_map_changed;  /* last commit changed the durable map */
    unsigned commits;
    unsigned palette_allocations;
};

enum agent_model_event_kind {
    AG_EV_SET_DURABLE, /* working-durable write of one cell */
    AG_EV_ANIM,        /* an animation step: working + scratch, then frame */
    AG_EV_ANIM_FRAME,  /* capture a frame from the current scratch view */
    AG_EV_BLOCKING,    /* blocking display: durable boundary */
    AG_EV_INPUT,       /* unsatisfied input request: durable boundary */
    AG_EV_FINAL,       /* final ordinary completion: durable boundary */
    AG_EV_NONBLOCK,    /* nonblocking display: never advances seq */
    AG_EV_AUDIT_BEGIN, /* begin(interval, base) */
    AG_EV_CHUNK_SPLIT, /* plan chunks over the event's part lengths */
    AG_EV_RETRY,       /* re-send a chunk identically */
    AG_EV_ACK_CHUNK,   /* cumulative chunk acknowledgement */
    AG_EV_ACK_SEQ,     /* durable acknowledgement */
    AG_EV_RESYNC       /* replay the retained interval prefix, deduped */
};

struct agent_model_event {
    enum agent_model_event_kind kind;
    int x, y;
    struct agent_cell cell;
    size_t part_len[AG_MODEL_MAX_PARTS];
    size_t nparts;
    size_t budget;
    int index; /* chunk index for retry/ack */
};

void agent_model_reset(struct agent_model *m);
enum agent_result agent_model_step(struct agent_model *m,
                                   const struct agent_model_event *ev);

#endif /* AGENT_REFERENCE_STATE_H */
