#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Thread_local bool evaluator;
static _Atomic(pthread_cond_t *) bootstrap_condition;
static atomic_bool receipt_processed;
static bool before_wait;

__attribute__((constructor)) static void initialize(void) {
    before_wait = getenv("MCP_CONSOLE_TEST_EVALUATOR_BEFORE_WAIT") != NULL;
    // The server owns this scheduler gate; only the existing worker receipt
    // fixture reaches preparation/worker children.
    if (setenv("DYLD_INSERT_LIBRARIES",
               getenv("MCP_CONSOLE_TEST_BOOTSTRAP_INTERPOSER"), 1) != 0) _exit(120);
}

static void notify(const char *name) {
    int descriptor = open(getenv(name), O_WRONLY | O_NONBLOCK);
    if (descriptor < 0 || write(descriptor, "1", 1) != 1) _exit(121);
    close(descriptor);
}

static void hold_evaluator(pthread_mutex_t *mutex) {
    // A scheduler delay must not retain the operation mutex needed by receipt
    // processing and interrupt admission. Condvar waits permit spurious wakes.
    if (pthread_mutex_unlock(mutex) != 0) _exit(122);
    evaluator = false;
    notify("MCP_CONSOLE_TEST_EVALUATOR_REACHED");
    int descriptor = open(getenv("MCP_CONSOLE_TEST_EVALUATOR_RELEASE"), O_RDONLY);
    if (descriptor < 0) _exit(123);
    char token;
    ssize_t count;
    do { count = read(descriptor, &token, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(124);
    close(descriptor);
    if (pthread_mutex_lock(mutex) != 0) _exit(125);
}

static int observed_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list arguments;
        va_start(arguments, flags);
        mode = va_arg(arguments, int);
        va_end(arguments);
    }
    int result = open(path, flags, mode);
    const char suffix[] = "/outputs/call-000001.log";
    size_t length = strlen(path);
    if (result >= 0 && length >= sizeof(suffix) - 1 &&
        strcmp(path + length - (sizeof(suffix) - 1), suffix) == 0) {
        // begin_cell creates the first cell's recorded output on the evaluator
        // thread, before attaching stdin and entering its bootstrap wait.
        evaluator = true;
    }
    return result;
}

static int observed_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    if (evaluator) {
        atomic_store(&bootstrap_condition, condition);
        if (before_wait) {
            hold_evaluator(mutex);
            return 0;
        }
        // Keep the operation mutex until the native wait atomically releases
        // it. Receipt processing cannot race ahead of this wait-entry marker.
        notify("MCP_CONSOLE_TEST_EVALUATOR_WAITING");
    }
    int result = pthread_cond_wait(condition, mutex);
    if (result == 0 && evaluator && atomic_load(&receipt_processed))
        hold_evaluator(mutex);
    return result;
}

static int observed_broadcast(pthread_cond_t *condition) {
    bool bootstrap = condition == atomic_load(&bootstrap_condition);
    // finish_bootstrap commits its receipt and drops the operation mutex before
    // broadcasting. Publish the fact before waking its evaluator.
    if (bootstrap) atomic_store(&receipt_processed, true);
    int result = pthread_cond_broadcast(condition);
    if (bootstrap && result == 0) notify("MCP_CONSOLE_TEST_RECEIPT_PROCESSED");
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
INTERPOSE(observed_open, open)
INTERPOSE(observed_wait, pthread_cond_wait)
INTERPOSE(observed_broadcast, pthread_cond_broadcast)
