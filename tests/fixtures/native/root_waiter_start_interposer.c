#include "runner_interposer.h"
#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/event.h>
#include <sys/syscall.h>
#include <unistd.h>

static _Atomic int gated_root_waiter_start = 0;

static void signal_checkpoint(const char *name) {
    const char *checkpoint = getenv(name);
    if (checkpoint == NULL) {
        _exit(125);
    }
    int descriptor = open(checkpoint, O_WRONLY | O_NONBLOCK);
    if (descriptor < 0) {
        _exit(125);
    }
    const char value = '1';
    ssize_t count;
    do {
        count = write(descriptor, &value, sizeof(value));
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != sizeof(value)) {
        _exit(125);
    }
}

static void wait_for_release(const char *name) {
    const char *release = getenv(name);
    if (release == NULL) {
        _exit(125);
    }
    int descriptor;
    do {
        descriptor = open(release, O_RDONLY);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        _exit(125);
    }
    char value;
    ssize_t count;
    do {
        count = read(descriptor, &value, sizeof(value));
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != sizeof(value)) {
        _exit(125);
    }
}

static int gate_root_waiter_start(void) {
    if (runner_is_supervisor()
        && atomic_exchange(&gated_root_waiter_start, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_ROOT_WAITER_START");
        wait_for_release("MCP_CONSOLE_TEST_ROOT_WAITER_RELEASE");
    }
    return getppid();
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(gate_root_waiter_start, getppid)
