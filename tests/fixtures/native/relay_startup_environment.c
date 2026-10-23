#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

__attribute__((constructor)) static void initialize(void) {
    // Observe only the relay, not its worker or the worker's subprocesses.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static int observe_thread(pthread_t *thread, const pthread_attr_t *attributes,
                          void *(*start)(void *), void *argument) {
    // Read the native environment before the relay becomes multithreaded.
    int descriptor = open(getenv("MCP_CONSOLE_TEST_STARTUP_ENVIRONMENT"),
                          O_WRONLY | O_CREAT | O_APPEND, 0600);
    char present = getenv("MCP_CONSOLE_STARTUP_FILE") == NULL ? '0' : '1';
    if (descriptor < 0 || write(descriptor, &present, 1) != 1) _exit(90);
    close(descriptor);
#ifdef __APPLE__
    return pthread_create(thread, attributes, start, argument);
#else
    return ((int (*)(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *))
              dlsym(RTLD_NEXT, "pthread_create"))(thread, attributes, start, argument);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct { const void *replacement; const void *original; }
interpose_thread __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observe_thread, (const void *)(uintptr_t)&pthread_create
};
#else
int pthread_create(pthread_t *thread, const pthread_attr_t *attributes,
                   void *(*start)(void *), void *argument) {
    return observe_thread(thread, attributes, start, argument);
}
#endif
