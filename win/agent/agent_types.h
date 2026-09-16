/* agent_types.h -- pointer-free public values for the NetHack agent port.
 *
 * Engine-free: this header includes only <stdint.h>, <stddef.h>, <stdbool.h>.
 * It must never be reachable from hack.h and must never include an engine
 * header.  Public semantic values contain no engine pointers, no native
 * anything identifiers, and no opaque engine types.
 *
 * Everything here is the v1 contract described in doc/agent-interface.md.
 */

#ifndef AGENT_TYPES_H
#define AGENT_TYPES_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* ---- protocol identity and hard limits (plan 3.3) ---- */
#define AG_VERSION 1
#define AG_MAX_LINE_BYTES 65536        /* including the terminating LF */
#define AG_MAX_NESTING 8               /* JSON container depth */
#define AG_MAX_KEYS_PER_OBJECT 32
#define AG_MAX_TOKENS 32768
#define AG_MAX_ACTION_BYTES 65535
#define AG_KEY_MIN 1
#define AG_KEY_MAX 255
#define AG_LINE_INPUT_MAX 255     /* min(255, native dest capacity-1) */
#define AG_MAX_WINDOWS 32
#define AG_MAX_MENU_ROWS 65535
#define AG_MAX_TEXT_LINES 65535
#define AG_MAX_TEXT_BYTES 1048576
#define AG_MAX_RETAINED_BYTES 33554432u
#define AG_PAGE_MAX_ROWS 128
#define AG_PAGE_MAX_BYTES 16384
#define AG_COUNT_MAX 2147483647L       /* additionally <= LONG_MAX */
#define AG_COUNTER_MAX 9007199254740991ULL /* 2^53-1 */

/* presentation identifiers are "mN" / "cN" / "wN" strings; this is the
 * widest one the encoder ever emits ("c" + 14 digits + NUL) */
#define AG_ID_STR_MAX 16

/* content pages per menu/text window: at most AG_MAX_PAGES pages, whose
 * zero-based indices run 0..AG_MAX_PAGE_INDEX */
#define AG_MAX_PAGES 65535
#define AG_MAX_PAGE_INDEX 65534
#define AG_PAGES_BITMAP_BYTES ((AG_MAX_PAGES + 7) / 8)

/* fixture bounds: a single public view/action value is a fixed-size value */
#define AG_VIEW_MAX_PALETTE 256
#define AG_VIEW_MAX_STATUS 32
#define AG_VIEW_MAX_COND 32
#define AG_VIEW_MAX_WINDOWS 32
#define AG_VIEW_MAX_COMMIT 256

/* ---- map geometry (include/global.h COLNO/ROWNO) ----
 *
 * The view stores the native 80-column map array indexed by native x:
 * map[native_y][native_x].  Native column zero is unused and is never
 * published, so the legal public coordinates are exactly
 * x = AG_MAP_MIN_X .. AG_MAP_MAX_X and y = AG_MAP_MIN_Y .. AG_MAP_MAX_Y.
 * The encoder emits the stored coordinates unchanged; it never adds an
 * offset and never emits x = 0 or x = 80.  A cursor follows the same
 * convention.
 */
#define AG_MAP_COLS 80
#define AG_MAP_ROWS 21
#define AG_MAP_MIN_X 1
#define AG_MAP_MAX_X 79
#define AG_MAP_MIN_Y 0
#define AG_MAP_MAX_Y 20

/* ---- results and public error codes ---- */
enum agent_result {
    AG_OK = 0,
    AG_BAD_INPUT,   /* public schema/input fact; input is not consumed */
    AG_LIMIT,       /* a declared bound was exceeded */
    AG_IO,          /* transport failure or EOF */
    AG_INTERNAL     /* protocol/state failure; close generically */
};

/* public code carried by a control "invalid" record, never diagnostic text */
enum agent_invalid_code {
    AG_INV_NONE = 0,
    AG_INV_SCHEMA,
    AG_INV_STALE,
    AG_INV_KIND,
    AG_INV_RANGE,
    AG_INV_INCOMPLETE
};

/* ---- public appearance ---- */
enum agent_render_context {
    AG_RC_MAP = 0,
    AG_RC_MENU
};

/* Public basic color slots, in native include/color.h numbering.
 * Slot 8 is the native NO_COLOR and is published as "none". */
enum agent_color {
    AG_COL_BLACK = 0,
    AG_COL_RED = 1,
    AG_COL_GREEN = 2,
    AG_COL_BROWN = 3,
    AG_COL_BLUE = 4,
    AG_COL_MAGENTA = 5,
    AG_COL_CYAN = 6,
    AG_COL_GRAY = 7,
    AG_COL_NONE = 8,
    AG_COL_ORANGE = 9,
    AG_COL_BRIGHT_GREEN = 10,
    AG_COL_YELLOW = 11,
    AG_COL_BRIGHT_BLUE = 12,
    AG_COL_BRIGHT_MAGENTA = 13,
    AG_COL_BRIGHT_CYAN = 14,
    AG_COL_WHITE = 15,
    AG_COL_MAX = 16
};

#define AG_STYLE_BOLD 0x01u
#define AG_STYLE_DIM 0x02u
#define AG_STYLE_ITALIC 0x04u
#define AG_STYLE_UNDERLINE 0x08u
#define AG_STYLE_BLINK 0x10u
#define AG_STYLE_INVERSE 0x20u

