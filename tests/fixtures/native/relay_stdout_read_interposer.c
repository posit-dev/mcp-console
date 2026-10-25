#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_bool claimed = false;
static atomic_uintptr_t blocked_thread = 0;
#define TRACKED_DESCRIPTORS 1024
static atomic_size_t match_lengths[TRACKED_DESCRIPTORS];

// One relay reader owns this bounded startup replay; no bytes are discarded.
static int replay_descriptor = -1;
static unsigned char replay[64 * 1024];
static size_t replay_length;
static size_t replay_offset;
static bool replay_ready;


typedef ssize_t (*read_function)(int, void *, size_t);
typedef int (*poll_function)(struct pollfd *, nfds_t, int);
typedef int (*pthread_join_function)(pthread_t, void **);

static read_function next_read(void) {
#ifdef __APPLE__
    return read;
#else
    return (read_function)dlsym(RTLD_NEXT, "read");
#endif
}

static poll_function next_poll(void) {
#ifdef __APPLE__
    return poll;
#else
    return (poll_function)dlsym(RTLD_NEXT, "poll");
#endif
}

static pthread_join_function next_pthread_join(void) {
#ifdef __APPLE__
    return pthread_join;
#else
    return (pthread_join_function)dlsym(RTLD_NEXT, "pthread_join");
#endif
}

static bool target_process(void) {
    const char *value = getenv("MCP_CONSOLE_TEST_RELAY_READ_PID");
    if (value == NULL) {
        return false;
    }
    char *end = NULL;
    long process_id = strtol(value, &end, 10);
    return end != value && *end == '\0' && process_id == getpid();
}

static bool contains(int descriptor, const void *buffer, size_t length,
                     const char *match) {
    size_t match_length = strlen(match);
    if (match_length == 0) {
        return false;
    }
    size_t matched = 0;
    if (descriptor >= 0 && descriptor < TRACKED_DESCRIPTORS) {
        matched = atomic_load(&match_lengths[descriptor]);
    }
    const unsigned char *bytes = buffer;
    for (size_t index = 0; index < length; ++index) {
        if (bytes[index] == (unsigned char)match[matched]) {
            ++matched;
        } else {
            matched = bytes[index] == (unsigned char)match[0] ? 1 : 0;
        }
        if (matched == match_length) {
            if (descriptor >= 0 && descriptor < TRACKED_DESCRIPTORS) {
                atomic_store(&match_lengths[descriptor], 0);
            }
            return true;
        }
    }
    if (descriptor >= 0 && descriptor < TRACKED_DESCRIPTORS) {
        atomic_store(&match_lengths[descriptor], matched);
    }
    return false;
}

static void notify(const char *variable) {
    const char *path = getenv(variable);
    if (path == NULL) {
        return;
    }
    int descriptor;
    do {
        descriptor = open(path, O_WRONLY | O_NONBLOCK);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        return;
    }
    (void)write(descriptor, "1", 1);
    close(descriptor);
}

static void release_blocked_read(void) {
    notify("MCP_CONSOLE_TEST_RELAY_READ_RELEASE");
}

static void await_bootstrap(int descriptor, const void *buffer, size_t length,
                            int release_descriptor, read_function read_next) {
    const char *match = getenv("MCP_CONSOLE_TEST_RELAY_READ_AWAIT_MATCH");
    if (match == NULL || contains(descriptor, buffer, length, match)) return;
    replay_descriptor = descriptor;
    while (true) {
        // Keep join/cancellation able to release the gate during a failed setup.
        struct pollfd waiting[] = {
            {.fd = descriptor, .events = POLLIN},
            {.fd = release_descriptor, .events = POLLIN},
        };
        int available;
        do { available = poll(waiting, 2, -1); } while (available < 0 && errno == EINTR);
        if (available < 0) _exit(125);
        if (waiting[1].revents != 0) return;
        if (replay_length == sizeof(replay)) _exit(125);
        ssize_t count;
        do {
            count = read_next(descriptor, replay + replay_length,
                              sizeof(replay) - replay_length);
        } while (count < 0 && errno == EINTR);
        if (count <= 0) _exit(125);
        bool reached = contains(descriptor, replay + replay_length, (size_t)count, match);
        replay_length += (size_t)count;
        if (reached) return;
    }
}

