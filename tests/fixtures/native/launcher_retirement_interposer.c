#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
#endif

static atomic_bool claimed = false;

typedef int (*kill_function)(pid_t, int);

static kill_function next_kill(void) {
#ifdef __APPLE__
    return kill;
#else
    return (kill_function)dlsym(RTLD_NEXT, "kill");
#endif
}

static bool configured(const char *variable) {
    return getenv(variable) != NULL;
}

static bool target_process(void) {
    const char *value = getenv("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_PID");
    if (value == NULL) {
        return false;
    }
    char *end = NULL;
    long process_id = strtol(value, &end, 10);
    return end != value && *end == '\0' && process_id == getpid();
}

static void notify(const char *variable) {
    const char *path = getenv(variable);
    if (path == NULL) {
        _exit(125);
    }
    int descriptor;
    do {
        descriptor = open(path, O_WRONLY | O_NONBLOCK);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        _exit(125);
    }
    ssize_t count;
    do {
        count = write(descriptor, "1", 1);
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != 1) {
        _exit(125);
    }
}

static void wait_for_release(const char *variable) {
    const char *path = getenv(variable);
    if (path == NULL) {
        _exit(125);
    }
    int descriptor;
    do {
        descriptor = open(path, O_RDONLY);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        _exit(125);
    }
    char token;
    ssize_t count;
    do {
        count = read(descriptor, &token, 1);
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != 1) {
        _exit(125);
    }
}

static int delayed_retirement_signal(pid_t process_id, int signal_number) {
    kill_function kill_next = next_kill();
    if (!target_process() || signal_number != SIGTERM ||
        atomic_exchange(&claimed, true)) {
        return kill_next(process_id, signal_number);
    }

    if (configured("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_BLOCKED")) {
        notify("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_BLOCKED");
        wait_for_release("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RELEASE");
    }
    int result = kill_next(process_id, signal_number);
    int saved_errno = errno;
    if (result == 0) {
        notify("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURNED");
        if (configured("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURN_RELEASE")) {
            wait_for_release(
                "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURN_RELEASE");
        }
    }
    errno = saved_errno;
    return result;
}

__attribute__((constructor)) static void prevent_child_injection(void) {
    if (target_process()) {
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

#ifdef __APPLE__
#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(delayed_retirement_signal, kill)

#else
int kill(pid_t process_id, int signal_number) {
    return delayed_retirement_signal(process_id, signal_number);
}
#endif
