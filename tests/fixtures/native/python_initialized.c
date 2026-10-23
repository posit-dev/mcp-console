#define _GNU_SOURCE
#include <dlfcn.h>
#include <stddef.h>

void mcp_console_probe_python_initialized(int *value) {
    int (*is_initialized)(void) = dlsym(RTLD_DEFAULT, "Py_IsInitialized");
    *value = is_initialized == NULL ? -1 : is_initialized();
}