static bool replay_owner(void) {
    uintptr_t owner = atomic_load(&blocked_thread);
    return owner != 0 && pthread_equal(pthread_self(), (pthread_t)owner);
}

static ssize_t delayed_read(int descriptor, void *buffer, size_t length) {
    if (replay_owner() && descriptor == replay_descriptor &&
        replay_offset < replay_length) {
        size_t count = replay_length - replay_offset;
        if (count > length) count = length;
        memcpy(buffer, replay + replay_offset, count);
        replay_offset += count;
        return (ssize_t)count;
    }
    read_function read_next = next_read();
    // Deliver complete earlier frames before withholding a later match, even
    // when the pipe coalesces Ready and RuntimeInitialized into one native read.
    // This opt-in checkpoint needs no extra buffer or protocol parser.
    if (length > 1 && target_process() && !atomic_load(&claimed) &&
        getenv("MCP_CONSOLE_TEST_RELAY_READ_BYTEWISE") != NULL) {
        length = 1;
    }
    ssize_t result = read_next(descriptor, buffer, length);
    const char *match = getenv("MCP_CONSOLE_TEST_RELAY_READ_MATCH");
    if (result <= 0 || !target_process() || match == NULL ||
        !contains(descriptor, buffer, (size_t)result, match) ||
        atomic_exchange(&claimed, true)) {
        return result;
    }

    const char *release = getenv("MCP_CONSOLE_TEST_RELAY_READ_RELEASE");
    if (release == NULL) {
        return result;
    }
    int release_descriptor;
    do {
        release_descriptor = open(release, O_RDWR);
    } while (release_descriptor < 0 && errno == EINTR);
    if (release_descriptor < 0) {
        return result;
    }
    // Establish the release reader before publishing the blocked thread, so
    // join can release it even if the read thread is immediately descheduled.
    atomic_store(&blocked_thread, (uintptr_t)pthread_self());
    await_bootstrap(descriptor, buffer, (size_t)result, release_descriptor, read_next);
    replay_ready = true;
    notify("MCP_CONSOLE_TEST_RELAY_READ_BLOCKED");
    char token;
    while (read_next(release_descriptor, &token, 1) < 0 && errno == EINTR) {
    }
    close(release_descriptor);
    return result;
}

static int replay_poll(struct pollfd *descriptors, nfds_t count, int timeout) {
    poll_function poll_next = next_poll();
    if (!replay_owner() || !replay_ready || replay_offset == replay_length)
        return poll_next(descriptors, count, timeout);
    bool buffered = false;
    for (nfds_t index = 0; index < count; ++index)
        buffered |= descriptors[index].fd == replay_descriptor &&
                    (descriptors[index].events & POLLIN) != 0;
    if (!buffered) return poll_next(descriptors, count, timeout);
    int ready = poll_next(descriptors, count, 0);
    if (ready < 0) return ready;
    ready = 0;
    for (nfds_t index = 0; index < count; ++index) {
        // The replay buffer is readable even after draining the native pipe.
        if (descriptors[index].fd == replay_descriptor &&
            (descriptors[index].events & POLLIN) != 0)
            descriptors[index].revents |= POLLIN;
        ready += descriptors[index].revents != 0;
    }
    return ready;
}

static int releasing_pthread_join(pthread_t thread, void **result) {
    pthread_join_function join_next = next_pthread_join();
    uintptr_t blocked = atomic_load(&blocked_thread);
    if (blocked != 0 && pthread_equal(thread, (pthread_t)blocked)) {
        release_blocked_read();
    }
    return join_next(thread, result);
}

__attribute__((constructor)) static void prevent_child_injection(void) {
    if (target_process()) {
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

#ifdef __APPLE__
#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(delayed_read, read)
DYLD_INTERPOSE(replay_poll, poll)
DYLD_INTERPOSE(releasing_pthread_join, pthread_join)

#else
ssize_t read(int descriptor, void *buffer, size_t length) {
    return delayed_read(descriptor, buffer, length);
}
int poll(struct pollfd *descriptors, nfds_t count, int timeout) {
    return replay_poll(descriptors, count, timeout);
}
int pthread_join(pthread_t thread, void **result) {
    return releasing_pthread_join(thread, result);
}
#endif
