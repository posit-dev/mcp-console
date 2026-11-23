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
        if (isspace((unsigned char)*path)) return 1;
    }
    return 0;
}

/* R_reInitTempDir in R's src/main/sysutils.c selects the first writable
   directory before checking its spelling. R calls R_Suicide on whitespace. */
int mcp_r_tempdir_has_whitespace(char *(*environment)(const char *)) {
    const char *variables[] = {"TMPDIR", "TMP", "TEMP"};
#ifdef _WIN32
    /* R_SetParams has set R_USER by this point. */
    const char *path = environment("R_USER");
#else
    const char *path = "/tmp";
#endif
    for (size_t i = 0; i < sizeof(variables) / sizeof(variables[0]); ++i) {
        const char *candidate = environment(variables[i]);
        if (writable_directory(candidate)) {
            path = candidate;
            break;
        }
    }
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
