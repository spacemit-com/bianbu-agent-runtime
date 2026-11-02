#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
build_venv=${TEST_BUILD_VENV:-$repo_root/debian/.bianbu-agent-build/venv}
build_python=$build_venv/bin/python
if [ ! -x "$build_python" ]; then
    build_python=$repo_root/.venv/bin/python
fi
work=$(mktemp -d)
cleanup() {
    rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

mkdir -p \
    "$work/debian/.bianbu-agent-build/venv/bin" \
    "$work/resources/models/custom-bibit" \
    "$work/resources" \
    "$work/resources/skills/example" \
    "$work/.bianbu-agent/hooks/example" \
    "$work/resources/plugins/vendor.example" \
    "$work/scripts" \
    "$work/bianbu_agent/console/web/static"

for path in \
    bianbu-agent-wrapper \
    provision-user.py \
    cleanup-retired-desktop-icon \
    retired-bianbu-agent.desktop \
    runtime-user-entrypoint \
    prepare-runtime-device \
    render-starter-agent.py \
    configure-runtime-service-user \
    runtime-admin-token.py \
    bianbu-agent-runtime-token \
    bianbu-agent-runtime.service \
    bianbu-agent-runtime-register.service \
    bianbu-agent-runtime-keepalive.service \
    bianbu-agent-runtime-keepalive.timer \
    bianbu-agent-runtime.polkit \
    bianbu-agent-runtime.tmpfiles \
    bianbu-agent-runtime-wrapper; do
    : >"$work/debian/$path"
done

cp "$repo_root/debian/render-starter-agent.py" "$work/debian/render-starter-agent.py"
cp "$repo_root/scripts/provision-system-agent.py" "$work/scripts/provision-system-agent.py"
mkdir -p "$work/resources/agents/com.bianbu.627d97ad908c"
cp "$repo_root/resources/agents/com.bianbu.627d97ad908c/agent.yaml" \
    "$work/resources/agents/com.bianbu.627d97ad908c/agent.yaml"
cp "$repo_root/resources/config.yaml" \
    "$work/resources/config.yaml"
printf '%s\n' '#!/bin/sh' "exec \"$build_python\" \"\$@\"" \
    >"$work/debian/.bianbu-agent-build/venv/bin/python"
chmod 0755 "$work/debian/.bianbu-agent-build/venv/bin/python"

: >"$work/resources/skills/example/SKILL.md"
: >"$work/.bianbu-agent/hooks/example/hook.py"
cat >"$work/resources/plugins/vendor.example/plugin.yaml" <<'EOF'
schema_version: 1
name: vendor.example
version: 1.0.0
EOF
: >"$work/resources/models/custom-bibit/provider.yaml"
: >"$work/bianbu_agent/console/web/static/logo.jpg"

# A clean checkout may have no standalone MCP definitions at all.
test ! -e "$work/resources/mcps"

make -C "$work" -f "$repo_root/debian/rules" \
    override_dh_auto_install

resources="$work/debian/bianbu-agent-runtime/usr/share/bianbu-agent-runtime"
test -x "$work/debian/bianbu-agent-runtime/usr/lib/bianbu-agent-runtime/provision-system-agent.py"
test ! -e "$resources/default-profile"
test -r "$resources/skills/example/SKILL.md"
test -r "$resources/plugins/vendor.example/plugin.yaml"
test -r "$resources/models/custom-bibit/provider.yaml"
test -d "$resources/mcps"
test ! -e "$work/debian/bianbu-agent-runtime/usr/share/bianbu-agent-runtime/security-manifest"
"$build_python" - "$repo_root/resources/agents/com.bianbu.627d97ad908c/agent.yaml" \
    "$resources/agents/com.bianbu.627d97ad908c/agent.yaml" <<'PY'
import sys
import yaml

source = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
packaged = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))
source["agent"]["model"] = {"provider": "custom:bibit", "name": "deepseek-flash"}
assert packaged == source
assert "*" not in packaged["agent"]["plugins"]
assert packaged["agent"]["plugins"]
# 插件名下的 MCP / 技能 / 工具不单独点名：选中插件就等于选中它们。
plugins = set(packaged["agent"]["plugins"])
for selector in (
    packaged["agent"].get("mcps", [])
    + packaged["agent"].get("skills", [])
    + packaged["agent"].get("tools", [])
):
    owner = selector.split("/", 1)[0] if "/" in selector else selector
    assert owner not in plugins, f"{selector} belongs to the selected Plugin {owner}"
PY
