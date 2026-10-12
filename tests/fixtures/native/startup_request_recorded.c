#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <dlfcn.h>
#include <linux/futex.h>
#include <stdarg.h>
#include <sys/syscall.h>
static int (*native_clock_gettime)(clockid_t, struct timespec *);
static long (*native_syscall)(long, ...);
#endif

static pthread_t main_thread;
static atomic_bool observed;
static _Thread_local bool recording;

__attribute__((constructor)) static void initialize(void) {
    main_thread = pthread_self();
#ifdef __linux__
    native_clock_gettime = dlsym(RTLD_NEXT, "clock_gettime");
    native_syscall = dlsym(RTLD_NEXT, "syscall");
    if (!native_clock_gettime || !native_syscall) _exit(120);
#endif
    // Preparation children keep their ordinary discovery and I/O behavior.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static void observe_recording(clockid_t clock) {
    const char *armed = getenv("MCP_CONSOLE_TEST_REQUEST_ARMED");
    // Discovery is held and there are no prior send calls. The armed UTC
    // timestamp belongs to Transcript::begin on its blocking-pool thread.
    if (clock == CLOCK_REALTIME && !pthread_equal(pthread_self(), main_thread)
        && armed && access(armed, F_OK) == 0 && !atomic_exchange(&observed, true))
        recording = true;
}

static void recorder_returned(void) {
    if (!recording) return;
    recording = false;
    // A pool wait follows task completion and publication of its result/wake.
    // The parent can now send a ping behind that ready handler, without a sleep.
    int fd = open(getenv("MCP_CONSOLE_TEST_REQUEST_RECORDED"), O_WRONLY | O_NONBLOCK);
    if (fd < 0 || write(fd, "1", 1) != 1) _exit(121);
    close(fd);
}

#ifdef __APPLE__
static int observed_clock_gettime(clockid_t clock, struct timespec *value) {
    observe_recording(clock);
    return clock_gettime(clock, value);
}
static int observed_cond_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    recorder_returned();
    return pthread_cond_wait(condition, mutex);
}
static int observed_timed_wait(pthread_cond_t *condition, pthread_mutex_t *mutex,
                               const struct timespec *timeout) {
    recorder_returned();
    return pthread_cond_timedwait_relative_np(condition, mutex, timeout);
}
#define INTERPOSE(replacement, original)                                      \
    __attribute__((used)) static struct {                                     \
        const void *replacement;                                              \
        const void *original;                                                 \
    } interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                                \
        (const void *)(uintptr_t)&original,                                   \
    };
INTERPOSE(observed_clock_gettime, clock_gettime)
INTERPOSE(observed_cond_wait, pthread_cond_wait)
INTERPOSE(observed_timed_wait, pthread_cond_timedwait_relative_np)
#else
int clock_gettime(clockid_t clock, struct timespec *value) {
    observe_recording(clock);
    return native_clock_gettime(clock, value);
}
long syscall(long number, ...) {
    va_list arguments;
    va_start(arguments, number);
    // Forward the six machine-word syscall argument slots; Linux ignores slots
    // beyond the selected syscall's arity. Rust's pool uses futex wait/bitset.
    long slots[6];
    for (unsigned i = 0; i < 6; ++i) slots[i] = va_arg(arguments, long);
    va_end(arguments);
    if (number == SYS_futex) {
        int operation = (int)slots[1] & FUTEX_CMD_MASK;
        if ((operation == FUTEX_WAIT || operation == FUTEX_WAIT_BITSET) && slots[3] != 0)
            recorder_returned();
    }
    return native_syscall(number, slots[0], slots[1], slots[2], slots[3], slots[4], slots[5]);
}
#endif