#define AG_BLANK_CHAR ' '

/* A public cell: [char, foreground, style, frame]. */
struct agent_cell {
    uint8_t ch;
    uint8_t fg;
    uint8_t style;
    uint8_t frame;
};

/* The sanitizer's synthetic input.  It contains ONLY the candidate character,
 * the candidate basic foreground/frame slot, and already reduced display
 * booleans.  Never a glyph number, tile, symbol index, pointer, or MG mask.
 * The engine bridge computes these candidates; the engine-free sanitizer
 * tests normalization and precedence. */
struct agent_render_input {
    uint8_t ch;
    uint8_t fg;
    uint8_t frame;
    bool pet_attr;      /* pet-highlight candidate (map context) */
    bool pile_attr;     /* pile-highlight candidate (map context) */
    bool detected;      /* detection/inverse candidate (map context) */
    bool bw_inverse;    /* black-and-white inverse candidate (map context) */
    bool female_wizard; /* wizard-only reason; always dropped */
};

/* A caller-owned text buffer.  agent_* functions fill buf and set len. */
struct agent_text {
    char *buf;
    size_t len;
    size_t cap;
};

/* ---- outstanding request ---- */
enum agent_need_kind {
    AG_NEED_NONE = 0,
    AG_NEED_COMMAND,
    AG_NEED_KEY,
    AG_NEED_DIRECTION,
    AG_NEED_POSITION,
    AG_NEED_YN,
    AG_NEED_LINE,
    AG_NEED_EXTCMD,
    AG_NEED_MENU,
    AG_NEED_ACK
};

struct agent_need {
    enum agent_need_kind kind;
    uint64_t id;
    const char *prompt;    /* nullable */
    int x0, y0, x1, y1;    /* AG_NEED_POSITION legal bounds */
    const char *choices;   /* AG_NEED_YN visible choices, or NULL */
    int def;               /* AG_NEED_YN default byte, or 0 */
    bool numeric;          /* AG_NEED_YN numeric affordance displayed */
    int max;               /* AG_NEED_LINE/EXTCMD max UTF-8 bytes */
    const char *menu;      /* AG_NEED_MENU "mN" */
    int mode;              /* AG_NEED_MENU: an agent_menu_mode value */
    const char *content;   /* "cN" */
    int pages;
};

/* ---- parsed action ---- */
enum agent_action_kind {
    AG_ACT_NONE = 0,
    AG_ACT_KEY,
    AG_ACT_TEXT,
    AG_ACT_POSITION,
    AG_ACT_YN,
    AG_ACT_MENU,
    AG_ACT_CANCEL,
    AG_ACT_ACK
};

struct agent_commit_row {
    long r;      /* 1-based public row id */
    long count;  /* -1 or a positive count */
};

struct agent_action {
    enum agent_action_kind kind;
    uint64_t id;             /* request id the action answers */
    bool has_seq;
    uint64_t seq;            /* durable version acknowledged */
    uint8_t key;             /* AG_ACT_KEY / AG_ACT_YN byte */
    char text[AG_LINE_INPUT_MAX + 1]; /* AG_ACT_TEXT */
    int px, py, pmod;        /* AG_ACT_POSITION; mod is frozen to 0 */
    long yn_count;
    bool has_count;
    char menu[AG_ID_STR_MAX]; /* AG_ACT_MENU generation id "mN" */
    struct agent_commit_row *commit; /* caller-provided storage */
    size_t commit_cap;
    size_t ncommit;
    enum agent_invalid_code code; /* why a parse failed */
    bool replay;             /* retained-response replay; do not execute */
};

/* ---- durable presentation view (fixture-scoped fixed bounds) ---- */
struct agent_status {
    const char *name;
    const char *text;
    uint8_t color;
    uint8_t style;
};

struct agent_cond {
    const char *text;
    uint8_t color;
    uint8_t style;
};

struct agent_msg {
    uint64_t e;
    const char *text;
    uint8_t style;
};

struct agent_window {
    const char *w;         /* "wN" */
    int kind;              /* 0 = text, 1 = menu */
    const char *title;
    int mode;              /* menu mode, else 0 */
    const char *content;   /* "cN" */
    int pages;
};

struct agent_view {
    bool full;                                  /* full snapshot */
    struct agent_cell pal[AG_VIEW_MAX_PALETTE]; /* id 0 is the blank tuple */
    size_t npal;
    uint16_t map[AG_MAP_ROWS][AG_MAP_COLS];     /* palette ids, row-major */
    bool has_cursor;
    int cur_x, cur_y;
    struct agent_status status[AG_VIEW_MAX_STATUS];
    size_t nstatus;
    struct agent_cond cond[AG_VIEW_MAX_COND];
    size_t ncond;
    /* The message/history arrays are sized to the retained content (complete
     * up to the frozen byte limit), so a boundary can publish any number of
     * ordered events.  The arrays are owned by the caller that built the view
     * and borrowed by agent_commit(); a view with n > 0 must have a non-NULL
     * pointer. */
    struct agent_msg *msg;
    size_t nmsg;
    struct agent_msg *hist;
    size_t nhist;
    struct agent_window windows[AG_VIEW_MAX_WINDOWS];
    size_t nwindows;
};

/* Public color wire name for a slot, or NULL if out of range. */
const char *agent_color_name(uint8_t slot);

#endif /* AGENT_TYPES_H */
