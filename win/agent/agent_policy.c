/* agent_policy.c -- agent runtime policy state.
 *
 * The option gate and the profile application live in src/options.c (they
 * need the option table and its private request enum); the command policy
 * lives in src/cmd.c (it needs the command table and cmd.c's file-static
 * handlers).  This file holds the policy state the other subsystems consult:
 * whether a sysconf directive is recognised, and whether the publication gate
 * is open.
 *
 * Nothing here publishes a player-facing byte.
 */

#include "hack.h"

#include "winagent.h"

/* ---- sysconf directive classification ---- */

/* The trusted sysconf may carry ONLY the statements the shipped agent
 * configuration actually needs, each pinned to the exact value the frozen
 * profile expects.  This is a positive allowlist of frozen statements, not a
 * classification of directive names: a directive that is merely "known" is
 * still refused.
 *
 * Name classification cannot make the excluded directives safe.  They are
 * direct config-statement functions that mutate sysopt (src/cfgfiles.c), so
 * the option gate never sees them; a path-bearing statement would let the
 * sysconf point play at a shared playground, and an execution, tracing,
 * reporting, sound, mail, hook, or logging statement would activate an
 * external facility the profile forbids (plan section 4 step 6: reject
 * unknown/external directives).  Only the five frozen statements below are
 * accepted; anything else -- and any altered value on an accepted name --
 * is a rejection the caller turns into a private termination. */
#define AGENT_SYSCONF_NAME_MAX 64
#define AGENT_SYSCONF_VALUE_MAX 64

struct agent_sysconf_stmt {
    const char *name;  /* directive name, lower case */
    const char *value; /* exact accepted value; "" means "no value" */
};

static const struct agent_sysconf_stmt agent_sysconf_allowed[] = {
    { "wizards", "" },
    { "explorers", "" },
    { "genericusers", "agent" },
    { "maxplayers", "1" },
    { "max_reroll_rate", "0" },
};

/* Split a mungspaced config statement into its directive name and value and
 * report whether it is exactly one of the frozen statements.  The engine --
 * not this function -- decides what a rejection does. */
boolean
agent_policy_sysconf_directive(const char *stmt)
{
    char name[AGENT_SYSCONF_NAME_MAX], value[AGENT_SYSCONF_VALUE_MAX];
    const char *s;
    size_t i, d, vlen;

    if (!agent_mode() || !stmt)
        return TRUE; /* the human path is unchanged */
    while (*stmt == ' ' || *stmt == '\t')
        ++stmt;
    if (*stmt == '#' || *stmt == '\0')
        return TRUE; /* comment or blank line */

    /* the directive name ends at the first delimiter or space */
    for (d = 0; stmt[d] && stmt[d] != '=' && stmt[d] != ':' && stmt[d] != ' '
                && stmt[d] != '\t'; ++d)
        ;
    if (d == 0 || d >= sizeof name)
        return FALSE;
    for (i = 0; i < d; ++i)
        name[i] = (char) lowc((uchar) stmt[i]);
    name[d] = '\0';

    /* skip the delimiter and any whitespace around it */
    s = stmt + d;
    while (*s == ' ' || *s == '\t')
        ++s;
    if (*s == '=' || *s == ':')
        ++s;
    while (*s == ' ' || *s == '\t')
        ++s;
    vlen = strlen(s);
    if (vlen >= sizeof value)
        return FALSE; /* an accepted statement has a short frozen value */
    for (i = 0; i < vlen; ++i)
        value[i] = s[i];
    value[vlen] = '\0';

    for (i = 0; i < SIZE(agent_sysconf_allowed); ++i)
        if (strcmp(name, agent_sysconf_allowed[i].name) == 0)
            return (boolean) (strcmp(value, agent_sysconf_allowed[i].value)
                              == 0);
    return FALSE;
}

/* ---- publication gate ---- */

static boolean agent_publication = FALSE;

/* Set only by agent_apply_profile() after the frozen pins have been applied
 * and their effective values verified.  It is the precondition the gate
 * checks, so no start sequence can publish before profile validation. */
static boolean agent_profile_validated = FALSE;

void
agent_policy_profile_validated(void)
{
    agent_profile_validated = TRUE;
    agent_publication = FALSE; /* still closed until the restore decision */
}

/* The gate opens only after profile validation in trusted initialization and
 * the decision that no restore is pending.  A missing validation is a
 * fail-closed condition, not a warning: the worker must not publish with an
 * unproven profile, so it terminates privately instead. */
void
agent_publication_ready(void)
{
    if (!agent_mode())
        return;
    if (!agent_profile_validated)
        agent_private_fatal("publication before profile validation");
    agent_publication = TRUE;
}

/* Any restore attempt, or a failed profile validation, closes the gate. */
void
agent_publication_close(void)
{
    agent_publication = FALSE;
}

boolean
agent_publication_open(void)
{
    return agent_publication;
}

/*agent_policy.c*/
