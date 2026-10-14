#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
postinst=$repo_root/debian/postinst
postrm=$repo_root/debian/postrm
system_paths=$repo_root/bianbu_agent/agents/system_paths.py
profile_root=/var/lib/bianbu-agent-profiles
retired_root=/var/lib/bianbu-agent-runtime/profiles

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

# The declaration published here is read by the Runtime user and by sandboxed
# Workers, so the root must not sit below the 0750 Runtime data directory.  A
# 0750 ancestor hides the tree from every reader, whatever the declaration's
# own mode is.
grep -qx "SYSTEM_PROFILE_ROOT = Path(\"$profile_root\")" "$system_paths" \
    || fail "the Runtime must publish its system Agent declaration in $profile_root"
if grep -q "Path(\"$retired_root\")" "$system_paths"; then
    fail "$profile_root must not be nested under $retired_root"
fi
grep -qx "    install -d -m 0755 -o root -g root $profile_root" "$postinst" \
    || fail "postinst must create $profile_root as root:root 0755"
grep -qx "    rm -rf -- $retired_root" "$postinst" \
    || fail "postinst must drop the retired publication tree"
grep -q "$profile_root" "$postrm" \
    || fail "postrm must remove $profile_root"

printf '%s\n' 'System profile directory test passed.'
