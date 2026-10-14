#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
work=$(mktemp -d)
cleanup() {
    rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$work/updates"
cat >"$work/status" <<'EOF'
Package: curl
Status: install ok installed
Architecture: riscv64
Version: 8.14.1-2ubuntu1

Package: gcc
Status: install ok installed
Architecture: riscv64
Version: 4:15.2.0-4ubuntu1

Package: debhelper
Status: install ok installed
Architecture: riscv64
Version: 13.20
Provides: debhelper-compat (= 13)

Package: python3.14
Status: install ok installed
Architecture: riscv64
Version: 3.14.0-1

Package: python3-pip
Status: install ok installed
Architecture: all
Version: 25.1+dfsg-1

Package: libfst-dev
Status: install ok installed
Architecture: riscv64
Version: 1.8.4-3build1

EOF

cd "$repo_root"
dpkg-checkbuilddeps --admindir="$work" -a riscv64 -I
