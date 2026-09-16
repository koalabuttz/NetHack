/* test_menu.c -- the complete 5.6 menu contract, engine-free.
 *
 * Covers selector-zero selectable rows, headings, duplicate visible text,
 * preselected PICK_ANY, counts (-1/positive/zero/overflow), empty versus
 * cancel, PICK_NONE/ONE/ANY, rejection of group/bulk fields, and the
 * then-current selection state a repeated selection starts from.
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

    /* ---- reverse-order submission normalizes to insertion order ---- */
    m = make_menu(AG_MENU_ANY, 4);
    memset(&a, 0, sizeof a);
    commit[0].r = 4;
    commit[0].count = 1;
    commit[1].r = 3;
    commit[1].count = 2;
    commit[2].r = 2;
    commit[2].count = -1;
    a.rows = commit;
    a.nrows = 3;
    CHECK(agent_menu_validate(&m, &a, &sel) == AG_OK);
    CHECK(sel.nrows == 3);
    CHECK(sel.rows[0].r == 2 && sel.rows[0].count == -1);
    CHECK(sel.rows[1].r == 3 && sel.rows[1].count == 2);
    CHECK(sel.rows[2].r == 4 && sel.rows[2].count == 1);

    /* ---- selector-zero selectable row beyond the first protocol page ----
     * A declared page boundary is a presentation slice, not an id boundary:
     * row ids stay authoritative across pages, so a selection naming a row
     * that would fall on a later page is legal and a large menu validates. */
    {
        static struct agent_menu_row big[AG_PAGE_MAX_ROWS + 40];
        struct agent_menu bm;
        size_t i;

        for (i = 0; i < sizeof big / sizeof big[0]; ++i) {
            memset(&big[i], 0, sizeof big[i]);
            big[i].r = (long) i + 1;
            snprintf(big[i].text, sizeof big[i].text, "row %lu",
                     (unsigned long) (i + 1));
            big[i].selectable = true;
            /* row 140 carries no accelerator (selector zero) yet stays
             * selectable, and it lies on the second page */
            big[i].key = (i == AG_PAGE_MAX_ROWS + 11)
                             ? 0 : (int) ('a' + (i % 26));
        }
        memset(&bm, 0, sizeof bm);
        bm.id = "m9";
        bm.mode = AG_MENU_ANY;
        bm.rows = big;
        bm.nrows = sizeof big / sizeof big[0];
        bm.cap = bm.nrows;
        CHECK(agent_menu_check(&bm) == AG_OK);
        memset(&a, 0, sizeof a);
        commit[0].r = AG_PAGE_MAX_ROWS + 12; /* second page, selector zero */
        commit[0].count = -1;
        a.rows = commit;
        a.nrows = 1;
        CHECK(agent_menu_validate(&bm, &a, &sel) == AG_OK && sel.nrows == 1);
        CHECK(sel.rows[0].r == AG_PAGE_MAX_ROWS + 12);
        /* an id past the last row is rejected */
        commit[0].r = (long) bm.nrows + 1;
        CHECK(agent_menu_validate(&bm, &a, &sel) == AG_BAD_INPUT);
    }

    /* ---- SKIPINVERT row: explicit-id selection is legal in v1 ---------- */
    {
        struct agent_menu_row srow[3];
        struct agent_menu sm;

        /* row 2 is a native MENU_ITEMFLAGS_SKIPINVERT item.  Version 1
         * publishes it as an ordinary selectable row (there is no public
         * invert affordance) and requires explicit row-id selection; the
         * group/invert/bulk shapes below stay rejected. */
        memset(srow, 0, sizeof srow);
        srow[0].r = 1;
        snprintf(srow[0].text, sizeof srow[0].text, "heading");
        srow[1].r = 2;
        snprintf(srow[1].text, sizeof srow[1].text, "skipinvert item");
        srow[1].selectable = true;
        srow[1].key = 'i';
        srow[1].group = ')';
        srow[2].r = 3;
        snprintf(srow[2].text, sizeof srow[2].text, "plain item");
        srow[2].selectable = true;
        srow[2].key = 'p';
        srow[2].group = ')';
        memset(&sm, 0, sizeof sm);
        sm.id = "m1";
        sm.mode = AG_MENU_ANY;
        sm.rows = srow;
        sm.nrows = 3;
        sm.cap = 3;
        CHECK(agent_menu_check(&sm) == AG_OK);
        memset(&a, 0, sizeof a);
        commit[0].r = 2;
        commit[0].count = -1;
        a.rows = commit;
        a.nrows = 1;
        CHECK(agent_menu_validate(&sm, &a, &sel) == AG_OK && sel.result == 1);
        /* the advisory group accelerator never authorizes a bulk operation */
        a.group_op = true;
        CHECK(agent_menu_validate(&sm, &a, &sel) == AG_BAD_INPUT);
        CHECK(sel.code == AG_INV_SCHEMA);
        a.group_op = false;
        a.invert = true;
        CHECK(agent_menu_validate(&sm, &a, &sel) == AG_BAD_INPUT);
        a.invert = false;
        a.bulk = true;
        CHECK(agent_menu_validate(&sm, &a, &sel) == AG_BAD_INPUT);
    }

    /* ---- duplicate visible text is never an identity ------------------ */
    {
        struct agent_menu_row drow[2];
        struct agent_menu dm;

        memset(drow, 0, sizeof drow);
        drow[0].r = 1;
        snprintf(drow[0].text, sizeof drow[0].text, "an apple");
        drow[0].selectable = true;
        drow[0].key = 'a';
        drow[1].r = 2;
        snprintf(drow[1].text, sizeof drow[1].text, "an apple");
        drow[1].selectable = true;
        drow[1].key = 'b';
        memset(&dm, 0, sizeof dm);
        dm.id = "m2";
        dm.mode = AG_MENU_ANY;
        dm.rows = drow;
        dm.nrows = 2;
        dm.cap = 2;
        CHECK(agent_menu_check(&dm) == AG_OK);
        /* two identical texts are distinct row ids: selecting both yields two
         * results in insertion order, and there is no select-by-label path
         * that could pick one of them arbitrarily. */
        memset(&a, 0, sizeof a);
        commit[0].r = 2;
        commit[0].count = -1;
        commit[1].r = 1;
        commit[1].count = -1;
        a.rows = commit;
        a.nrows = 2;
        CHECK(agent_menu_validate(&dm, &a, &sel) == AG_OK && sel.nrows == 2);
        CHECK(sel.rows[0].r == 1 && sel.rows[1].r == 2);
        /* an id that does not exist is rejected, never matched by text */
        commit[0].r = 3;
        a.nrows = 1;
        CHECK(agent_menu_validate(&dm, &a, &sel) == AG_BAD_INPUT);
        CHECK(sel.code == AG_INV_RANGE);
    }

    /* ---- structural row invariants ---- */
    {
        struct agent_menu_row r2[3];

        /* a well-formed menu passes the structural check */
        memset(r2, 0, sizeof r2);
        r2[0].r = 1;
        snprintf(r2[0].text, sizeof r2[0].text, "heading");
        r2[1].r = 2;
        snprintf(r2[1].text, sizeof r2[1].text, "a");
        r2[1].selectable = true;
        r2[1].has_initial = true;
        r2[1].initial = 5; /* a positive initial count is preserved */
        r2[2].r = 3;
        snprintf(r2[2].text, sizeof r2[2].text, "b");
        r2[2].selectable = true;
        r2[2].has_initial = true;
        r2[2].initial = -1; /* native all/default */
        r2[2].color = AG_COL_GRAY;
        m = make_menu(AG_MENU_ANY, 3);
        m.rows = r2;
        CHECK(agent_menu_check(&m) == AG_OK);

        /* an initial value of zero is neither null nor a legal count */
        r2[1].initial = 0;
        CHECK(agent_menu_check(&m) == AG_BAD_INPUT);
        r2[1].initial = 5;

        /* "no initial" is expressed by has_initial == false */
        r2[1].has_initial = false;
        r2[1].initial = 0;
        CHECK(agent_menu_check(&m) == AG_OK);
        r2[1].has_initial = true;
        r2[1].initial = 5;

        /* row ids must be sequential in insertion order */
        r2[2].r = 4;
        CHECK(agent_menu_check(&m) == AG_BAD_INPUT);
        r2[2].r = 3;

        /* an out-of-range advisory accelerator is rejected */
        r2[1].key = 300;
        CHECK(agent_menu_check(&m) == AG_BAD_INPUT);
        r2[1].key = 0;

        /* a published icon must itself be publishable */
        r2[1].has_icon = true;
        r2[1].icon.ch = 0x80;
        CHECK(agent_menu_check(&m) == AG_BAD_INPUT);
        r2[1].icon.ch = ')';
        r2[1].icon.fg = AG_COL_GRAY;
        r2[1].icon.frame = AG_COL_NONE;
        CHECK(agent_menu_check(&m) == AG_OK);
    }

    /* ---- then-current selection state for repeated selections ---- */
    {
        struct agent_menu pm;
        struct agent_selection_row pr[4];
        struct agent_selection psel;

        /* a heading, two selectable rows, and a preselection on row 3 */
        mkrow(0, "heading", false, 0, 0, false, 0);
        mkrow(1, "a", true, 'a', 0, false, 0);
        mkrow(2, "b", true, 'b', 0, true, -1);
        pm = make_menu(AG_MENU_ANY, 3);

        memset(&psel, 0, sizeof psel);
        memset(pr, 0, sizeof pr);
        psel.rows = pr;
        psel.cap = sizeof pr / sizeof pr[0];

        /* an accepted set replaces the state wholesale: the omitted row 3
         * becomes unselected and row 2 takes the explicit positive count,
         * which is the only way a positive initial count can arise */
        pr[0].r = 2;
        pr[0].count = 5;
        psel.nrows = 1;
        psel.result = 1;
        CHECK(agent_menu_apply_selection(&pm, &psel) == AG_OK);
        CHECK(rows[0].has_initial == false); /* heading never selected */
        CHECK(rows[1].has_initial == true && rows[1].initial == 5);
        CHECK(rows[2].has_initial == false && rows[2].initial == 0);
        /* the folded state is itself a legal published menu */
        CHECK(agent_menu_check(&pm) == AG_OK);

        /* an accepted EMPTY set clears every selectable initial */
        psel.nrows = 0;
        psel.result = 0;
        CHECK(agent_menu_apply_selection(&pm, &psel) == AG_OK);
        CHECK(rows[1].has_initial == false && rows[1].initial == 0);
        CHECK(rows[2].has_initial == false && rows[2].initial == 0);

        /* restore a native-default selection, then prove cancellation does
         * not move the state */
        pr[0].r = 3;
        pr[0].count = -1;
        psel.nrows = 1;
        psel.result = 1;
        CHECK(agent_menu_apply_selection(&pm, &psel) == AG_OK);
        psel.result = -1; /* cancel: not a selection */
        psel.nrows = 0;
        CHECK(agent_menu_apply_selection(&pm, &psel) == AG_OK);
        CHECK(rows[1].has_initial == false);
        CHECK(rows[2].has_initial == true && rows[2].initial == -1);

        /* a rejected set is never applied: an unselectable row, a zero count
         * and an out-of-range row each leave the state exactly as it was */
        pr[0].r = 1;
        pr[0].count = -1;
        psel.nrows = 1;
        psel.result = 1;
        CHECK(agent_menu_apply_selection(&pm, &psel) == AG_BAD_INPUT);
        pr[0].r = 2;
        pr[0].count = 0;
        CHECK(agent_menu_apply_selection(&pm, &psel) == AG_BAD_INPUT);
        pr[0].r = 4;
        pr[0].count = -1;
        CHECK(agent_menu_apply_selection(&pm, &psel) == AG_BAD_INPUT);
        CHECK(rows[1].has_initial == false && rows[1].initial == 0);
        CHECK(rows[2].has_initial == true && rows[2].initial == -1);
    }

    if (failures) {
        printf("test_menu: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_menu: ok\n");
    return 0;
}
