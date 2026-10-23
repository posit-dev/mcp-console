#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/event.h>
#endif

static atomic_int observed_pid;
static atomic_bool settled;
static atomic_bool interrupted;
static atomic_bool signal_gated;
static _Thread_local bool observer_thread;

__attribute__((constructor)) static void initialize(void) {
    // An MCP test loads the same fixture in the server so its direct resolver
    // child inherits it. Other processes drop injection before spawning.
    const char *server = getenv("MCP_CONSOLE_TEST_OBSERVER_SERVER");
    if (server != NULL && strtol(server, NULL, 10) == getpid()) return;
    // Interpose only the relay or preparation owner, never its child.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static bool target_child(id_t id) {
    const char *path = getenv("MCP_CONSOLE_TEST_OBSERVER_TARGET");
    if (path == NULL) return true;
    FILE *file = fopen(path, "r");
    if (file == NULL) return false; // The materializer has not published its PID.
    int pid;
    int count = fscanf(file, "%d", &pid);
    if (fclose(file) != 0 || count != 1) _exit(125);
    return pid == (int)id;
}

static void checkpoint(const char *name, int flags) {
    int descriptor = open(getenv(name), flags);
    char token = '1';
    if (descriptor < 0) _exit(120);
    ssize_t count = flags == O_RDONLY ? read(descriptor, &token, 1)
                                     : write(descriptor, &token, 1);
    if (count != 1 || token != '1') _exit(121);
    close(descriptor);
}

static int observe_exit(idtype_t type, id_t id, siginfo_t *info, int options) {
    bool observing = type == P_PID && (options & WNOWAIT) && target_child(id);
    // The cancellable resolver observer probes waitid before its native event
    // wait. Gate that observer thread, without gating owner-side status probes.
    if (observing && atomic_load(&observed_pid) == 0 &&
        getenv("MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE") != NULL) {
        observer_thread = true;
    }
    bool blocking = observing && (!(options & WNOHANG) || observer_thread);
    bool first = blocking && atomic_load(&observed_pid) == 0;
    bool stale_probe = getenv("MCP_CONSOLE_TEST_OBSERVER_STALE_PROBE") != NULL;
    if (first) {
        atomic_store(&observed_pid, (int)id);
        const char *pid_path = getenv("MCP_CONSOLE_TEST_OBSERVER_PID");
        if (pid_path != NULL) {
            FILE *file = fopen(pid_path, "w");
            if (file == NULL) _exit(123);
            if (fprintf(file, "%d\n", (int)id) < 0 || fclose(file) != 0) _exit(124);
        }
        if (!stale_probe) {
            checkpoint("MCP_CONSOLE_TEST_OBSERVER_ENTERED", O_WRONLY);
            checkpoint("MCP_CONSOLE_TEST_OBSERVER_RELEASE", O_RDONLY);
        }
    }
    if (blocking && getenv("MCP_CONSOLE_TEST_OBSERVER_FAIL") != NULL) {
        atomic_store(&settled, true);
        errno = EIO;
        return -1;
    }
    if (observing && !blocking && getenv("MCP_CONSOLE_TEST_OBSERVER_PROBE_FAIL") != NULL) {
        errno = EIO;
        return -1;
    }
    if (blocking && getenv("MCP_CONSOLE_TEST_OBSERVER_EINTR") != NULL &&
        !atomic_exchange(&interrupted, true)) {
        errno = EINTR;
        return -1;
    }
#ifdef __APPLE__
    if (observer_thread && observing && !first &&
        getenv("MCP_CONSOLE_TEST_OBSERVER_DELAY_STATUS") != NULL) {
        // NOTE_EXIT is ready, but terminal status is not yet available to a
        // nonblocking probe. Hold confirmation to witness the reaping barrier.
        checkpoint("MCP_CONSOLE_TEST_STATUS_PENDING", O_WRONLY);
        checkpoint("MCP_CONSOLE_TEST_STATUS_RELEASE", O_RDONLY);
        if (options & WNOHANG) {
            memset(info, 0, sizeof(*info));
            return 0;
        }
    }
#endif
    int native_options = options;
    if (first && getenv("MCP_CONSOLE_TEST_STOP_OVERLAP") != NULL) {
        // Darwin's WEXITED probe can return a pending stop. Use the real
        // stopped child's status to reproduce that notification deterministically.
        native_options |= WSTOPPED;
    }
#ifdef __APPLE__
    int result = waitid(type, id, info, native_options);
#else
    int result = ((int (*)(idtype_t, id_t, siginfo_t *, int))dlsym(RTLD_NEXT, "waitid"))(
        type, id, info, native_options);
#endif
    int saved_errno = errno;
    if (!(options & WNOWAIT) && (options & WSTOPPED) &&
        getenv("MCP_CONSOLE_TEST_STOP_OVERLAP") != NULL) {
        checkpoint("MCP_CONSOLE_TEST_STOP_PROCESSED", O_WRONLY);
    }
    if (first && stale_probe) {
        // Retain the live-child probe result until the fixture has confirmed
        // exit and delivered an interrupt. Native event registration follows.
        checkpoint("MCP_CONSOLE_TEST_OBSERVER_ENTERED", O_WRONLY);
        checkpoint("MCP_CONSOLE_TEST_OBSERVER_RELEASE", O_RDONLY);
    }
    if (blocking && result == 0 &&
        (info->si_code == CLD_EXITED || info->si_code == CLD_KILLED || info->si_code == CLD_DUMPED)) {
        atomic_store(&settled, true);
    }
    errno = saved_errno;
    return result;
}

