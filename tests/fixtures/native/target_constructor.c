#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

__attribute__((constructor)) static void before_main(void) {
    const char *protected = getenv("MCP_CONSOLE_TEST_PROTECTED_FILE");
    if (protected == NULL) _exit(80);
    int file = open(protected, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (file >= 0) {
        close(file);
        _exit(81);
    }
    if (errno != EACCES && errno != EPERM && errno != EROFS) _exit(82);
    const char *alias = getenv("MCP_CONSOLE_TEST_DENIED_ALIAS");
    if (alias != NULL) {
        file = open(alias, O_RDONLY);
        if (file >= 0) {
            close(file);
            _exit(83);
        }
        if (errno != EACCES && errno != EPERM && errno != ENOENT) _exit(84);
    }
}

int main(void) {
    puts("target constructor observed native enforcement");
    return 0;
}
