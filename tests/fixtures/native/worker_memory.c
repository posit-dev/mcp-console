#ifdef __linux__
#define _GNU_SOURCE
#endif

#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __APPLE__
#include <libproc.h>
#include <sys/sysctl.h>
#else
#include <sys/syscall.h>
#endif

static const char *root;
static pid_t controller;

__attribute__((constructor)) static void initialize_memory_fixture(void) {
    controller = getpid();
    root = getenv("MCP_CONSOLE_TEST_MEMORY_ROOT");
    // Alter host observations only in the controller, never the workload.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static uint64_t resident_bytes(int pid) {
    char path[4096];
    if (root == NULL || getpid() != controller) return 0;
    if (snprintf(path, sizeof(path), "%s/target", root) >= (int)sizeof(path)) _exit(125);
    FILE *target = fopen(path, "r");
    if (target == NULL) return 0;
    int selected = 0;
    unsigned int resident_mib = 0;
    int count;
    while ((count = fscanf(target, "%d %u", &selected, &resident_mib)) == 2 &&
           selected != pid) {}
    fclose(target);
    if (count != 2 || selected != pid) return 0;
    if (snprintf(path, sizeof(path), "%s/sampled", root) >= (int)sizeof(path)) _exit(125);
    int sampled = open(path, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    if (sampled < 0 || write(sampled, "1", 1) != 1) _exit(125);
    close(sampled);
    return (uint64_t)resident_mib * 1024 * 1024;
}

#ifdef __APPLE__
static int fixture_sysctl(int *name, unsigned int count, void *old_value,
                          size_t *old_length, void *new_value, size_t new_length) {
    int result = sysctl(name, count, old_value, old_length, new_value, new_length);
    if (result == 0 && root != NULL && getpid() == controller && count == 2 &&
        name[0] == CTL_HW && name[1] == HW_MEMSIZE && old_value != NULL &&
        *old_length == sizeof(uint64_t)) {
        *(uint64_t *)old_value = 1024ULL * 1024 * 1024;
    }
    return result;
}

static int fixture_proc_pidinfo(int pid, int flavor, uint64_t argument,
                                void *buffer, int size) {
    int result = proc_pidinfo(pid, flavor, argument, buffer, size);
    if (root != NULL && getpid() == controller && flavor == PROC_PIDTASKINFO &&
        result == sizeof(struct proc_taskinfo)) {
        ((struct proc_taskinfo *)buffer)->pti_resident_size = resident_bytes(pid);
    }
    return result;
}

#define DYLD_INTERPOSE(replacement, replacee)                                \
    __attribute__((used)) static struct {                                    \
        const void *replacement;                                            \
        const void *replacee;                                                \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                               \
        (const void *)(uintptr_t)&replacee,                                  \
    };

DYLD_INTERPOSE(fixture_sysctl, sysctl)
DYLD_INTERPOSE(fixture_proc_pidinfo, proc_pidinfo)
#else
static int fixture_open(const char *path, int flags, mode_t mode) {
    char contents[256];
    int pid;
    int end = 0;
    if (root != NULL && getpid() == controller && strcmp(path, "/proc/meminfo") == 0) {
        snprintf(contents, sizeof(contents),
                 "MemTotal: 1048576 kB\nMemFree: 1048576 kB\nMemAvailable: 1048576 kB\n");
    } else if (root != NULL && getpid() == controller &&
               sscanf(path, "/proc/%d/statm%n", &pid, &end) == 1 &&
               end > 0 && path[end] == '\0') {
        snprintf(contents, sizeof(contents), "0 %llu\n",
                 (unsigned long long)(resident_bytes(pid) / sysconf(_SC_PAGESIZE)));
    } else {
        return syscall(SYS_openat, AT_FDCWD, path, flags, mode);
    }
    int descriptor = syscall(SYS_memfd_create, "memory-observation", 1);
    size_t length = strlen(contents);
    if (descriptor < 0 || write(descriptor, contents, length) != (ssize_t)length ||
        lseek(descriptor, 0, SEEK_SET) < 0) _exit(125);
    return descriptor;
}

int open(const char *path, int flags, ...) {
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = flags & O_CREAT ? va_arg(arguments, int) : 0;
    va_end(arguments);
    return fixture_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = flags & O_CREAT ? va_arg(arguments, int) : 0;
    va_end(arguments);
    return fixture_open(path, flags, mode);
}
#endif
