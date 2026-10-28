#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

static pthread_t main_thread;
static atomic_uint attempts;
static unsigned admission_ordinal = 1;
static _Atomic(pthread_cond_t *) main_condition;
static _Atomic(pthread_mutex_t *) main_mutex;
static atomic_bool input_returned;
static bool input_dispatched;
static bool shutdown_observed;
static _Thread_local bool input_eof;

__attribute__((constructor)) static void initialize(void) {
    main_thread = pthread_self();
    const char *ordinal = getenv("MCP_CONSOLE_TEST_ADMISSION_ORDINAL");
    if (ordinal) admission_ordinal = (unsigned)atoi(ordinal);
    // Only the server is gated, never its preparation children.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");

}

static void notify(const char *name) {
    int descriptor = open(getenv(name), O_WRONLY | O_NONBLOCK);
    if (descriptor < 0 || write(descriptor, "1", 1) != 1) _exit(121);
    close(descriptor);
}

static void gate_retry(void) {
    notify("MCP_CONSOLE_TEST_ADMISSION_REACHED");
    int descriptor = open(getenv("MCP_CONSOLE_TEST_ADMISSION_RELEASE"), O_RDONLY);
    if (descriptor < 0) _exit(122);
    char token;
    ssize_t count;
    do {
        count = read(descriptor, &token, 1);
    } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(123);
    close(descriptor);
}

static int observed_fstat(int descriptor, struct stat *status) {
    // Each startup attempt inspects MCP stdin before admission. Hold the
    // selected attempt here; the protocol reader and shutdown remain responsive.
    if (descriptor == STDIN_FILENO && atomic_fetch_add(&attempts, 1) == admission_ordinal) gate_retry();
    return fstat(descriptor, status);
}

static ssize_t observed_read(int descriptor, void *buffer, size_t count) {
    ssize_t result = read(descriptor, buffer, count);
    if (descriptor == STDIN_FILENO && result == 0) input_eof = true;
    return result;
}

static void before_park(void) {
    // Returning to Tokio's blocking pool proves the stdin EOF result has been
    // delivered to the async reader, rather than just read by its OS thread.
    if (input_eof) {
        input_eof = false;
        atomic_store(&input_returned, true);
        // Wake the runtime even if it consumed EOF before this thread returned
        // to its pool. Its next full park proves shutdown ran; no timer is needed.
        pthread_mutex_t *mutex = atomic_load(&main_mutex);
        pthread_cond_t *condition = atomic_load(&main_condition);
        if (!mutex || !condition) _exit(124);
        pthread_mutex_lock(mutex);
        pthread_cond_signal(condition);
        pthread_mutex_unlock(mutex);
    }
    // The current-thread runtime has dispatched the EOF wake, then run ready
    // tasks before parking again. Shutdown has closed startup admission and
    // now awaits the gated attempt. No sleep or scheduler delay establishes it.
    if (pthread_equal(pthread_self(), main_thread) && input_dispatched && !shutdown_observed) {
        shutdown_observed = true;
        notify("MCP_CONSOLE_TEST_ADMISSION_SHUTDOWN");
    }
}

static void after_park(void) {
    if (pthread_equal(pthread_self(), main_thread) && atomic_load(&input_returned))
        input_dispatched = true;
}

static bool before_cond_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    if (pthread_equal(pthread_self(), main_thread)) {
        atomic_store(&main_mutex, mutex);
        atomic_store(&main_condition, condition);
        // A spurious wake closes the race between EOF delivery and entering
        // this wait. Run ready tasks once before acknowledging shutdown.
        if (atomic_load(&input_returned) && !input_dispatched) {
            input_dispatched = true;
            return true;
        }
    }
    before_park();
    return false;
}

static int observed_timed_wait(pthread_cond_t *condition, pthread_mutex_t *mutex,
                               const struct timespec *timeout) {
    if (before_cond_wait(condition, mutex)) return 0;
    int result = pthread_cond_timedwait_relative_np(condition, mutex, timeout);
    after_park();
    return result;
}

static int observed_cond_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    if (before_cond_wait(condition, mutex)) return 0;
    int result = pthread_cond_wait(condition, mutex);
    after_park();
    return result;
}

#define INTERPOSE(replacement, original)                                      \
    __attribute__((used)) static struct {                                     \
        const void *replacement;                                              \
        const void *original;                                                 \
    } interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                                \
        (const void *)(uintptr_t)&original,                                   \
    };
INTERPOSE(observed_fstat, fstat)
INTERPOSE(observed_read, read)
INTERPOSE(observed_timed_wait, pthread_cond_timedwait_relative_np)
INTERPOSE(observed_cond_wait, pthread_cond_wait)
