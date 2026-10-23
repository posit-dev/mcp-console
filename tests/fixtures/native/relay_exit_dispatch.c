#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *root;
static pid_t server;
static atomic_bool exit_reached;
static atomic_bool dispatch_reached;
static pthread_key_t dispatcher;

static int checkpoint(const char *name) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s", root, name) >= (int)sizeof(path)) _exit(125);
    int descriptor = open(path, O_RDWR | O_CLOEXEC);
    if (descriptor < 0) _exit(125);
    return descriptor;
}

static void notify(const char *name) {
    int descriptor = checkpoint(name);
    if (write(descriptor, "1", 1) != 1) _exit(125);
    close(descriptor);
}

static void wait_for(const char *name) {
    int descriptor = checkpoint(name);
    char token;
    ssize_t result;
    do { result = read(descriptor, &token, 1); } while (result < 0 && errno == EINTR);
    if (result != 1 || token != '1') _exit(125);
    close(descriptor);
}

static void dispatcher_exited(void *value) {
    (void)value;
    notify("exit-settled");
}

__attribute__((constructor)) static void initialize(void) {
    server = getpid();
    root = getenv("MCP_CONSOLE_TEST_EXIT_ROOT");
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    if (pthread_key_create(&dispatcher, dispatcher_exited) != 0) _exit(125);
}

static void *gate_exit_diagnostic(size_t size) {
#ifdef __APPLE__
    void *result = malloc(size);
#else
    void *result = ((void *(*)(size_t))dlsym(RTLD_NEXT, "malloc"))(size);
#endif
    // Pause the real dispatcher while it constructs its public exit diagnostic.
    // Admission is already closed in the regression, but must remain open until
    // the exit cause has been recorded. No failure or completion is fabricated.
    if (getpid() == server && root != NULL &&
        size == strlen("worker relay stdout closed before retirement completed")) {
        char path[4096];
        snprintf(path, sizeof(path), "%s/armed", root);
        if (access(path, F_OK) == 0 && !atomic_exchange(&exit_reached, true)) {
            if (pthread_setspecific(dispatcher, (void *)1) != 0) _exit(125);
            notify("exit-reached");
            wait_for("exit-release");
        }
    }
    return result;
}

static ssize_t gate_evaluate_write(int descriptor, const void *bytes, size_t length) {
    if (getpid() == server && atomic_load(&exit_reached) &&
        length == 1 && memcmp(bytes, "{", length) == 0 &&
        !atomic_exchange(&dispatch_reached, true)) {
        // A queued cell must not turn relay death into EPIPE while its exit
        // dispatcher is paused. Release this actual write after exit settlement.
        notify("dispatch-reached");
        wait_for("write-release");
    }
#ifdef __APPLE__
    return write(descriptor, bytes, length);
#else
    return ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(descriptor, bytes, length);
#endif
}

static pid_t observe_reap(pid_t pid, int *status, int options) {
    // Closed admission rejects dispatch synchronously. Reaping is the public
    // operation's next native boundary, after its first failure was recorded.
    if (getpid() == server && atomic_load(&exit_reached) &&
        !atomic_exchange(&dispatch_reached, true)) notify("dispatch-reached");
#ifdef __APPLE__
    return waitpid(pid, status, options);
#else
    return ((pid_t (*)(pid_t, int *, int))dlsym(RTLD_NEXT, "waitpid"))(pid, status, options);
#endif
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original \
    };
INTERPOSE(gate_exit_diagnostic, malloc)
INTERPOSE(gate_evaluate_write, write)
INTERPOSE(observe_reap, waitpid)
#else
void *malloc(size_t size) { return gate_exit_diagnostic(size); }
ssize_t write(int descriptor, const void *bytes, size_t length) { return gate_evaluate_write(descriptor, bytes, length); }
pid_t waitpid(pid_t pid, int *status, int options) { return observe_reap(pid, status, options); }
#endif
