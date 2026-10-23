#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static pid_t owner;
static bool server;
static bool relay;
static const char *root;
static struct stat output_identity;

__attribute__((constructor)) static void initialize(int argc, char **argv) {
    owner = getpid();
    root = getenv("MCP_CONSOLE_TEST_STORAGE_RETIREMENT_ROOT");
    server = argc > 1 && strcmp(argv[1], "serve") == 0;
    relay = argc > 1 && strcmp(argv[1], "worker-relay") == 0;
    if (relay) {
        if (fstat(STDOUT_FILENO, &output_identity) < 0) _exit(125);
        // Keep the real worker and its subprocesses free of the observer.
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

static ssize_t native_write(int descriptor, const void *bytes, size_t length) {
#ifdef __APPLE__
    return write(descriptor, bytes, length);
#else
    return ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(
        descriptor, bytes, length);
#endif
}

static void checkpoint(const char *name, bool wait) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s", root, name) >= (int)sizeof(path)) _exit(125);
    int descriptor = open(path, wait ? O_RDONLY : O_WRONLY | O_NONBLOCK);
    if (descriptor < 0) _exit(125);
    char token = '1';
    ssize_t result;
    do {
        result = wait ? read(descriptor, &token, 1) : native_write(descriptor, &token, 1);
    } while (result < 0 && errno == EINTR);
    close(descriptor);
    if (result != 1 || token != '1') _exit(125);
}

static ssize_t observe_write(int descriptor, const void *bytes, size_t length) {
    ssize_t result = native_write(descriptor, bytes, length);
    if (getpid() != owner || root == NULL || result <= 0) return result;
    struct stat identity;
    if (!server && !(relay && fstat(descriptor, &identity) == 0 &&
        identity.st_dev == output_identity.st_dev &&
        identity.st_ino == output_identity.st_ino)) return result;

    static _Thread_local int observed = -1;
    static _Thread_local size_t matched;
    static _Thread_local bool shutdown;
    if (observed != descriptor) {
        observed = descriptor;
        matched = 0;
        shutdown = false;
    }
    const char *expected = server ? "\"kind\":\"shutdown\"" :
        "{\"kind\":\"worker_exited\",\"code\":47}\n";
    const char *data = bytes;
    for (ssize_t index = 0; index < result; index++) {
        if (shutdown && data[index] == '\n') {
            checkpoint("shutdown-written", false);
            shutdown = false;
        }
        matched = data[index] == expected[matched] ? matched + 1 :
            (data[index] == expected[0] ? 1 : 0);
        if (expected[matched] == '\0') {
            matched = 0;
            if (server) {
                shutdown = true;
            } else {
                // The real relay has reaped the worker and joined its readers.
                // Keep its command/output descriptors open through the server's
                // shutdown write and ordered dispatcher retirement barrier.
                checkpoint("worker-reaped", false);
                checkpoint("relay-release", true);
            }
        }
    }
    return result;
}

static int gate_retirement(pid_t pid, int number) {
    if (getpid() == owner && server && root != NULL && number == SIGTERM) {
        // This real signal follows the generation's ordered retirement barrier.
        checkpoint("launcher-retiring", false);
        checkpoint("signal-release", true);
    }
#ifdef __APPLE__
    return kill(pid, number);
#else
    return ((int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill"))(pid, number);
#endif
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original \
    };
INTERPOSE(observe_write, write)
INTERPOSE(gate_retirement, kill)
#else
ssize_t write(int descriptor, const void *bytes, size_t length) { return observe_write(descriptor, bytes, length); }
int kill(pid_t pid, int number) { return gate_retirement(pid, number); }
#endif
