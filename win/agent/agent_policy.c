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

/* The trusted sysconf may only carry directives the agent profile classifies.
 * An unrecognised statement is rejected rather than silently ignored: the
 * profile is frozen, so a new directive has to be classified deliberately
 * instead of taking effect by being unknown.
 *
 * Path-bearing directives are listed because the launcher supplies the
 * writable locations; a sysconf is never allowed to point play at a shared
 * playground. */
static const char *const agent_sysconf_directives[] = {
    "wizards", "explorers", "genericusers", "maxplayers", "max_reroll_rate",
    "support", "check_save_uid", "shell", "pager", "editor", "mail",
    "crashreporturl", "panicreport", "panictrace_gdb", "panictrace_libc",
    "gdbpath", "greppath", "recordfile", "logfile", "xlogfile", "sysconf",
    "hackdir", "nethackdir", "portable_device_paths", "bones", "sounds",
    "livelog", "wizkit", "max_score_age", "max_score_age_when"
};

boolean
agent_policy_sysconf_directive(const char *stmt)
{
    char buf[64];
    size_t i, n;

    if (!agent_mode() || !stmt)
        return TRUE; /* the human path is unchanged */
    while (*stmt == ' ' || *stmt == '\t')
        ++stmt;
    if (*stmt == '#' || *stmt == '\0')
        return TRUE; /* comment or blank line */
    for (n = 0; stmt[n] && stmt[n] != '=' && stmt[n] != ' ' && stmt[n] != '\t'
                && n < sizeof buf - 1; ++n)
        buf[n] = stmt[n];
    buf[n] = '\0';
    if (n == 0)
        return TRUE;
    for (i = 0; i < n; ++i)
        buf[i] = (char) lowc((uchar) buf[i]);
    for (i = 0; i < SIZE(agent_sysconf_directives); ++i)
        if (strcmp(buf, agent_sysconf_directives[i]) == 0)
            return TRUE;
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
