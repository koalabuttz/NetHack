/* test_menu.c -- the complete 5.6 menu contract, engine-free.
 *
 * Covers selector-zero selectable rows, headings, duplicate visible text,
 * preselected PICK_ANY, counts (-1/positive/zero/overflow), empty versus
 * cancel, PICK_NONE/ONE/ANY, and rejection of group/bulk fields.
 */

#include <stdio.h>
#include <string.h>

#include "agent_menu.h"

static int failures;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
            ++failures;                                                   \
        }                                                                 \
    } while (0)

static struct agent_menu_row rows[8];

static void
mkrow(int i, const char *text, bool selectable, int key, int group,
      bool has_initial, long initial)
{
    memset(&rows[i], 0, sizeof rows[i]);
    rows[i].r = i + 1;
    snprintf(rows[i].text, sizeof rows[i].text, "%s", text);
    rows[i].selectable = selectable;
    rows[i].key = key;
    rows[i].group = group;
    rows[i].has_initial = has_initial;
    rows[i].initial = initial;
}

static struct agent_menu
make_menu(enum agent_menu_mode mode, size_t nrows)
{
    struct agent_menu m;

    memset(&m, 0, sizeof m);
    m.id = "m1";
    m.mode = mode;
    m.rows = rows;
    m.nrows = nrows;
    m.cap = sizeof rows / sizeof rows[0];
    return m;
}

