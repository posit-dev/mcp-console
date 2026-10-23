#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

struct joiner {
    pthread_t target;
    struct joiner *next;
};
static struct joiner *joiners;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;

static void corrupt_preparation(int signal) {
    (void)signal;
    // Leave the process and stderr alive after the controller rejects stdout.
    if (write(STDOUT_FILENO, "null\n", 5) != 5) _exit(125);
    for (;;) pause();
}

__attribute__((constructor)) static void install_preparation_fault(void) {
    if (getenv("MCP_CONSOLE_TEST_REAP_CORRUPT") == NULL) return;
    struct sigaction action = { .sa_handler = corrupt_preparation };
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGUSR1, &action, NULL) < 0) _exit(126);
}

static ssize_t native_write(int fd, const void *bytes, size_t count) {
#ifdef __APPLE__
    return write(fd, bytes, count);
#else
    return ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(fd, bytes, count);
#endif
}

static ssize_t observe_write(int fd, const void *bytes, size_t count) {
    // Selected-Python inspection clears RETICULATE_PYTHON and owns a separate
    // short-lived preparation peer. Fault only the retained dependency peer.
    if (getenv("RETICULATE_PYTHON") != NULL &&
        count == 8 && memcmp(bytes, "\"Closed\"", 8) == 0) {
        int marker = open(getenv("MCP_CONSOLE_TEST_REAP_PID"), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        char pid[32];
        int length = snprintf(pid, sizeof(pid), "%ld", (long)getpid());
        if (marker < 0 || native_write(marker, pid, (size_t)length) != length) _exit(121);
        close(marker);
        const char *blocked = getenv("MCP_CONSOLE_TEST_REAP_BLOCK_CLOSE");
        if (blocked != NULL) {
            int checkpoint = open(blocked, O_WRONLY);
            if (checkpoint < 0 || native_write(checkpoint, "1", 1) != 1) _exit(123);
            close(checkpoint);
            // The close request arrived, but this peer never acknowledges it
            // or exits. Only its controller can finish retirement.
            for (;;) pause();
        }
    }
    return native_write(fd, bytes, count);
}

static bool resolver(pid_t pid) {
    int marker = open(getenv("MCP_CONSOLE_TEST_REAP_PID"), O_RDONLY);
    if (marker < 0) return false;
    char text[32] = {0};
    ssize_t count = read(marker, text, sizeof(text) - 1);
    close(marker);
    return count > 0 && strtol(text, NULL, 10) == pid;
}

static int deny_termination(pid_t pid, int signal) {
    const char *denied = getenv("MCP_CONSOLE_TEST_REAP_DENY_KILL");
    if (signal == SIGKILL && denied != NULL && resolver(pid) &&
        (getenv("MCP_CONSOLE_TEST_REAP_DENY_ONCE") == NULL || access(denied, F_OK) != 0)) {
        int marker = open(denied, O_WRONLY | O_CREAT, 0600);
        if (marker < 0) _exit(124);
        close(marker);
        errno = EPERM;
        return -1;
    }
#ifdef __APPLE__
    return kill(pid, signal);
#else
    return ((int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill"))(pid, signal);
#endif
}

static bool joined(void) {
    for (struct joiner *entry = joiners; entry != NULL; entry = entry->next) {
        if (pthread_equal(entry->target, pthread_self())) return true;
    }
    return false;
}

static pid_t gated_waitpid(pid_t pid, int *status, int options) {
    bool held = pid > 0 && resolver(pid);
    if (held && getenv("MCP_CONSOLE_TEST_REAP_CORRUPT") == NULL) {
        // Closed and EOF have reached the controller. Hold the actual reap
        // until its owner is joined, including a join that started earlier.
        pthread_mutex_lock(&lock);
        while (!joined()) pthread_cond_wait(&changed, &lock);
        pthread_mutex_unlock(&lock);
    }
#ifdef __APPLE__
    pid_t result = waitpid(pid, status, options);
#else
    pid_t result = ((pid_t (*)(pid_t, int *, int))dlsym(RTLD_NEXT, "waitpid"))(pid, status, options);
#endif
    int saved_errno = errno;
    if (held && result == pid) {
        int marker = open(getenv("MCP_CONSOLE_TEST_REAP_DONE"), O_WRONLY | O_CREAT, 0600);
        if (marker < 0) _exit(122);
        close(marker);
    }
    errno = saved_errno;
    return result;
}

static int observe_join(pthread_t thread, void **value) {
    struct joiner entry = { .target = thread };
    pthread_mutex_lock(&lock);
    entry.next = joiners;
    joiners = &entry;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
#ifdef __APPLE__
    int result = pthread_join(thread, value);
#else
    int result = ((int (*)(pthread_t, void **))dlsym(RTLD_NEXT, "pthread_join"))(thread, value);
#endif
    pthread_mutex_lock(&lock);
    struct joiner **current = &joiners;
    while (*current != &entry) current = &(*current)->next;
    *current = entry.next;
    pthread_mutex_unlock(&lock);
    return result;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)replacement, (const void *)original \
    };
INTERPOSE(observe_write, write)
INTERPOSE(deny_termination, kill)
INTERPOSE(gated_waitpid, waitpid)
INTERPOSE(observe_join, pthread_join)
#else
ssize_t write(int fd, const void *bytes, size_t count) { return observe_write(fd, bytes, count); }
int kill(pid_t pid, int signal) { return deny_termination(pid, signal); }
pid_t waitpid(pid_t pid, int *status, int options) { return gated_waitpid(pid, status, options); }
int pthread_join(pthread_t thread, void **value) { return observe_join(thread, value); }
#endif
