#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Thread_local pthread_mutex_t *held[32];
static _Thread_local size_t held_count;
static _Thread_local pthread_mutex_t *recording;
static _Atomic(pthread_mutex_t *) journal;

static int observed_lock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_lock(mutex);
    if (result == 0) {
        if (held_count == sizeof(held) / sizeof(*held)) _exit(120);
        held[held_count++] = mutex;
    }
    return result;
}

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
}

static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
    ssize_t result = write(descriptor, buffer, length);
    const char call[] = "{\"event\":\"tool_call\",";
    if (length >= sizeof(call) - 1 && memcmp(buffer, call, sizeof(call) - 1) == 0 && held_count != 0)
        atomic_store(&journal, held[held_count - 1]);
    const char *marker = getenv("MCP_CONSOLE_TEST_HOLD_RESULT") != NULL
        ? "{\"event\":\"tool_result\",\"call_id\":2,"
        : "{\"event\":\"tool_call\",\"call_id\":2,";
    if (result == (ssize_t)length && length >= strlen(marker) &&
        memcmp(buffer, marker, strlen(marker)) == 0)
        recording = atomic_load(&journal);
    const char ended[] = "{\"event\":\"recording_ended\",";
    const char *finalized = getenv("MCP_CONSOLE_TEST_RECORDING_FINALIZED");
    if (finalized != NULL && result == (ssize_t)length && length >= sizeof(ended) - 1 &&
        memcmp(buffer, ended, sizeof(ended) - 1) == 0) {
        int output = open(finalized, O_WRONLY | O_NONBLOCK);
        if (output < 0 || write(output, "1", 1) != 1) _exit(124);
        close(output);
    }
    return result;
}

static int observed_unlock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_unlock(mutex);
    if (result == 0) {
        for (size_t index = held_count; index != 0; --index) {
            if (held[index - 1] == mutex) {
                memmove(held + index - 1, held + index,
                        (held_count - index) * sizeof(*held));
                --held_count;
                break;
            }
        }
    }
    if (result == 0 && recording == mutex) {
        // Pause the recording task after releasing its journal lock. An
        // earlier response can now finish and cancel the transport's receive.
        recording = NULL;
        int reached = open(getenv("MCP_CONSOLE_TEST_RECORDING_REACHED"), O_WRONLY);
        if (reached < 0 || write(reached, "1", 1) != 1) _exit(121);
        close(reached);
        int release = open(getenv("MCP_CONSOLE_TEST_RECORDING_RELEASE"), O_RDONLY);
        if (release < 0) _exit(122);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(123);
        close(release);
    }
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
INTERPOSE(observed_write, write)
INTERPOSE(observed_lock, pthread_mutex_lock)
INTERPOSE(observed_unlock, pthread_mutex_unlock)
