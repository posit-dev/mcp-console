#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
#include <malloc.h>
#define allocation_size malloc_usable_size
static void *(*native_malloc)(size_t);
static void *(*native_calloc)(size_t, size_t);
static void *(*native_realloc)(void *, size_t);
static void (*native_free)(void *);
static ssize_t (*native_write)(int, const void *, size_t);
#define RESOLVE(name) do { \
    if (native_##name == NULL) native_##name = dlsym(RTLD_NEXT, #name); \
    if (native_##name == NULL) _exit(120); \
} while (0)
#else
#include <malloc/malloc.h>
#define allocation_size malloc_size
#define native_malloc malloc
#define native_calloc calloc
#define native_realloc realloc
#define native_free free
#define native_write write
#define RESOLVE(name) ((void)0)
#endif

// Requested allocation sizes, independent of allocator size classes or RSS.
// The test enables measurement only after the public worker startup completes.
struct profile {
    _Atomic uint64_t enabled;
    _Atomic uint64_t bytes;
    _Atomic uint64_t largest;
    _Atomic uint64_t pause_results;
};
static struct profile *profile;
static pid_t owner;
static int reached = -1;
static int release = -1;
static const char *thread_marker;
static pthread_t measured_thread;
static atomic_bool thread_selected;
static const char *dispatcher_exited;
static const char *dispatcher_armed;
static pthread_key_t dispatcher;
static atomic_bool dispatcher_selected;

static void notify_dispatcher_exit(void *value) {
    (void)value;
    int descriptor = open(dispatcher_exited, O_WRONLY | O_NONBLOCK);
    if (descriptor < 0 || native_write(descriptor, "1", 1) != 1) _exit(126);
    close(descriptor);
}

__attribute__((constructor)) static void initialize(void) {
    RESOLVE(malloc);
    RESOLVE(calloc);
    RESOLVE(realloc);
    RESOLVE(free);
    RESOLVE(write);
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    int descriptor = open(getenv("MCP_CONSOLE_TEST_ALLOCATION_PROFILE"), O_RDWR);
    if (descriptor < 0) _exit(121);
    void *mapping = mmap(NULL, sizeof(struct profile), PROT_READ | PROT_WRITE,
                         MAP_SHARED, descriptor, 0);
    close(descriptor);
    if (mapping == MAP_FAILED) _exit(122);
    profile = mapping;
    thread_marker = getenv("MCP_CONSOLE_TEST_ALLOCATION_THREAD_MARKER");
    dispatcher_exited = getenv("MCP_CONSOLE_TEST_DISPATCHER_EXITED");
    dispatcher_armed = getenv("MCP_CONSOLE_TEST_DISPATCHER_ARMED");
    if (dispatcher_exited != NULL &&
        (dispatcher_armed == NULL ||
         pthread_key_create(&dispatcher, notify_dispatcher_exit) != 0)) _exit(127);
    const char *checkpoint = getenv("MCP_CONSOLE_TEST_RESULT_REACHED");
    if (checkpoint != NULL) {
        reached = open(checkpoint, O_WRONLY);
        release = open(getenv("MCP_CONSOLE_TEST_RESULT_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0) _exit(123);
    }
}

static void observe(size_t size) {
    if (profile == NULL || getpid() != owner || !atomic_load(&profile->enabled)) return;
    if (thread_marker != NULL && (!atomic_load(&thread_selected) ||
        !pthread_equal(pthread_self(), measured_thread))) return;
    atomic_fetch_add(&profile->bytes, size);
    uint64_t previous = atomic_load(&profile->largest);
    while (size > previous &&
           !atomic_compare_exchange_weak(&profile->largest, &previous, size)) {}
}

static void *profile_malloc(size_t size) {
    RESOLVE(malloc);
    observe(size);
    return native_malloc(size);
}

static void profile_free(void *pointer) {
    RESOLVE(free);
    // The dispatcher constructs this final diagnostic after consuming the
    // worker outcome. Its thread exit proves WorkerEnded was queued, even while
    // the journal mutex is held by the result-write checkpoint below.
    const char marker[] = "worker stopped before interrupt completed";
    if (getpid() == owner && dispatcher_exited != NULL && pointer != NULL &&
        access(dispatcher_armed, F_OK) == 0 &&
        allocation_size(pointer) >= sizeof(marker) - 1 &&
        memcmp(pointer, marker, sizeof(marker) - 1) == 0 &&
        !atomic_exchange(&dispatcher_selected, true)) {
        if (pthread_setspecific(dispatcher, (void *)1) != 0) _exit(128);
    }
    native_free(pointer);
}

static void *profile_calloc(size_t count, size_t size) {
    RESOLVE(calloc);
    observe(count * size);
    return native_calloc(count, size);
}

static void *profile_realloc(void *pointer, size_t size) {
    RESOLVE(realloc);
    observe(size);
    return native_realloc(pointer, size);
}

static ssize_t profile_write(int descriptor, const void *buffer, size_t size) {
    RESOLVE(write);
    if (thread_marker != NULL && getpid() == owner && !atomic_load(&thread_selected) &&
        size >= strlen(thread_marker) && memcmp(buffer, thread_marker, strlen(thread_marker)) == 0) {
        // A raw-output write identifies its ingestion thread during warmup.
        // Recorder threads may drain different batches under different loads;
        // their allocations do not measure the direct UTF-8 decoder's copies.
        measured_thread = pthread_self();
        atomic_store(&thread_selected, true);
    }
    const char marker[] = "{\"event\":\"tool_result\",";
    if (profile != NULL && getpid() == owner && atomic_load(&profile->pause_results) &&
        size >= sizeof(marker) - 1 && memcmp(buffer, marker, sizeof(marker) - 1) == 0) {
        // The result is assembled and owns delivery before its journal append.
        // Hold that public recording write until the client cancels the call.
        if (native_write(reached, "1", 1) != 1) _exit(124);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(125);
    }
    return native_write(descriptor, buffer, size);
}

#ifdef __APPLE__
#define INTERPOSE(replacement, replacee) \
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } \
    interpose_##replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&replacee \
    };
INTERPOSE(profile_malloc, malloc)
INTERPOSE(profile_calloc, calloc)
INTERPOSE(profile_realloc, realloc)
INTERPOSE(profile_free, free)
INTERPOSE(profile_write, write)
#else
void *malloc(size_t size) { return profile_malloc(size); }
void *calloc(size_t count, size_t size) { return profile_calloc(count, size); }
void *realloc(void *pointer, size_t size) { return profile_realloc(pointer, size); }
void free(void *pointer) { profile_free(pointer); }
ssize_t write(int descriptor, const void *buffer, size_t size) {
    return profile_write(descriptor, buffer, size);
}
#endif
