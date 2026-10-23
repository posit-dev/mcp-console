#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
#endif

static void write_pid_marker(const char *name, pid_t process_id) {
    const char *marker = getenv(name);
    if (marker == NULL) {
        return;
    }
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d\n", process_id);
        close(descriptor);
    }
}

static int deny_killpg(pid_t process_group, int signal) {
    if (signal == SIGINT
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGINT") != NULL) {
        write_pid_marker("MCP_CONSOLE_TEST_DENIED_SIGINT", process_group);
        errno = EPERM;
        return -1;
    }
    return kill(-process_group, signal);
}

static int observe_waitid(idtype_t type, id_t id, siginfo_t *info, int options) {
    const char *directory = getenv("MCP_CONSOLE_TEST_RESOLVER_WATCHES");
    if (directory != NULL && type == P_PID && (options & WNOWAIT) != 0) {
        // watch_exit marks the child active before starting this watcher.
        // This alone does not prove stop-handle registration. These custom-worker
        // tests match the later ir run PID: discovery has already consumed
        // on_started and registered the shared ResolverProcess stop handle
        // before the preceding ir --version call returns.
        char path[PATH_MAX];
        int length = snprintf(path, sizeof(path), "%s/%u", directory, (unsigned)id);
        if (length < 0 || (size_t)length >= sizeof(path)) _exit(121);
        int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (descriptor < 0) _exit(122);
        close(descriptor);
    }
#ifdef __APPLE__
    return waitid(type, id, info, options);
#else
    return ((int (*)(idtype_t, id_t, siginfo_t *, int))dlsym(RTLD_NEXT, "waitid"))(
        type, id, info, options);
#endif
}

__attribute__((constructor))
static void remove_interposer_from_child_environment(void) {
    const char *server = getenv("MCP_CONSOLE_TEST_PERMISSION_SERVER");
    if (server == NULL) {
        char pid[32];
        snprintf(pid, sizeof(pid), "%ld", (long)getpid());
        setenv("MCP_CONSOLE_TEST_PERMISSION_SERVER", pid, 1);
    } else {
        // The server's direct children include the resolver owner. Their
        // children, including ir and the worker, must not inherit the hook.
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

#ifdef __APPLE__
__attribute__((used))
static struct {
    const void *replacement;
    const void *replacee;
} interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)&deny_killpg, (const void *)&killpg},
    {(const void *)&observe_waitid, (const void *)&waitid},
};

#else
int killpg(pid_t process_group, int signal) { return deny_killpg(process_group, signal); }
int waitid(idtype_t type, id_t id, siginfo_t *info, int options) {
    return observe_waitid(type, id, info, options);
}
#endif
