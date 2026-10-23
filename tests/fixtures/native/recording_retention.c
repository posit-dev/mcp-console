#define _GNU_SOURCE
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

#ifdef __APPLE__
#include <malloc/malloc.h>
#define allocation_size malloc_size
#define native_malloc malloc
#define native_calloc calloc
#define native_realloc realloc
#define native_free free
#define RESOLVE(name) ((void)0)
#else
#include <dlfcn.h>
#include <malloc.h>
#define allocation_size malloc_usable_size
static void *(*native_malloc)(size_t);
static void *(*native_calloc)(size_t, size_t);
static void *(*native_realloc)(void *, size_t);
static void (*native_free)(void *);
#define RESOLVE(name) do { \
    if (native_##name == NULL) native_##name = dlsym(RTLD_NEXT, #name); \
    if (native_##name == NULL) _exit(120); \
} while (0)
#endif

// Count live large server allocations, not RSS or allocator cache retention.
// Small allocations and allocations in the worker do not affect this bound.
struct retention_profile {
    _Atomic int64_t live;
};
static struct retention_profile *retained;
static pid_t owner;

__attribute__((constructor)) static void initialize(void) {
    RESOLVE(malloc);
    RESOLVE(calloc);
    RESOLVE(realloc);
    RESOLVE(free);
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    int descriptor = open(getenv("MCP_CONSOLE_TEST_RETENTION_PROFILE"), O_RDWR);
    if (descriptor < 0) _exit(121);
    void *mapping = mmap(NULL, sizeof(struct retention_profile), PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
    close(descriptor);
    if (mapping == MAP_FAILED) _exit(122);
    retained = mapping;
}

static void observe(void *pointer, int direction) {
    if (retained == NULL || getpid() != owner || pointer == NULL) return;
    size_t size = allocation_size(pointer);
    if (size >= 256 * 1024) {
        atomic_fetch_add(&retained->live, direction * (int64_t)size);
    }
}

static void *observed_malloc(size_t size) {
    RESOLVE(malloc);
    void *pointer = native_malloc(size);
    observe(pointer, 1);
    return pointer;
}
static void *observed_calloc(size_t count, size_t size) {
    RESOLVE(calloc);
    void *pointer = native_calloc(count, size);
    observe(pointer, 1);
    return pointer;
}
static void *observed_realloc(void *old, size_t size) {
    RESOLVE(realloc);
    observe(old, -1);
    void *pointer = native_realloc(old, size);
    observe(pointer == NULL && size != 0 ? old : pointer, 1);
    return pointer;
}
static void observed_free(void *pointer) {
    RESOLVE(free);
    observe(pointer, -1);
    native_free(pointer);
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original \
    };
INTERPOSE(observed_malloc, malloc)
INTERPOSE(observed_calloc, calloc)
INTERPOSE(observed_realloc, realloc)
INTERPOSE(observed_free, free)
#else
void *malloc(size_t size) { return observed_malloc(size); }
void *calloc(size_t count, size_t size) { return observed_calloc(count, size); }
void *realloc(void *pointer, size_t size) { return observed_realloc(pointer, size); }
void free(void *pointer) { observed_free(pointer); }
#endif
