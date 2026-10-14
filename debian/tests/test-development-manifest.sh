#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
build_venv=${TEST_BUILD_VENV:-$repo_root/.venv}
work=$(mktemp -d)
cleanup() {
    rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

make -C "$repo_root" -f debian/rules prepare-security-manifest \
    BUILD_ROOT="$work/build" \
    BUILD_VENV="$build_venv"

generated=$work/build/security-manifest
package=$generated/com.bianbu.runtime
test -s "$package/manifest.toml"
test -s "$package/manifest.toml.sig"
test -s "$package/pubkey.hex"
test -s "$generated/DEVELOPMENT-SELF-SIGNED"
test ! -e "$work/build/development-signing-key.pem"
test ! -e "$work/build/development-signing-key.hex"

"$build_venv/bin/python" - "$package" <<'PY'
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

package = Path(sys.argv[1])
body = (package / "manifest.toml").read_text(encoding="utf-8")
assert '[permissions.network]' in body
assert 'mode = "allow_all"' in body
assert 'allowed_domains = ["api.deepseek.com", "api.rvcompute.com", "localhost"]' in body
payload = "\n".join(
    line for line in body.splitlines()
    if not line.strip().startswith("signature")
).encode("utf-8")
signature = bytes.fromhex(
    (package / "manifest.toml.sig").read_text(encoding="ascii").strip()
)
public_key = bytes.fromhex(
    (package / "pubkey.hex").read_text(encoding="ascii").strip()
)
Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
PY

production=$work/production-manifest
mkdir -p "$production"
cp -a "$package" "$production/com.bianbu.runtime"
before=$(sha256sum "$production"/com.bianbu.runtime/*)

make -C "$repo_root" -f debian/rules prepare-security-manifest \
    BUILD_ROOT="$work/production-build" \
    BUILD_VENV="$build_venv" \
    SECURITY_MANIFEST_DIR="$production"

after=$(sha256sum "$production"/com.bianbu.runtime/*)
test "$before" = "$after"
test ! -e "$work/production-build/security-manifest"

cp -a "$production" "$work/tampered-production"
printf '%s\n' "$(printf '00%.0s' $(seq 1 64))" \
    >"$work/tampered-production/com.bianbu.runtime/manifest.toml.sig"
if make -C "$repo_root" -f debian/rules prepare-security-manifest \
    BUILD_ROOT="$work/tampered-build" \
    BUILD_VENV="$build_venv" \
    SECURITY_MANIFEST_DIR="$work/tampered-production"; then
    printf '%s\n' 'Tampered production manifest was accepted' >&2
    exit 1
fi

printf '%s\n' 'Development and production manifest build paths are valid.'
