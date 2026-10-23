#include <stddef.h>
#include <stdlib.h>
#include <unistd.h>

// Exercise the public launcher with a 1 MiB maximum individual allocation.
// The sandbox removes this interposer before starting the private runner.
static void check_size(size_t size) {
    if (size > 1024 * 1024) {
        _exit(86);
    }
}

static void *bounded_malloc(size_t size) {
    check_size(size);
    return malloc(size);
}

static void *bounded_calloc(size_t count, size_t size) {
    if (size && count > 1024 * 1024 / size) {
        _exit(86);
    }
    return calloc(count, size);
}

static void *bounded_realloc(void *pointer, size_t size) {
    check_size(size);
    return realloc(pointer, size);
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                    \
        const void *replacement;                                             \
        const void *replacee;                                                \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(replacement), (const void *)(replacee)};

DYLD_INTERPOSE(bounded_malloc, malloc)
DYLD_INTERPOSE(bounded_calloc, calloc)
DYLD_INTERPOSE(bounded_realloc, realloc)
