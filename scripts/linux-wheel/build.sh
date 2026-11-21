#!/usr/bin/env bash
# Run inside the official manylinux_2_28 image for this native target.
set -euo pipefail
target=${1:?expected a native GNU Rust target}
test "$target" = "$(uname -m)-unknown-linux-gnu"
test "$(getconf GNU_LIBC_VERSION)" = 'glibc 2.28'
export PATH="/opt/python/cp314-cp314/bin:$HOME/.cargo/bin:$PATH"

# The release checkout and its pinned companion are bind-mounted from CI.
git config --global --add safe.directory "$PWD"
git config --global --add safe.directory "$PWD/.sandbox-runner-source"
curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/rustup-init.sh
sh /tmp/rustup-init.sh -y --profile minimal --no-modify-path
python -m pip install 'maturin>=1.15,<2'

# libcap is outside the manylinux host ABI. Use AlmaLinux's signed static
# archive. Removing the development symlink makes the pinned build's -lcap
# resolve to libcap.a; libcap.so.2 remains available to system programs.
dnf -y --repofrompath='alma-devel,https://repo.almalinux.org/almalinux/8/devel/$basearch/os/' \
    --setopt=alma-devel.gpgcheck=1 \
    --setopt=alma-devel.gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux \
    install libcap-devel libcap-static openssl-devel
libdir=$(pkg-config --variable=libdir libcap)
test -f "$libdir/libcap.a"
unlink "$libdir/libcap.so"
export MCP_CONSOLE_LIBCAP_NOTICE=/usr/share/licenses/libcap/License

scripts/stage-sandbox-runner .sandbox-runner-source --target "$target"
scripts/with-checkout maturin build --release --locked --target "$target" \
    --compatibility manylinux_2_28 --out dist
