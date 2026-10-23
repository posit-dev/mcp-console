#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <sys/socket.h>

typedef int (*connect_function)(int, const struct sockaddr *, socklen_t);

static connect_function next_connect(void) {
#ifdef __APPLE__
    return connect;
#else
    return (connect_function)dlsym(RTLD_NEXT, "connect");
#endif
}

static int deny_worker_connect(int socket, const struct sockaddr *address, socklen_t length) {
    if (getenv("MCP_CONSOLE_LOCAL_RUNTIME") && getenv("MCP_CONSOLE_TEST_DENY_WORKER_NETWORK")) {
        errno = EACCES;
        return -1;
    }
    return next_connect()(socket, address, length);
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} connect_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)deny_worker_connect, (const void *)connect
};
#else
int connect(int socket, const struct sockaddr *address, socklen_t length) {
    return deny_worker_connect(socket, address, length);
}
#endif
