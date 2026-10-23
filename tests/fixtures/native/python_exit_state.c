#include <dlfcn.h>
#include <unistd.h>

// Exercise extension-library teardown at actual worker process exit.
__attribute__((destructor)) static void observe_python_thread(void) {
    int (*gil_held)(void) = dlsym(RTLD_DEFAULT, "PyGILState_Check");
    if (gil_held != NULL && gil_held()) {
        const char message[] = "Python exit thread attached\n";
        (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    } else {
        const char message[] = "Python exit thread detached\n";
        (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    }
}
