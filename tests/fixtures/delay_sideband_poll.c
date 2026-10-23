#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_bool claimed = false;
static atomic_int sideband_descriptor = -1;

typedef int (*poll_function)(struct pollfd *, nfds_t, int);
typedef ssize_t (*read_function)(int, void *, size_t);

static poll_function next_poll(void) {
#ifdef __APPLE__
  return poll;
#else
  return (poll_function)dlsym(RTLD_NEXT, "poll");
#endif
}

static read_function next_read(void) {
#ifdef __APPLE__
  return read;
#else
  return (read_function)dlsym(RTLD_NEXT, "read");
#endif
}

static bool target_process(void) {
  const char *value = getenv("MCP_CONSOLE_TEST_POLL_PID");
  if (value == NULL) {
    return false;
  }
  char *end = NULL;
  long process_id = strtol(value, &end, 10);
  return end != value && *end == '\0' && process_id == getpid();
}

static bool armed(void) {
  const char *path = getenv("MCP_CONSOLE_TEST_POLL_ARM");
  return path != NULL && access(path, F_OK) == 0;
}

static void mark(const char *name) {
  const char *path = getenv(name);
  if (path == NULL) {
    return;
  }
  int descriptor = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
  if (descriptor >= 0) {
    close(descriptor);
  }
}

static void notify(const char *name) {
  const char *path = getenv(name);
  if (path == NULL) {
    return;
  }
  int descriptor = open(path, O_WRONLY | O_NONBLOCK);
  if (descriptor >= 0) {
    (void)write(descriptor, "1", 1);
    close(descriptor);
  }
}

static int delayed_poll(struct pollfd *descriptors, nfds_t count, int timeout) {
  poll_function poll_next = next_poll();
  if (poll_next == NULL) {
    errno = ENOSYS;
    return -1;
  }
  int result = poll_next(descriptors, count, timeout);
  if (result <= 0 || !target_process() || count != 2 || timeout != -1 ||
      !armed() || (descriptors[0].revents & POLLIN) == 0 ||
      descriptors[0].fd != atomic_load(&sideband_descriptor) || atomic_exchange(&claimed, true)) {
    return result;
  }

  mark("MCP_CONSOLE_TEST_POLL_SIDEBAND_READY");
  if (descriptors[1].revents == 0) {
    struct pollfd cancellation = descriptors[1];
    do {
      cancellation.revents = 0;
      result = poll_next(&cancellation, 1, -1);
    } while (result < 0 && errno == EINTR);
    if (result < 0) {
      return result;
    }
    descriptors[1].revents = cancellation.revents;
  }
  notify("MCP_CONSOLE_TEST_POLL_CANCEL_READY");
  return (descriptors[0].revents != 0) + (descriptors[1].revents != 0);
}

static ssize_t observed_read(int descriptor, void *buffer, size_t length) {
  read_function read_next = next_read();
  if (read_next == NULL) {
    errno = ENOSYS;
    return -1;
  }
  ssize_t result = read_next(descriptor, buffer, length);
  const char ready[] = "{\"kind\":\"ready\"}\n";
  // Learn the sideband from its ready frame, then gate that reader's next
  // armed poll. Ordinary stdout/stderr and cancellation are also pipes.
  if (target_process() && result >= (ssize_t)(sizeof(ready) - 1) &&
      memcmp(buffer, ready, sizeof(ready) - 1) == 0) {
    atomic_store(&sideband_descriptor, descriptor);
  }
  return result;
}

__attribute__((constructor)) static void prevent_worker_injection(void) {
  if (target_process()) {
    mark("MCP_CONSOLE_TEST_POLL_LOADED");
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
  }
}

#ifdef __APPLE__
#define DYLD_INTERPOSE(replacement, replacee)                                  \
  __attribute__((used)) static struct {                                        \
    const void *replacement;                                                   \
    const void *replacee;                                                      \
  } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {    \
      (const void *)(uintptr_t)&replacement,                                   \
      (const void *)(uintptr_t)&replacee};

DYLD_INTERPOSE(delayed_poll, poll)
DYLD_INTERPOSE(observed_read, read)

#else
int poll(struct pollfd *descriptors, nfds_t count, int timeout) {
  return delayed_poll(descriptors, count, timeout);
}
ssize_t read(int descriptor, void *buffer, size_t length) {
  return observed_read(descriptor, buffer, length);
}
#endif
