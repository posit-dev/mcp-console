#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <errno.h>
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
static atomic_bool published;
static _Thread_local bool recording;
#ifdef __APPLE__
static _Thread_local pthread_mutex_t *locks[64];
static _Thread_local size_t lock_count;
static _Thread_local pthread_mutex_t *recording_mutex;
static bool dispatch_pumped;
static bool dispatch_notified;
#endif

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
        && armed && access(armed, F_OK) == 0 && !atomic_exchange(&observed, true)) {
        recording = true;
#ifdef __APPLE__
        if (getenv("MCP_CONSOLE_TEST_REQUEST_RELEASE") != NULL) {
            if (lock_count == 0) _exit(122);
            recording_mutex = locks[lock_count - 1];
        }
#endif
    }
}

static void recorder_returned(void) {
    if (!recording) return;
    recording = false;
    atomic_store(&published, true);
    // A pool wait follows task completion and publication of its result/wake.
    // The parent can now send a ping behind that ready handler, without a sleep.
    int fd = open(getenv("MCP_CONSOLE_TEST_REQUEST_RECORDED"), O_WRONLY | O_NONBLOCK);
    if (fd < 0 || write(fd, "1", 1) != 1) _exit(121);
    close(fd);
}

#ifdef __APPLE__
static bool observe_dispatch(void) {
    const char *path = getenv("MCP_CONSOLE_TEST_REQUEST_DISPATCHED");
    if (!path || !pthread_equal(pthread_self(), main_thread) ||
        !atomic_load(&published)) return false;
    // Force one spurious park return so the current-thread runtime drains the
    // recorder's published wake before acknowledging the handler's dispatch.
    if (!dispatch_pumped) {
        dispatch_pumped = true;
        return true;
    }
    if (!dispatch_notified) {
        dispatch_notified = true;
        int descriptor = open(path, O_WRONLY | O_NONBLOCK);
        if (descriptor < 0 || write(descriptor, "1", 1) != 1) _exit(126);
        close(descriptor);
    }
    return false;
}

static int observed_mutex_lock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_lock(mutex);
    if (result == 0) {
        if (lock_count == 64) _exit(123);
        locks[lock_count++] = mutex;
    }
    return result;
}

static int observed_mutex_unlock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_unlock(mutex);
    if (result != 0) return result;
    if (lock_count != 0 && locks[lock_count - 1] == mutex) --lock_count;
    if (mutex == recording_mutex) {
        recording_mutex = NULL;
        // The call's record is complete and its transcript lock is released.
        // Hold only this handler's task return while another call completes.
        int reached = open(getenv("MCP_CONSOLE_TEST_REQUEST_PAUSED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_REQUEST_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(124);
        close(reached);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(125);
        close(release);
    }
    return result;
}

static int observed_clock_gettime(clockid_t clock, struct timespec *value) {
    observe_recording(clock);
    return clock_gettime(clock, value);
}
static int observed_cond_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    recorder_returned();
    if (observe_dispatch()) return 0;
    return pthread_cond_wait(condition, mutex);
}
static int observed_timed_wait(pthread_cond_t *condition, pthread_mutex_t *mutex,
                               const struct timespec *timeout) {
    recorder_returned();
    if (observe_dispatch()) return 0;
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
INTERPOSE(observed_mutex_lock, pthread_mutex_lock)
INTERPOSE(observed_mutex_unlock, pthread_mutex_unlock)
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
