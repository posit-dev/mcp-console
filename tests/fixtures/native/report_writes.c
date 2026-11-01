#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

static _Atomic uint64_t *written;
static pid_t owner;
static _Atomic dev_t device;
static _Atomic ino_t inode;

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    int descriptor = open(getenv("MCP_CONSOLE_TEST_REPORT_WRITES"), O_RDWR);
    if (descriptor < 0) _exit(121);
    void *mapping = mmap(NULL, sizeof(*written), PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
    close(descriptor);
    if (mapping == MAP_FAILED) _exit(122);
    written = mapping;
}

static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
#ifdef __APPLE__
    ssize_t result = write(descriptor, buffer, length);
#else
    ssize_t (*next_write)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
    ssize_t result = next_write(descriptor, buffer, length);
#endif
    if (getpid() != owner || result <= 0) return result;
    struct stat status;
    if (fstat(descriptor, &status) != 0 || !S_ISREG(status.st_mode)) return result;
    const char header[] = "# MCP Console session ";
    if ((size_t)result >= sizeof(header) - 1 && memcmp(buffer, header, sizeof(header) - 1) == 0) {
        device = status.st_dev;
        inode = status.st_ino;
    }
    if (inode != 0 && device == status.st_dev && inode == status.st_ino) {
        atomic_fetch_add(written, (uint64_t)result);
    }
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observed_write, (const void *)(uintptr_t)&write,
};
#else
ssize_t write(int descriptor, const void *buffer, size_t length) {
    return observed_write(descriptor, buffer, length);
}
#endif
