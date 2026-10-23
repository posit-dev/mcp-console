#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

static atomic_uint fork_count = 0;
static int sandboxed_owner = 0;

static int is_resolver(void) {
    const char *server = getenv("MCP_CONSOLE_TEST_SPAWN_SERVER");
    const char *child = getenv("MCP_CONSOLE_TEST_SPAWN_CHILD");
    return sandboxed_owner || (server != NULL && strtol(server, NULL, 10) ==
        (child != NULL ? getppid() : getpid()));
}

__attribute__((constructor)) static void prevent_child_injection(void) {
    // Resolver settings identify the owner beyond the native launcher boundary.
    sandboxed_owner = getenv("MCP_CONSOLE_TEST_SPAWN_OWNER") != NULL;
    unsetenv("MCP_CONSOLE_TEST_SPAWN_OWNER");
    if (is_resolver()) {
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

static pid_t checkpoint_fork(void) {
    const char *armed = getenv("MCP_CONSOLE_TEST_SPAWN_ARMED");
    const char *ordinal = getenv("MCP_CONSOLE_TEST_SPAWN_ORDINAL");
    if (is_resolver() && armed != NULL && access(armed, F_OK) == 0 &&
        atomic_fetch_add(&fork_count, 1) + 1 == strtoul(ordinal, NULL, 10)) {
        int started = open(getenv("MCP_CONSOLE_TEST_SPAWN_STARTED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_SPAWN_RELEASE"), O_RDONLY);
        if (started < 0 || release < 0 || write(started, "1", 1) != 1) {
            _exit(121);
        }
        char token;
        ssize_t result;
        do {
            result = read(release, &token, 1);
        } while (result < 0 && errno == EINTR);
        if (result != 1 || token != '1') {
            _exit(122);
        }
        close(started);
        close(release);
    }
#ifdef __APPLE__
    return fork();
#else
    return ((pid_t (*)(void))dlsym(RTLD_NEXT, "fork"))();
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} fork_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&checkpoint_fork,
    (const void *)(uintptr_t)&fork,
};

#else
pid_t fork(void) { return checkpoint_fork(); }
#endif
