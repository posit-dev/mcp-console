#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#endif

static bool await_closed_input;

static void reached(void) {
    const char *path = getenv("MCP_CONSOLE_TEST_WARMUP_HANDSHAKE_REACHED");
    int descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (descriptor < 0 || write(descriptor, "1", 1) != 1) _exit(125);
    close(descriptor);
}

__attribute__((constructor)) static void initialize(int argc, char **argv) {
#ifdef __APPLE__
    argc = *_NSGetArgc();
    argv = *_NSGetArgv();
#endif
    const char *order = getenv("MCP_CONSOLE_TEST_WARMUP_HANDSHAKE");
    if (argc < 2 || order == NULL) return;
    await_closed_input = strcmp(argv[1], "warmup") == 0 && strcmp(order, "closed") == 0;
    if (strcmp(argv[1], "serve") == 0 && strcmp(order, "closed") == 0) {
        // Keep stdout open until the parent attempts initialization, so its
        // transport cannot observe EOF before reaching the write checkpoint.
        // Retain this writer until process exit; EOF is the parent's receipt.
        int lifetime = open(getenv("MCP_CONSOLE_TEST_WARMUP_LIFETIME"), O_WRONLY);
        if (lifetime < 0 || write(lifetime, "1", 1) != 1) _exit(125);
        int attempt = open(getenv("MCP_CONSOLE_TEST_WARMUP_ATTEMPT"), O_RDONLY);
        char token;
        if (attempt < 0 || read(attempt, &token, 1) != 1 || token != '1') _exit(125);
        close(attempt);
    }
    if (strcmp(argv[1], "serve") == 0 && strcmp(order, "written") == 0) {
        // Configuration rejection must follow the client's actual pipe write.
        // The invalid configuration exits before MCP would consume this frame.
        char byte;
        do {
            if (read(STDIN_FILENO, &byte, 1) != 1) _exit(125);
        } while (byte != '\n');
        reached();
    }
}

static void before_write(const void *buffer, size_t count) {
    const char method[] = "\"method\":\"initialize\"";
    if (await_closed_input && memmem(buffer, count, method, sizeof(method) - 1) != NULL) {
        await_closed_input = false;
        int lifetime = open(getenv("MCP_CONSOLE_TEST_WARMUP_LIFETIME"), O_RDONLY);
        char token;
        if (lifetime < 0 || read(lifetime, &token, 1) != 1 || token != '1') _exit(125);
        int attempt = open(getenv("MCP_CONSOLE_TEST_WARMUP_ATTEMPT"), O_WRONLY);
        if (attempt < 0 || write(attempt, "1", 1) != 1) _exit(125);
        close(attempt);
        // Server exit closes stdin before this write can proceed.
        if (read(lifetime, &token, 1) != 0) _exit(125);
        close(lifetime);
        reached();
    }
}

static ssize_t observed_write(int descriptor, const void *buffer, size_t count) {
    before_write(buffer, count);
#ifdef __APPLE__
    return write(descriptor, buffer, count);
#else
    ssize_t (*next_write)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
    return next_write(descriptor, buffer, count);
#endif
}

static ssize_t observed_writev(int descriptor, const struct iovec *buffers, int count) {
    for (int i = 0; i < count; ++i)
        before_write(buffers[i].iov_base, buffers[i].iov_len);
#ifdef __APPLE__
    return writev(descriptor, buffers, count);
#else
    ssize_t (*next_writev)(int, const struct iovec *, int) = dlsym(RTLD_NEXT, "writev");
    return next_writev(descriptor, buffers, count);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_writes[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)(uintptr_t)&observed_write, (const void *)(uintptr_t)&write},
    {(const void *)(uintptr_t)&observed_writev, (const void *)(uintptr_t)&writev},
};
#else
ssize_t write(int descriptor, const void *buffer, size_t count) {
    return observed_write(descriptor, buffer, count);
}
ssize_t writev(int descriptor, const struct iovec *buffers, int count) {
    return observed_writev(descriptor, buffers, count);
}
#endif
