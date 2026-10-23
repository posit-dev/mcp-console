#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <spawn.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

static int spawned;

static void launched(void) { spawned = 1; }

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    if (pthread_atfork(NULL, launched, NULL) != 0) _exit(90);
}

static void checkpoint(const char *name, int flags) {
    int descriptor = open(getenv(name), flags | O_CLOEXEC);
    char token = '1';
    ssize_t result;
    if (descriptor < 0) _exit(91);
    do {
        result = flags == O_RDONLY ? read(descriptor, &token, 1)
                                   : write(descriptor, &token, 1);
    } while (result < 0 && errno == EINTR);
    if (result != 1 || token != '1') _exit(92);
    close(descriptor);
}

static int fail_setup(void) {
    if (!spawned) return 0;
    spawned = 0;
    // Fail the next actual pipe creation after launch, without counting setup
    // calls. Keep the child alive for the test's exit subscription first.
    checkpoint("MCP_CONSOLE_TEST_SETUP_ENTERED", O_WRONLY);
    checkpoint("MCP_CONSOLE_TEST_SETUP_RELEASE", O_RDONLY);
    errno = EIO;
    return 1;
}

static int observe_spawn(pid_t *pid, const char *path,
                         const posix_spawn_file_actions_t *actions,
                         const posix_spawnattr_t *attributes,
                         char *const arguments[], char *const environment[]) {
#ifdef __APPLE__
    int result = posix_spawn(pid, path, actions, attributes, arguments, environment);
#else
    int result = ((int (*)(pid_t *, const char *, const posix_spawn_file_actions_t *,
                          const posix_spawnattr_t *, char *const[], char *const[]))
                  dlsym(RTLD_NEXT, "posix_spawn"))(
        pid, path, actions, attributes, arguments, environment);
#endif
    if (result == 0) launched();
    return result;
}

#ifdef __APPLE__
static int observe_pipe(int descriptors[2]) {
    return fail_setup() ? -1 : pipe(descriptors);
}
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original \
    };
INTERPOSE(observe_spawn, posix_spawn)
INTERPOSE(observe_pipe, pipe)
#else
int pipe2(int descriptors[2], int flags) {
    if (fail_setup()) return -1;
    return ((int (*)(int[2], int))dlsym(RTLD_NEXT, "pipe2"))(descriptors, flags);
}
int posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *actions,
                const posix_spawnattr_t *attributes, char *const arguments[],
                char *const environment[]) {
    return observe_spawn(pid, path, actions, attributes, arguments, environment);
}
int posix_spawnp(pid_t *pid, const char *path, const posix_spawn_file_actions_t *actions,
                 const posix_spawnattr_t *attributes, char *const arguments[],
                 char *const environment[]) {
    int result = ((int (*)(pid_t *, const char *, const posix_spawn_file_actions_t *,
                          const posix_spawnattr_t *, char *const[], char *const[]))
                  dlsym(RTLD_NEXT, "posix_spawnp"))(
        pid, path, actions, attributes, arguments, environment);
    if (result == 0) launched();
    return result;
}
#endif
