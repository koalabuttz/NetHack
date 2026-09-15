/* agent_bootstrap.c -- trusted bootstrap latch and private diagnostics.
 *
 * Engine-facing but deliberately narrow: this file touches only OS resources
 * plus private state, so that it can run before early_init() has reset the
 * engine globals.  It must never write to stdout and must never call into the
 * port's player-facing presentation.
 */

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "winagent.h"
#include "agent_handshake.h"

#ifndef AG_AGENT_PROFILE
#define AG_AGENT_PROFILE "normal-ascii-color-v1"
#endif

#define AG_DIAG_MAX 2048

/* Process-lifetime, non-restorable agent latch.  Nothing in configuration,
 * the command line, or save/restore data can reach this storage. */
static int agent_latched = 0;
static int agent_latched_fd = -1;
static struct agent_handshake agent_hs;

static void
ag_write_private(const char *buf, size_t len)
{
    size_t off = 0;

    /* fd 2 is the private bounded sink the launcher installs.  It is never
     * the public channel. */
    while (off < len) {
        ssize_t n = write(STDERR_FILENO, buf + off, len - off);

        if (n <= 0) {
            if (n < 0 && errno == EINTR)
                continue;
            break;
        }
        off += (size_t) n;
    }
}

void
agent_private_diag(const char *msg)
{
    if (!msg)
        return;
    ag_write_private("agent: ", 7);
    ag_write_private(msg, strlen(msg));
    ag_write_private("\n", 1);
}

void
agent_private_fatal(const char *msg)
{
    /* Low level only: no exit_nhwindows(), no pline(), no panic(), no stdout.
     * The launcher alone reports terminal closure to the public channel. */
    agent_private_diag(msg);
    _exit(70);
}

void
agent_impossible_fatal(const char *fmt, va_list ap)
{
    char buf[AG_DIAG_MAX];

    buf[0] = '\0';
    (void) vsnprintf(buf, sizeof buf, fmt, ap);
    buf[sizeof buf - 1] = '\0';
    agent_private_fatal(buf);
}

int
agent_mode(void)
{
    return agent_latched;
}

int
agent_bootstrap_fd(void)
{
    return agent_latched ? agent_latched_fd : -1;
}

const char *
agent_trusted_data_root(void)
{
    return agent_latched ? agent_hs.data_root : (const char *) 0;
}

const char *
agent_trusted_writable_root(void)
{
    return agent_latched ? agent_hs.writable_root : (const char *) 0;
}

const char *
agent_trusted_config_root(void)
{
    return agent_latched ? agent_hs.config_root : (const char *) 0;
}

const char *
agent_trusted_sysconf(void)
{
    if (agent_latched && agent_hs.sysconf_path[0])
        return agent_hs.sysconf_path;
    return (const char *) 0;
}

/* Parse "--agent-fd=N" out of one argument, returning N or -1. */
static int
ag_parse_locator(const char *arg)
{
    static const char prefix[] = "--agent-fd=";
    const char *p;
    long v = 0;

    if (!arg || strncmp(arg, prefix, sizeof prefix - 1) != 0)
        return -1;
    p = arg + sizeof prefix - 1;
    if (*p < '0' || *p > '9')
        return -1;
    while (*p) {
        if (*p < '0' || *p > '9')
            return -1;
        v = v * 10 + (*p - '0');
        if (v > 1024)
            return -1;
        ++p;
    }
    return (int) v;
}

static int
ag_read_exact(int fd, void *buf, size_t len)
{
    size_t off = 0;

    while (off < len) {
        ssize_t n = read(fd, (char *) buf + off, len - off);

        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (n == 0)
            return -1; /* truncated handshake */
        off += (size_t) n;
    }
    return 0;
}

