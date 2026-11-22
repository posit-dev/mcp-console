#!/bin/sh
# Exercise the archive exactly as a native client would in an EL8 userspace.
set -eu
test "$(getconf GNU_LIBC_VERSION)" = 'glibc 2.28'
test -z "$(rpm -qa '*python*')"
for name in uv pip bwrap cc make cargo; do
    if command -v "$name"; then exit 1; fi
done
root=$(mktemp -d)
mkdir "$root/extract"
tar -xzf "$1" -C "$root/extract"
mv "$root/extract" "$root/relocated bundle"
console="$root/relocated bundle/bin/mcp-console"
mkdir "$root/workspace"
cd "$root/workspace"
export PATH="$root/workspace"
"$console" --version
"$console" --help
"$console" sandbox -- /usr/bin/true
