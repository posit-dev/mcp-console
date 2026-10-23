import errno
import fcntl
import os

for direction in ("READ", "WRITE"):
    fd = int(os.environ[f"MCP_CONSOLE_TEST_SIDEBAND_{direction}"])
    try:
        fcntl.fcntl(fd, fcntl.F_GETFD)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        raise AssertionError("exec child retained sideband")