static pid_t observe_reap(pid_t pid, int *status, int options) {
    if (pid == atomic_load(&observed_pid) && !atomic_load(&settled)) {
        // Even a WNOHANG try_wait can reap and release the child's PID.
        int descriptor = open(getenv("MCP_CONSOLE_TEST_EARLY_REAP"),
                              O_WRONLY | O_CREAT, 0600);
        if (descriptor < 0) _exit(122);
        close(descriptor);
    }
#ifdef __APPLE__
    return waitpid(pid, status, options);
#else
    return ((pid_t (*)(pid_t, int *, int))dlsym(RTLD_NEXT, "waitpid"))(pid, status, options);
#endif
}

static int observe_kill(pid_t pid, int signal) {
    bool gate_signal = getenv("MCP_CONSOLE_TEST_INTERRUPT_RACE") != NULL &&
        (signal == SIGINT || signal == SIGSTOP) &&
        (pid == atomic_load(&observed_pid) || -pid == atomic_load(&observed_pid));
    if (gate_signal && !atomic_exchange(&signal_gated, true)) {
        // Hold the first control signal after any owner-side liveness probe.
        // The test confirms the child's independent exit before releasing it.
        checkpoint("MCP_CONSOLE_TEST_SIGNAL_ENTERED", O_WRONLY);
        checkpoint("MCP_CONSOLE_TEST_SIGNAL_RELEASE", O_RDONLY);
    }
    if (signal == SIGKILL && -pid == atomic_load(&observed_pid) &&
        getenv("MCP_CONSOLE_TEST_CLEANUP_FAIL") != NULL) {
        checkpoint("MCP_CONSOLE_TEST_CHILD_KILLED", O_WRONLY);
        errno = EACCES;
        return -1;
    }
#ifdef __APPLE__
    int result = kill(pid, signal);
#else
    int result = ((int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill"))(pid, signal);
#endif
    int saved_errno = errno;
    if (signal == SIGSTOP && getenv("MCP_CONSOLE_TEST_STOP_OVERLAP") != NULL) {
        siginfo_t stopped = {0};
#ifdef __APPLE__
        int observed = waitid(P_PID, pid, &stopped, WSTOPPED | WNOWAIT);
#else
        int observed = ((int (*)(idtype_t, id_t, siginfo_t *, int))dlsym(RTLD_NEXT, "waitid"))(
            P_PID, pid, &stopped, WSTOPPED | WNOWAIT);
#endif
        if (observed != 0 || stopped.si_code != CLD_STOPPED) _exit(126);
        checkpoint("MCP_CONSOLE_TEST_SIGNAL_ENTERED", O_WRONLY);
        checkpoint("MCP_CONSOLE_TEST_SIGNAL_RELEASE", O_RDONLY);
    }
    if (gate_signal && signal == SIGINT) {
        // Linux accepts killpg for an unreaped zombie. Reproduce that return
        // value on macOS too, without changing the real exit status.
        result = 0;
    }
    if (signal == SIGKILL && (pid == atomic_load(&observed_pid) ||
                             -pid == atomic_load(&observed_pid))) {
        checkpoint("MCP_CONSOLE_TEST_CHILD_KILLED", O_WRONLY);
    }
    errno = saved_errno;
    return result;
}

static int observe_killpg(pid_t group, int signal) {
    return observe_kill(-group, signal);
}

#ifdef __APPLE__
static int observe_kevent(int queue, const struct kevent *changes, int change_count,
                          struct kevent *events, int event_count,
                          const struct timespec *timeout) {
    int result = kevent(queue, changes, change_count, events, event_count, timeout);
    if (result == 0 && observer_thread && change_count == 1 &&
        changes[0].filter == EVFILT_PROC && (changes[0].fflags & NOTE_EXIT) &&
        getenv("MCP_CONSOLE_TEST_STOP_OVERLAP") != NULL) {
        checkpoint("MCP_CONSOLE_TEST_STOP_PROCESSED", O_WRONLY);
    }
    if (result == 0 && observer_thread && change_count == 1 &&
        changes[0].filter == EVFILT_PROC && (changes[0].fflags & NOTE_EXIT) &&
        getenv("MCP_CONSOLE_TEST_OBSERVER_DELAY_STATUS") != NULL) {
        // The fixture keeps the materializer live until its exit notification
        // is registered, so the regression exercises readiness, not ESRCH.
        checkpoint("MCP_CONSOLE_TEST_EXIT_REGISTERED", O_WRONLY);
    }
    return result;
}

#define INTERPOSE(replacement, replacee)                                     \
    __attribute__((used)) static struct {                                   \
        const void *replacement;                                           \
        const void *replacee;                                               \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {\
        (const void *)&replacement, (const void *)&replacee,                 \
    };
INTERPOSE(observe_exit, waitid)
INTERPOSE(observe_reap, waitpid)
INTERPOSE(observe_kill, kill)
INTERPOSE(observe_killpg, killpg)
INTERPOSE(observe_kevent, kevent)
#else
int waitid(idtype_t type, id_t id, siginfo_t *info, int options) {
    return observe_exit(type, id, info, options);
}
pid_t waitpid(pid_t pid, int *status, int options) { return observe_reap(pid, status, options); }
int kill(pid_t pid, int signal) { return observe_kill(pid, signal); }
int killpg(pid_t group, int signal) { return observe_killpg(group, signal); }
#endif
