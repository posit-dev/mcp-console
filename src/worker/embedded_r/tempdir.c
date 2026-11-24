#include <ctype.h>
#include <stdlib.h>
#include <sys/stat.h>
#ifdef _WIN32
#include <io.h>
#include <windows.h>
#else
#include <unistd.h>
#endif

static int writable_directory(const char *path) {
#ifdef _WIN32
    struct _stat64 st;
    return path && _stat64(path, &st) == 0 && (st.st_mode & _S_IFDIR)
        && _access(path, 2) == 0;
#else
    struct stat st;
    return path && stat(path, &st) == 0 && S_ISDIR(st.st_mode)
        && access(path, W_OK) == 0;
#endif
}

static int contains_whitespace(const char *path) {
    for (; *path; ++path) {
        /* R passes a plain char. On signed-char targets, treating negative
           bytes as unsigned can misclassify UTF-8 continuation bytes. */
        int byte = *path;
        if (byte >= 0 && isspace(byte)) return 1;
    }
    return 0;
}

/* R_reInitTempDir in R's src/main/sysutils.c selects the first writable
   directory before checking its spelling. */
const char *mcp_r_tempdir_variable(char *(*environment)(const char *)) {
    const char *variables[] = {"TMPDIR", "TMP", "TEMP"};
    for (size_t i = 0; i < sizeof(variables) / sizeof(variables[0]); ++i)
        if (writable_directory(environment(variables[i]))) return variables[i];
#ifdef _WIN32
    /* R_SetParams has set R_USER by this point. */
    return "R_USER";
#else
    return NULL; /* R uses /tmp. */
#endif
}

int mcp_r_tempdir_has_whitespace(char *(*environment)(const char *)) {
    const char *variable = mcp_r_tempdir_variable(environment);
    const char *path = variable ? environment(variable) : "/tmp";
    if (!path || !contains_whitespace(path)) return 0;
#ifdef _WIN32
    DWORD size = GetShortPathNameA(path, NULL, 0);
    if (!size) return 1;
    char *alias = malloc(size);
    if (!alias) return 1;
    DWORD length = GetShortPathNameA(path, alias, size);
    int invalid = !length || length >= size || contains_whitespace(alias);
    free(alias);
    return invalid;
#else
    return 1;
#endif
}
