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
#include <sys/socket.h>
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

#ifdef AGENT_TEST_IMPOSSIBLE
/* Test-only: the launch mode the matrix selected, or AG_HS_MODE_NEW when the
 * process is not latched.  Never exists in a production binary. */
unsigned
agent_bootstrap_test_mode(void)
{
    return agent_latched ? agent_hs.mode : AG_HS_MODE_NEW;
}
#endif /* AGENT_TEST_IMPOSSIBLE */

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

/* A connected STREAM socket is what the launcher installs.  This proves the
 * inherited descriptor's shape; it is deliberately NOT an authentication
 * claim -- the handshake record above is the trust boundary. */
static int
ag_is_connected_socket(int fd)
{
    int sotype;
    socklen_t slen = (socklen_t) sizeof sotype;
    struct sockaddr_storage ss;
    socklen_t alen = (socklen_t) sizeof ss;

    if (fd < 0)
        return 0;
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &sotype, &slen) != 0)
        return 0;
    if (sotype != SOCK_STREAM)
        return 0;
    if (getpeername(fd, (struct sockaddr *) &ss, &alen) != 0)
        return 0;
    return 1;
}

/* Copy a directory root with exactly one trailing slash, the form fqname()
 * expects when it concatenates a basename. */
static char *
ag_prefix_dir(const char *dir)
{
    size_t n = strlen(dir);
    char *p = (char *) alloc(n + 2);

    Strcpy(p, dir);
    if (n == 0 || p[n - 1] != '/') {
        p[n] = '/';
        p[n + 1] = '\0';
    }
    return p;
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
    if (hs.mode != AG_HS_MODE_NEW
#ifdef AGENT_TEST_IMPOSSIBLE
        /* Test-only launch modes (see agent_handshake.h): the matrix drives
         * one unimplemented decision callback per worker process. */
        && (hs.mode < AG_HS_MODE_TEST_FIRST
            || hs.mode > AG_HS_MODE_TEST_LAST)
#endif
        )
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
    /* Every launch path the handshake names must be absolute as well: a
     * relative root would resolve against the worker's cwd and could be
     * steered by anything that changed it. */
    if (hs.data_root[0] && hs.data_root[0] != '/')
        agent_private_fatal("trusted data root must be absolute");
    if (hs.config_root[0] && hs.config_root[0] != '/')
        agent_private_fatal("trusted config root must be absolute");
    if (hs.sysconf_path[0] && hs.sysconf_path[0] != '/')
        agent_private_fatal("trusted sysconf path must be absolute");

    /* The descriptor must be a connected stream socket. */
    if (!ag_is_connected_socket(fd))
        agent_private_fatal("agent transport is not a connected socket");

    /* The descriptor is used for the whole run, so it stays open -- but it
     * must never leak into a descendant.  The launcher cleared FD_CLOEXEC so
     * the handshake could cross execve; from here on it is SET, so a later
     * exec (save compression, panic tracer) cannot inherit the player-JSON
     * channel.  A failed flag read or write is fatal, never assumed. */
    fdflags = fcntl(fd, F_GETFD);
    if (fdflags < 0)
        agent_private_fatal("cannot read transport descriptor flags");
    if (fcntl(fd, F_SETFD, fdflags | FD_CLOEXEC) < 0)
        agent_private_fatal("cannot set close-on-exec on transport");

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
    /* The immutable roots the handshake names must exist and be readable: a
     * run whose data, configuration, or sysconf is absent cannot be the
     * reviewed launch, so it fails closed rather than falling back. */
    if (agent_hs.data_root[0]
        && (stat(agent_hs.data_root, &st) != 0 || !S_ISDIR(st.st_mode)
            || access(agent_hs.data_root, R_OK) != 0))
        agent_private_fatal("trusted data root is missing or unreadable");
    if (agent_hs.config_root[0]
        && (stat(agent_hs.config_root, &st) != 0 || !S_ISDIR(st.st_mode)
            || access(agent_hs.config_root, R_OK) != 0))
        agent_private_fatal("trusted config root is missing or unreadable");
    if (agent_hs.sysconf_path[0]
        && (stat(agent_hs.sysconf_path, &st) != 0 || !S_ISREG(st.st_mode)
            || access(agent_hs.sysconf_path, R_OK) != 0))
        agent_private_fatal("trusted sysconf is missing or unreadable");
}

/* Make the launcher's roots authoritative for the engine's path prefixes.
 *
 * This runs at the END of the trusted option phase -- after agent mode's only
 * configuration source, the approved sysconf, has been parsed -- and it
 * OVERWRITES rather than defers, so the launcher's immutable data and
 * configuration roots stay bound and every writable prefix stays inside the
 * private episode root.  A configuration statement that names a prefix is
 * already refused outright (see agent_policy_sysconf_directive); this is the
 * defense in depth that keeps the binding true even if one were admitted.
 *
 * Where the engine does not use path prefixes at all, fqname() ignores these
 * and the episode root is simply the worker's private cwd. */
void
agent_bind_prefixes(void)
{
    if (!agent_latched)
        return;
    if (agent_hs.data_root[0])
        gf.fqn_prefix[DATAPREFIX] = ag_prefix_dir(agent_hs.data_root);
    if (agent_hs.config_root[0])
        gf.fqn_prefix[CONFIGPREFIX] = ag_prefix_dir(agent_hs.config_root);
    /* Every writable location is the private per-episode root: a path-bearing
     * default can therefore never reach a shared playground. */
    gf.fqn_prefix[HACKPREFIX] = ag_prefix_dir(agent_hs.writable_root);
    gf.fqn_prefix[LEVELPREFIX] = ag_prefix_dir(agent_hs.writable_root);
    gf.fqn_prefix[SAVEPREFIX] = ag_prefix_dir(agent_hs.writable_root);
    gf.fqn_prefix[BONESPREFIX] = ag_prefix_dir(agent_hs.writable_root);
    gf.fqn_prefix[LOCKPREFIX] = ag_prefix_dir(agent_hs.writable_root);
    gf.fqn_prefix[TROUBLEPREFIX] = ag_prefix_dir(agent_hs.writable_root);
    gf.fqn_prefix[SCOREPREFIX] = ag_prefix_dir(agent_hs.writable_root);
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
