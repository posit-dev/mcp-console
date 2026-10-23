#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pid_t owner;
static const char *cell_output_suffix;
typedef int (*close_function)(int);

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    cell_output_suffix = getenv("MCP_CONSOLE_TEST_CELL_OUTPUT_SUFFIX");
    if (cell_output_suffix == NULL) { _exit(125); }
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static bool is_cell_output(int descriptor) {
    char path[PATH_MAX];
#ifdef __APPLE__
    if (fcntl(descriptor, F_GETPATH, path) != 0) { return false; }
#else
    char link[64];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", descriptor);
    ssize_t path_length = readlink(link, path, sizeof(path) - 1);
    if (path_length < 0) { return false; }
    path[path_length] = '\0';
#endif
    size_t suffix_length = strlen(cell_output_suffix);
    size_t length = strlen(path);
    return length >= suffix_length &&
        strcmp(path + length - suffix_length, cell_output_suffix) == 0;
}

static int observed_close(int descriptor) {
#ifdef __APPLE__
    close_function close_next = close;
#else
    close_function close_next = (close_function)dlsym(RTLD_NEXT, "close");
#endif
    const char *checkpoint = getenv("MCP_CONSOLE_TEST_CELL_OUTPUT_CLOSED");
    bool matched = getpid() == owner && checkpoint != NULL && is_cell_output(descriptor);
    int result = close_next(descriptor);
    int saved_errno = errno;
    if (matched) {
        // The server closes the cell log under the output lock. Any subsequent
        // worker output is therefore recorded after the cell has retired.
        int reached = open(checkpoint, O_WRONLY);
        if (result != 0 || reached < 0 || write(reached, "1", 1) != 1) { _exit(125); }
        close_next(reached);
    }
    errno = saved_errno;
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_close __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observed_close, (const void *)(uintptr_t)&close,
};
#else
int close(int descriptor) { return observed_close(descriptor); }
#endif
