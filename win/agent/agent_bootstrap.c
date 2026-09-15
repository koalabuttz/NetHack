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

/* winagent.h is an engine-facing header (it uses the engine's `boolean`), so
 * this translation unit includes hack.h for its types.  Nothing here touches
 * engine state: the latch and the private sink use only OS resources and
 * private static storage, which is what lets the probe run before
 * early_init() has (re)initialized the globals. */
#include "hack.h"

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
    int fdflags;

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
    fdflags = fcntl(fd, F_GETFD);
    if (fdflags >= 0)
        (void) fcntl(fd, F_SETFD, fdflags & ~FD_CLOEXEC);

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

/* Classify a rejected argument for the PRIVATE diagnostic only.  The policy
 * itself is a closed list (nothing is accepted); this exists so the private
 * sink names which surface a hostile invocation tried to steer. */
static const char *
ag_argv_family(const char *arg)
{
    char c;

    if (!arg || arg[0] != '-')
        return "unexpected";
    c = arg[1];
    if (c == '\0')
        return "unexpected";
    if (c == 'd' && (arg[2] == '\0' || strcmp(arg, "-directory") == 0))
        return "playground-directory";
    if (strcmp(arg, "--directory") == 0)
        return "playground-directory";
    if (c == 'D' || strcmp(arg, "-debug") == 0)
        return "debug-or-DECgraphics";
    if (c == 'X')
        return "explore";
    if (c == 'I' || c == 'i')
        return "IBMgraphics";
    if (c == 'w' || c == 'W' || strncmp(arg, "--w", 3) == 0)
        return "window-type";
    if (strcmp(arg, "-config") == 0 || strcmp(arg, "--config") == 0)
        return "configuration";
    if (strcmp(arg, "-nethackrc") == 0 || strcmp(arg, "--nethackrc") == 0
        || strcmp(arg, "-no-nethackrc") == 0)
        return "rc-file";
    if (strcmp(arg, "-symset") == 0 || strcmp(arg, "--symset") == 0)
        return "symbol-set";
    if (strcmp(arg, "-hook") == 0 || strcmp(arg, "--hook") == 0)
        return "hook";
    if (strcmp(arg, "--showpaths") == 0 || strcmp(arg, "--version") == 0
        || strcmp(arg, "-version") == 0 || strcmp(arg, "-h") == 0
        || strcmp(arg, "-help") == 0 || strcmp(arg, "--help") == 0
        || strcmp(arg, "-?") == 0 || strcmp(arg, "?") == 0)
        return "startup-probe";
    return "unexpected";
}

/* Wave B argument policy: a latched worker has NO untrusted argument surface.
 *
 * The trusted launcher passes exactly one worker argument, the private
 * --agent-fd locator, which agent_bootstrap_probe() has already consumed and
 * removed.  Any surviving argument is therefore not supervisor-validated and
 * could steer the frontend (-w/-windowtype), the debug or explore mode
 * (-D/-debug/-X), the symbol set (-DECgraphics/-IBMgraphics/-symset), the
 * configuration or rc source (-config/-nethackrc), a hook, a startup probe
 * that would print privately (--showpaths/--version/-h/?), or the
 * playground directory (-d/-directory).  Ordinary argument parsing must never
 * see any of them, so the policy is a closed list rather than an enumeration:
 * an exact-spelling list would miss abbreviations (-I, -d<path>, --window).
 * Character start data, when it is implemented, arrives through the trusted
 * handshake rather than argv. */
void
agent_bootstrap_argv_policy(int argc, char **argv)
{
    int i;

    if (!agent_latched)
        return;
    for (i = 1; i < argc; ++i) {
        char msg[96];

        if (!argv[i])
            continue;
        (void) snprintf(msg, sizeof msg, "rejected %s argument in agent mode",
                        ag_argv_family(argv[i]));
        agent_private_fatal(msg);
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
