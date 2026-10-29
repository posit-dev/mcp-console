#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Thread_local pthread_mutex_t *locks[64];
static _Thread_local size_t lock_count;
static atomic_bool attaching;
static atomic_bool interrupt_recorded;
static atomic_bool paused;
static atomic_uintptr_t operation;

__attribute__((constructor)) static void initialize(void) {
    // Observe only the controller; the worker keeps ordinary input handling.
    if (unsetenv("DYLD_INSERT_LIBRARIES") != 0) _exit(120);
}

static bool contains(const void *buffer, size_t length, const char *text) {
    size_t size = strlen(text);
    for (size_t offset = 0; offset + size <= length; ++offset)
        if (memcmp((const char *)buffer + offset, text, size) == 0) return true;
    return false;
}

static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
    ssize_t result = write(descriptor, buffer, length);
    if (result != (ssize_t)length) return result;
    if (contains(buffer, length, "\"event\":\"tool_call\"") &&
        contains(buffer, length, "selected interrupt target: "))
        atomic_store(&attaching, true);
    if (contains(buffer, length, "\"direction\":\"integrator_to_server\"") &&
        contains(buffer, length, "\"op\":\"interrupt\""))
        atomic_store(&interrupt_recorded, true);
    return result;
}

static int observed_lock(pthread_mutex_t *mutex) {
    if (lock_count == 0 && atomic_load(&interrupt_recorded) &&
        (uintptr_t)mutex == atomic_load(&operation) &&
        !atomic_exchange(&paused, true)) {
        // Selection takes this mutex under publication and lifecycle guards.
        // Its blocking-pool continuation takes it alone, immediately before
        // enqueueing the interrupt. Hold that continuation without any lock.
        int reached = open(getenv("MCP_CONSOLE_TEST_INTERRUPT_SELECTED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_INTERRUPT_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(121);
        close(reached);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(122);
        close(release);
    }
    int result = pthread_mutex_lock(mutex);
    if (result == 0) {
        if (lock_count == 64) _exit(123);
        locks[lock_count++] = mutex;
        if (atomic_load(&attaching) && lock_count == 4 &&
            atomic_load(&operation) == 0) {
            // Prelude adoption holds worker, operation, evaluation, and output
            // mutexes, as in cell_attachment_checkpoint.c.
            atomic_store(&operation, (uintptr_t)locks[1]);
            atomic_store(&attaching, false);
        }
    }
    return result;
}

static int observed_unlock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_unlock(mutex);
    if (result == 0 && lock_count != 0 && locks[lock_count - 1] == mutex)
        --lock_count;
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
INTERPOSE(observed_write, write)
