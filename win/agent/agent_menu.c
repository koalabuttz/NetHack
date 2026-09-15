/* agent_menu.c -- standalone public menu model and final-set validation. */

#include "agent_menu.h"

const char *
agent_menu_mode_name(enum agent_menu_mode mode)
{
    switch (mode) {
    case AG_MENU_NONE:
        return "none";
    case AG_MENU_ONE:
        return "one";
    case AG_MENU_ANY:
        return "any";
    default:
        break;
    }
    return (const char *) 0;
}

bool
agent_menu_mode_parse(const char *s, enum agent_menu_mode *out)
{
    if (!s || !out)
        return false;
    if (s[0] == 'n' && s[1] == 'o' && s[2] == 'n' && s[3] == 'e'
        && s[4] == '\0') {
        *out = AG_MENU_NONE;
        return true;
    }
    if (s[0] == 'o' && s[1] == 'n' && s[2] == 'e' && s[3] == '\0') {
        *out = AG_MENU_ONE;
        return true;
    }
    if (s[0] == 'a' && s[1] == 'n' && s[2] == 'y' && s[3] == '\0') {
        *out = AG_MENU_ANY;
        return true;
    }
    return false;
}

static void
sel_fail(struct agent_selection *out, enum agent_invalid_code code)
{
    out->nrows = 0;
    out->result = 0;
    out->code = code;
}

/* Insertion sort by row id: row ids are insertion order, so this normalizes
 * the submitted final set to menu order rather than submission order. */
static void
sel_sort(struct agent_selection *out)
{
    size_t i, j;

    for (i = 1; i < out->nrows; ++i) {
        struct agent_selection_row key = out->rows[i];

        j = i;
        while (j > 0 && out->rows[j - 1].r > key.r) {
            out->rows[j] = out->rows[j - 1];
            --j;
        }
        out->rows[j] = key;
    }
}

enum agent_result
agent_menu_validate(const struct agent_menu *m,
                    const struct agent_menu_answer *a,
                    struct agent_selection *out)
{
    size_t i, j;

    if (!m || !a || !out)
        return AG_INTERNAL;

    out->nrows = 0;
    out->result = 0;
    out->code = AG_INV_NONE;

    /* version 1 omits group/bulk/invert/select-all and raw menu keys */
    if (a->bulk || a->group_op || a->selectall || a->invert || a->raw_key) {
        sel_fail(out, AG_INV_SCHEMA);
        return AG_BAD_INPUT;
    }
    /* cancel is exclusive with everything else */
    if (a->cancel) {
        if (a->ack || a->nrows) {
            sel_fail(out, AG_INV_SCHEMA);
            return AG_BAD_INPUT;
        }
        out->result = -1;
        return AG_OK;
    }
    if (a->ack) {
        /* acknowledgement is only a display-only menu operation */
        if (a->nrows) {
            sel_fail(out, AG_INV_SCHEMA);
            return AG_BAD_INPUT;
        }
        if (m->mode != AG_MENU_NONE) {
            sel_fail(out, AG_INV_KIND);
            return AG_BAD_INPUT;
        }
        out->result = 0;
        return AG_OK;
    }
    /* empty commit: legal for every mode; native count 0, no results */
    if (a->nrows == 0) {
        out->result = 0;
        return AG_OK;
    }
    /* PICK_NONE is display-only; a nonempty selection is invalid */
    if (m->mode == AG_MENU_NONE) {
        sel_fail(out, AG_INV_KIND);
        return AG_BAD_INPUT;
    }
    if (a->nrows > AG_MAX_MENU_ROWS) {
        sel_fail(out, AG_INV_RANGE);
        return AG_LIMIT;
    }
    if (a->nrows > out->cap) {
        sel_fail(out, AG_INV_RANGE);
        return AG_LIMIT;
    }
    if ((size_t) a->nrows > m->nrows) {
        /* more selections than rows cannot avoid a duplicate or a bad id */
        sel_fail(out, AG_INV_RANGE);
        return AG_BAD_INPUT;
    }

    for (i = 0; i < a->nrows; ++i) {
        long r = a->rows[i].r;
        long count = a->rows[i].count;
        const struct agent_menu_row *row;

        if (r < 1 || (size_t) r > m->nrows) {
            sel_fail(out, AG_INV_RANGE);
            return AG_BAD_INPUT;
        }
        row = &m->rows[r - 1];
        if (!row->selectable) {
            sel_fail(out, AG_INV_KIND);
            return AG_BAD_INPUT;
        }
        /* -1 (native all/default) or a positive count; never 0 or < -1 */
        if (count == 0 || count < -1) {
            sel_fail(out, AG_INV_RANGE);
            return AG_BAD_INPUT;
        }
        if (count > AG_COUNT_MAX) {
            sel_fail(out, AG_INV_RANGE);
            return AG_LIMIT;
        }
        for (j = 0; j < out->nrows; ++j) {
            if (out->rows[j].r == r) {
                sel_fail(out, AG_INV_KIND);
                return AG_BAD_INPUT;
            }
        }
        if (m->mode == AG_MENU_ONE && out->nrows >= 1) {
            sel_fail(out, AG_INV_KIND);
            return AG_BAD_INPUT;
        }
        out->rows[out->nrows].r = r;
        out->rows[out->nrows].count = count;
        ++out->nrows;
    }

    sel_sort(out);
    out->result = (long) out->nrows;
    return AG_OK;
}
