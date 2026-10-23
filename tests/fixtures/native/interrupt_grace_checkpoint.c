#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

static pid_t owner;
static atomic_bool claimed;

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static void checkpoint(const struct timespec *duration) {
    // Hold the documented 100 ms interrupt grace after signal acknowledgment.
    // Other sleeps and child processes retain their ordinary behavior.
    if (getpid() != owner || duration->tv_sec != 0 ||
        duration->tv_nsec != 100000000 || atomic_exchange(&claimed, true)) return;
    int release = open(getenv("MCP_CONSOLE_TEST_INTERRUPT_GRACE_RELEASE"), O_RDONLY);
    int reached = open(getenv("MCP_CONSOLE_TEST_INTERRUPT_GRACE_REACHED"), O_WRONLY);
    if (release < 0 || reached < 0 || write(reached, "1", 1) != 1) _exit(123);
    close(reached);
    char token;
    ssize_t count;
    do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(124);
    close(release);
}

#ifdef __APPLE__
static int gated_nanosleep(const struct timespec *duration, struct timespec *remaining) {
    checkpoint(duration);
    return nanosleep(duration, remaining);
}

__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_sleep __attribute__((section("__DATA,__interpose"))) = {
    (const void *)&gated_nanosleep, (const void *)&nanosleep,
};
#else
int clock_nanosleep(clockid_t clock, int flags, const struct timespec *duration,
                    struct timespec *remaining) {
    checkpoint(duration);
    int (*native_sleep)(clockid_t, int, const struct timespec *, struct timespec *) =
        dlsym(RTLD_NEXT, "clock_nanosleep");
    if (native_sleep == NULL) _exit(125);
    return native_sleep(clock, flags, duration, remaining);
}
#endif