void
agent_bootstrap_probe(int *argc, char ***argvp)
{
    char **argv = *argvp;
    int i, fd = -1, locator_index = -1;
    struct agent_handshake hs;
    int flags;

    if (!argc || !argvp || !argv || *argc < 1)
        return;

    for (i = 1; i < *argc; ++i) {
        int n = ag_parse_locator(argv[i]);

        if (n >= 0) {
            fd = n;
            locator_index = i;
            break;
        }
    }
    if (locator_index < 0)
        return; /* combined build without a locator: the human path */

    if (ag_read_exact(fd, &hs, sizeof hs) != 0)
        agent_private_fatal("short or failed bootstrap handshake");
    if (hs.magic != AG_HS_MAGIC)
        agent_private_fatal("bad handshake magic");
    if (hs.version != AG_HS_VERSION)
        agent_private_fatal("unsupported handshake version");
    if (hs.reserved != 0u)
        agent_private_fatal("handshake reserved field not zero");
    if (hs.mode != AG_HS_MODE_NEW)
        agent_private_fatal("unsupported launch mode");
    hs.profile[AG_HS_PROFILE_MAX - 1] = '\0';
    if (strcmp(hs.profile, AG_AGENT_PROFILE) != 0)
        agent_private_fatal("unsupported rendering profile");
    hs.data_root[AG_HS_ROOT_MAX - 1] = '\0';
    hs.config_root[AG_HS_ROOT_MAX - 1] = '\0';
    hs.writable_root[AG_HS_ROOT_MAX - 1] = '\0';
    hs.sysconf_path[AG_HS_ROOT_MAX - 1] = '\0';
    if (hs.writable_root[0] != '/')
        agent_private_fatal("private writable root must be absolute");

    /* the descriptor stays owned by this process for the whole run */
    flags = fcntl(fd, F_GETFD);
    if (flags >= 0)
        (void) fcntl(fd, F_SETFD, flags & ~FD_CLOEXEC);

    agent_hs = hs;
    agent_latched_fd = fd;
    agent_latched = 1;

    /* consume the locator so ordinary argument parsing never sees it */
    for (i = locator_index; i < *argc - 1; ++i)
        argv[i] = argv[i + 1];
    --*argc;
    argv[*argc] = NULL;
}

void
agent_bootstrap_after_globals(void)
{
    struct stat st;

    if (!agent_latched)
        return;
    /* The launcher created the private tree; verify it rather than trusting
     * the record alone. */
    if (stat(agent_hs.writable_root, &st) != 0 || !S_ISDIR(st.st_mode))
        agent_private_fatal("private writable root is missing");
}

/* Wave A argument policy: a latched worker must not be able to choose its
 * frontend, configuration, symbol set, hooks, or crash reporting through
 * ordinary argument parsing.  The full setter gate network is Wave B. */
void
agent_bootstrap_argv_policy(int argc, char **argv)
{
    static const char *const rejected[] = {
        "-D", "-X", "-w", "-windowtype", "--windowtype", "-config",
        "--config", "-nethackrc", "-symset", "-hook", "--showpaths",
        "--version", "-version"
    };
    int i;
    size_t k;

    if (!agent_latched)
        return;
    for (i = 1; i < argc; ++i) {
        if (!argv[i])
            continue;
        for (k = 0; k < sizeof rejected / sizeof rejected[0]; ++k) {
            if (strcmp(argv[i], rejected[k]) == 0
                || strncmp(argv[i], "-windowtype=", 13) == 0
                || strncmp(argv[i], "--windowtype=", 14) == 0) {
                agent_private_fatal(
                    "rejected startup option in agent mode");
            }
        }
    }
}

const char *
agent_enforce_window_choice(const char *requested)
{
    if (agent_latched) {
        /* the latch owns the choice; never fall back to a human port */
        return "agent";
    }
    if (requested && strcmp(requested, "agent") == 0) {
        /* an explicit -wagent without a trusted latch must fail rather than
         * synthesize trust */
        agent_private_fatal("no trusted bootstrap for the agent port");
    }
    return requested;
}
