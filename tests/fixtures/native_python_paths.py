"""Report only the interpreter path or site directory selected by a test."""

import site
import sys

if sys.argv[1] == "executable":
    print(sys.executable)
elif sys.argv[1] == "site-packages":
    print(site.getsitepackages()[0])
else:
    raise ValueError(f"unknown fixture request: {sys.argv[1]}")
