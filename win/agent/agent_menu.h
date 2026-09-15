/* agent_menu.h -- standalone public menu model and final-set validation.
 *
 * Native identifiers never appear here.  A row's selectability is a copied
 * public boolean and a selection result contains only public row id / count
 * pairs.  This is the whole of the 5.6 menu contract that can be validated
 * without an engine.
 */

#ifndef AGENT_MENU_H
#define AGENT_MENU_H

#include "agent_types.h"

enum agent_menu_mode {
    AG_MENU_NONE = 0, /* display-only: PICK_NONE */
    AG_MENU_ONE = 1,  /* PICK_ONE */
    AG_MENU_ANY = 2   /* PICK_ANY */
};

#define AG_MENU_TEXT_MAX 256

struct agent_menu_row {
    long r;                 /* 1-based insertion order, includes headings */
    char text[AG_MENU_TEXT_MAX];
    bool selectable;
    int key;                   /* advisory accelerator byte, or 0 for null */
    int group;                 /* advisory group byte, or 0 for null */
    bool has_initial;          /* false = initial:null (unselected) */
    long initial;              /* -1 = native all/default, else explicit */
    uint8_t style;
    uint8_t color;
    bool has_icon;             /* false = null icon */
    struct agent_cell icon;
};

struct agent_menu {
    const char *id;            /* "mN", public generation id */
    enum agent_menu_mode mode;
    struct agent_menu_row *rows;
    size_t nrows;
    size_t cap;
};

struct agent_menu_answer {
    const struct agent_commit_row *rows; /* [r, count] in submission order */
    size_t nrows;
    bool cancel;
    bool ack;
    /* v1 deliberately omits these operations; representing them here means a
     * caller that supplies them is rejected rather than silently ignored. */
    bool bulk;
    bool group_op;
    bool selectall;
    bool invert;
    bool raw_key;
};

struct agent_selection_row {
    long r;
    long count;
};

struct agent_selection {
    struct agent_selection_row *rows; /* caller-provided storage */
    size_t cap;
    size_t nrows;
    long result;              /* -1 cancel, 0 empty, else number of rows */
    enum agent_invalid_code code;
};

/* Validate a final-set answer against a constructed menu.
 *
 * On success returns AG_OK and fills out with rows normalized to menu
 * insertion order.  On a validation failure returns AG_BAD_INPUT (or AG_LIMIT
 * for a bound overflow) and leaves the outstanding request untouched;
 * out->code carries the public code.  Cancellation yields no rows.
 */
enum agent_result agent_menu_validate(const struct agent_menu *m,
                                      const struct agent_menu_answer *a,
                                      struct agent_selection *out);

/* Validate the structural invariants of a constructed menu itself: sequential
 * 1-based row ids, in-range advisory accelerators, and an initial value
 * null, -1, or strictly positive.  A row whose initial value is 0 is invalid
 * because 0 is neither "unselected" (null) nor a legal native count.
 */
enum agent_result agent_menu_check(const struct agent_menu *m);

/* Mode name helpers used by the encoder. */
const char *agent_menu_mode_name(enum agent_menu_mode mode);
bool agent_menu_mode_parse(const char *s, enum agent_menu_mode *out);

#endif /* AGENT_MENU_H */
