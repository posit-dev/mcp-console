#ifdef __linux__
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_read)(int, void *, size_t);
#define read native_read
#endif

static atomic_int output_descriptor = -1;
static int refill_request;
static int refill_done;
static int output_observed;
static const char *output_match;

__attribute__((constructor)) static void initialize_refill_checkpoints(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
#ifdef __linux__
    unsetenv("LD_PRELOAD");
    native_read = dlsym(RTLD_NEXT, "read");
    if (native_read == NULL) _exit(90);
#endif
    output_match = getenv("MCP_CONSOLE_TEST_REFILL_MATCH");
    refill_request = open(getenv("MCP_CONSOLE_TEST_REFILL_REQUEST"), O_WRONLY | O_CLOEXEC);
    refill_done = open(getenv("MCP_CONSOLE_TEST_REFILL_DONE"), O_RDONLY | O_CLOEXEC);
    output_observed = open(getenv("MCP_CONSOLE_TEST_REFILL_OBSERVED"), O_WRONLY | O_CLOEXEC);
    if (output_match == NULL || refill_request < 0 || refill_done < 0 || output_observed < 0) {
        _exit(91);
    }
}

// Let the actual descendant refill the descriptor before a successful read
// returns. Every subsequent retirement read therefore has real bytes available;
// the test does not depend on the producer winning a scheduling race.
static ssize_t refill_after_read(int descriptor, void *buffer, ssize_t result) {
    if (result <= 0) {
        return result;
    }
    int tracked = atomic_load(&output_descriptor);
    int first = 0;
    if (tracked == -1 && (size_t)result == strlen(output_match) &&
        memcmp(buffer, output_match, (size_t)result) == 0) {
        atomic_store(&output_descriptor, descriptor);
        tracked = descriptor;
        first = 1;
    }
    if (descriptor != tracked) {
        return result;
    }
    if (write(refill_request, "1", 1) != 1) {
        _exit(92);
    }
    char token;
    ssize_t acknowledged;
    do {
        acknowledged = read(refill_done, &token, 1);
    } while (acknowledged < 0 && errno == EINTR);
    if (acknowledged != 1 || token != '1') {
        _exit(93);
    }
    if (first && write(output_observed, "1", 1) != 1) {
        _exit(94);
    }
    return result;
}

static ssize_t refilling_read(int descriptor, void *buffer, size_t length) {
    return refill_after_read(descriptor, buffer, read(descriptor, buffer, length));
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

DYLD_INTERPOSE(refilling_read, read)

#else
#undef read
ssize_t read(int descriptor, void *buffer, size_t length) {
    return refilling_read(descriptor, buffer, length);
}
#endif
