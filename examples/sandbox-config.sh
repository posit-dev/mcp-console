#!/bin/sh
set -eu

console=${1:-mcp-console}
config='{"sandbox":{"profile":"read_only","network":"restricted"},"environment":{"MESSAGE":"value: café 雪, \"quotes\", $() and spaces"}}'

# Assignment syntax supplies this child's environment without exporting config.
# Each positional parameter is one argument; only the explicit target is a shell.
set -- /bin/sh -c 'printf "%s\n" "$MESSAGE" "$1"' sh 'literal argument: * ; $(echo no)'
CONSOLE_CONFIG="$config" "$console" sandbox --config-env CONSOLE_CONFIG -- "$@"