int
main(void)
{
    struct agent_menu m;
    struct agent_menu_answer a;
    struct agent_selection sel;
    struct agent_selection_row storage[8];
    struct agent_commit_row commit[8];

    /* 0: heading (unselectable)  1: a dagger (preselected, -1, key a)
       2: a dagger (duplicate text, key b)  3: b dagger (selector zero) */
    mkrow(0, "Weapons", false, 0, 0, false, 0);
    mkrow(1, "a dagger", true, 'a', ')', true, -1);
    mkrow(2, "a dagger", true, 'b', ')', false, 0);
    mkrow(3, "b dagger", true, 0, 0, false, 0);

    memset(&sel, 0, sizeof sel);
    sel.rows = storage;
    sel.cap = sizeof storage / sizeof storage[0];

    /* ---- mode parsing ---- */
    {
        enum agent_menu_mode mm;

        CHECK(agent_menu_mode_parse("none", &mm) && mm == AG_MENU_NONE);
        CHECK(agent_menu_mode_parse("one", &mm) && mm == AG_MENU_ONE);
        CHECK(agent_menu_mode_parse("any", &mm) && mm == AG_MENU_ANY);
        CHECK(!agent_menu_mode_parse("pickany", &mm));
        CHECK(strcmp(agent_menu_mode_name(AG_MENU_ANY), "any") == 0);
    }

    /* ---- selector-zero selectable row is reachable by row id ---- */
    m = make_menu(AG_MENU_ANY, 4);
    memset(&a, 0, sizeof a);
    commit[0].r = 4; /* selector 0 -> encoded as null, still selectable */
    commit[0].count = 1;
    a.rows = commit;
    a.nrows = 1;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK);
    CHECK(sel.result == 1);
    CHECK(sel.nrows == 1 && sel.rows[0].r == 4);

    /* ---- headings are not selectable ---- */
    memset(&a, 0, sizeof a);
    commit[0].r = 1; /* the heading */
    commit[0].count = -1;
    a.rows = commit;
    a.nrows = 1;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    CHECK(sel.code == AG_INV_KIND);

    /* ---- duplicate visible text: ids are authoritative ---- */
    memset(&a, 0, sizeof a);
    commit[0].r = 2;
    commit[0].count = 3;
    commit[1].r = 3;
    commit[1].count = -1;
    a.rows = commit;
    a.nrows = 2;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK);
    CHECK(sel.nrows == 2);
    /* normalized to menu insertion order */
    CHECK(sel.rows[0].r == 2 && sel.rows[1].r == 3);

    /* ---- preselected PICK_ANY: empty commit returns 0, not the
       preselection ---- */
    memset(&a, 0, sizeof a);
    a.rows = commit;
    a.nrows = 0;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK);
    CHECK(sel.result == 0);
    CHECK(sel.nrows == 0);

    /* ---- counts: -1 and positive legal; zero, -2, overflow rejected ---- */
    {
        struct agent_commit_row c[1];
        struct agent_menu_answer aa;

        memset(&aa, 0, sizeof aa);
        aa.rows = c;
        aa.nrows = 1;
        c[0].r = 2;
        c[0].count = -1;
        CHECK(agent_menu_validate(&m, &aa, &sel) == AG_OK);
        c[0].count = 7;
        CHECK(agent_menu_validate(&m, &aa, &sel) == AG_OK && sel.result == 1);
        c[0].count = 0;
        CHECK(agent_menu_validate(&m, &aa, &sel) == AG_BAD_INPUT);
        CHECK(sel.code == AG_INV_RANGE);
        c[0].count = -2;
        CHECK(agent_menu_validate(&m, &aa, &sel) == AG_BAD_INPUT);
        c[0].count = (long) AG_COUNT_MAX + 1;
        CHECK(agent_menu_validate(&m, &aa, &sel) == AG_LIMIT);
        CHECK(sel.code == AG_INV_RANGE);
        c[0].r = 99; /* not in this generation */
        c[0].count = 1;
        CHECK(agent_menu_validate(&m, &aa, &sel) == AG_BAD_INPUT);
    }

    /* ---- duplicate row in one commit ---- */
    memset(&a, 0, sizeof a);
    commit[0].r = 2;
    commit[0].count = 1;
    commit[1].r = 2;
    commit[1].count = -1;
    a.rows = commit;
    a.nrows = 2;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    CHECK(sel.code == AG_INV_KIND);

    /* ---- empty versus cancel ---- */
    memset(&a, 0, sizeof a);
    a.rows = commit;
    a.nrows = 0;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK && sel.result == 0);
    memset(&a, 0, sizeof a);
    a.cancel = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK && sel.result == -1);
    CHECK(sel.nrows == 0);
    /* cancel combined with anything else is invalid */
    memset(&a, 0, sizeof a);
    a.cancel = true;
    a.ack = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);

    /* ---- PICK_NONE: acknowledgement or empty commit only ---- */
    m = make_menu(AG_MENU_NONE, 4);
    memset(&a, 0, sizeof a);
    a.ack = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK && sel.result == 0);
    memset(&a, 0, sizeof a);
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK && sel.result == 0);
    memset(&a, 0, sizeof a);
    commit[0].r = 2;
    commit[0].count = 1;
    a.rows = commit;
    a.nrows = 1;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    CHECK(sel.code == AG_INV_KIND);

    /* ---- PICK_ONE: at most one selectable row ---- */
    m = make_menu(AG_MENU_ONE, 4);
    memset(&a, 0, sizeof a);
    commit[0].r = 2;
    commit[0].count = -1;
    a.rows = commit;
    a.nrows = 1;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK && sel.result == 1);
    memset(&a, 0, sizeof a);
    commit[1].r = 3;
    commit[1].count = 1;
    a.rows = commit;
    a.nrows = 2;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    /* an acknowledgement is only valid for a display-only menu */
    memset(&a, 0, sizeof a);
    a.ack = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    CHECK(sel.code == AG_INV_KIND);

    /* ---- PICK_ANY: many rows ---- */
    m = make_menu(AG_MENU_ANY, 4);
    memset(&a, 0, sizeof a);
    commit[0].r = 2;
    commit[0].count = 1;
    commit[1].r = 3;
    commit[1].count = 2;
    commit[2].r = 4;
    commit[2].count = -1;
    a.rows = commit;
    a.nrows = 3;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK && sel.result == 3);
    CHECK(sel.rows[0].r == 2 && sel.rows[1].r == 3 && sel.rows[2].r == 4);

    /* ---- v1 omits group/bulk/invert/select-all/raw-key operations ---- */
    m = make_menu(AG_MENU_ANY, 4);
    memset(&a, 0, sizeof a);
    commit[0].r = 2;
    commit[0].count = 1;
    a.rows = commit;
    a.nrows = 1;
    a.bulk = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    CHECK(sel.code == AG_INV_SCHEMA);
    a.bulk = false;
    a.group_op = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    a.group_op = false;
    a.selectall = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    a.selectall = false;
    a.invert = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);
    a.invert = false;
    a.raw_key = true;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_BAD_INPUT);

    /* ---- caller storage too small fails closed, not silently ---- */
    {
        struct agent_selection small;
        struct agent_selection_row one;

        memset(&a, 0, sizeof a);
        commit[0].r = 2;
        commit[0].count = 1;
        commit[1].r = 3;
        commit[1].count = 1;
        a.rows = commit;
        a.nrows = 2;
        small.rows = &one;
        small.cap = 1;
        CHECK(agent_menu_validate(&m, &a, &small) == AG_LIMIT);
    }

    if (failures) {
        printf("test_menu: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_menu: ok\n");
    return 0;
}
