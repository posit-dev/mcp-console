#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <malloc.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

static _Atomic uint64_t *allocated;
static pid_t owner;
static ssize_t (*native_write)(int, const void *, size_t);

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    native_write = dlsym(RTLD_NEXT, "write");
    if (native_write == NULL) _exit(122);
    unsetenv("LD_PRELOAD");
    int descriptor = open(getenv("MCP_CONSOLE_TEST_ASSOCIATION_HEAP"), O_RDWR);
    if (descriptor < 0) _exit(120);
    void *mapping = mmap(NULL, sizeof(*allocated), PROT_READ | PROT_WRITE,
                         MAP_SHARED, descriptor, 0);
    close(descriptor);
    if (mapping == MAP_FAILED) _exit(121);
    allocated = mapping;
}

ssize_t write(int descriptor, const void *buffer, size_t length) {
    if (descriptor == STDOUT_FILENO && getpid() == owner && allocated != NULL) {
        // Snapshot outside allocator callbacks, before the client receives the
        // reply. Fixed allocator caches are warmed before measuring growth.
        struct mallinfo2 heap = mallinfo2();
        atomic_store(allocated, heap.uordblks + heap.hblkhd);
    }
    return native_write(descriptor, buffer, length);
}
