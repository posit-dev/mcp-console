#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Thread_local pthread_mutex_t *locks[64];
static _Thread_local size_t lock_count;
static _Thread_local pthread_mutex_t *attachment;
static int selected;
static int cell_output = -1;

__attribute__((constructor)) static void initialize(void) {
    // This gate belongs to the server, not its resolver or worker children.
    if (unsetenv("DYLD_INSERT_LIBRARIES") != 0) _exit(120);
}

static int observed_lock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_lock(mutex);
    if (result == 0) {
        if (lock_count == 64) _exit(121);
        locks[lock_count++] = mutex;
    }
    return result;
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
    if (result >= 0 && strstr(path, "/outputs/call-") != NULL &&
        __atomic_exchange_n(&selected, 1, __ATOMIC_RELAXED) == 0) {
        // Cell attachment holds the operation lock outside the transcript lock.
        // Stop only after it is released and the Cell route has been installed.
        if (lock_count < 2) _exit(122);
        attachment = locks[lock_count - 2];
        __atomic_store_n(&cell_output, result, __ATOMIC_RELAXED);
    }
    return result;
}

static int observed_close(int descriptor) {
    int result = close(descriptor);
    int expected = descriptor;
    if (result == 0 && __atomic_compare_exchange_n(
            &cell_output, &expected, -1, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {
        // Closing this cell's recorded output proves completion, independent
        // of whether a send has collected its response yet.
        int completed = open(getenv("MCP_CONSOLE_TEST_CELL_COMPLETED"), O_WRONLY);
        if (completed < 0 || write(completed, "1", 1) != 1) _exit(125);
        close(completed);
    }
    return result;
}

static int observed_unlock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_unlock(mutex);
    if (result != 0) return result;
    if (lock_count != 0 && locks[lock_count - 1] == mutex) --lock_count;
    if (mutex == attachment) {
        attachment = NULL;
        int reached = open(getenv("MCP_CONSOLE_TEST_CELL_ATTACHED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_CELL_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(123);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(124);
        close(reached);
        close(release);
    }
    return result;
}

#define INTERPOSE(replacement, original)                                      \
    __attribute__((used)) static struct {                                     \
        const void *replacement;                                             \
        const void *original;                                                \
    } interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                                \
        (const void *)(uintptr_t)&original,                                   \
    };
INTERPOSE(observed_lock, pthread_mutex_lock)
INTERPOSE(observed_unlock, pthread_mutex_unlock)
INTERPOSE(observed_open, open)
INTERPOSE(observed_close, close)
