#!/bin/sh
set -eu

package=${1:?usage: test-desktop-package.sh /path/to/package.deb}
root=$(mktemp -d)
cleanup() {
    rm -rf "$root"
}
trap cleanup EXIT HUP INT TERM
dpkg-deb -x "$package" "$root"

retired="$root/usr/lib/bianbu-agent-runtime/retired-bianbu-agent.desktop"
unit="$root/usr/lib/systemd/system/bianbu-agent-runtime.service"
resources="$root/usr/share/bianbu-agent-runtime"
profile="$resources/default-profile"
export BIANBU_SYSTEM_RESOURCE_ROOT="$resources"
provisioner="$root/usr/lib/bianbu-agent-runtime/provision-user.py"

# 桌面入口、启动器与 hicolor 图标都已退役：包内只剩清理参照，控制台按端口打开。
test ! -e "$root/usr/share/applications/bianbu-agent.desktop"
test ! -e "$root/usr/share/icons/hicolor/512x512/apps/bianbu-agent.jpg"
test ! -e "$root/usr/bin/bianbu-agent-launcher"
test ! -e "$root/usr/lib/bianbu-agent-runtime/desktop-launcher.py"
test -r "$retired"
test -r "$unit"
test ! -e "$root/usr/lib/systemd/system/bianbu-agent-runtime@.service"
test ! -e "$root/usr/lib/systemd/system/bianbu-agent-runtime-control@.service"
test -x "$root/usr/lib/bianbu-agent-runtime/runtime-user-entrypoint"
test ! -e "$root/usr/lib/bianbu-agent-runtime/install-desktop-shortcut"
test ! -e "$root/etc/bianbu-agent/config.yaml"
test -r "$root/usr/share/polkit-1/rules.d/60-bianbu-agent-runtime.rules"
test ! -e "$root/usr/lib/systemd/user/bianbu-agent-runtime.service"
test -x "$provisioner"
test -x "$root/usr/lib/bianbu-agent-runtime/register-security-manifest.py"
test -x "$root/usr/lib/bianbu-agent-runtime/reset-security-binding.py"
test -r "$profile/config.yaml"
test -r "$root/usr/share/bianbu-agent-runtime/security-manifest/com.bianbu.runtime/manifest.toml"
test -r "$root/usr/share/bianbu-agent-runtime/security-manifest/com.bianbu.runtime/manifest.toml.sig"
test -r "$root/usr/share/bianbu-agent-runtime/security-manifest/com.bianbu.runtime/pubkey.hex"
test -r "$profile/starter-agent.yaml"
test -r "$resources/models/custom-bibit/provider.yaml"
test ! -e "$profile/tools/read_file/tool.py"
test ! -e "$profile/hooks"
# Plugin Console 机制已移除：系统插件不再携带 console.py。
test ! -e "$resources/plugins/bianbu.memory/console.py"
test ! -e "$resources/plugins/bianbu.bibit.intent/console.py"
test -r "$resources/plugins/bianbu.memory/plugin.yaml"
test -d "$resources/plugins/bianbu.bibit.intent"
test -r "$resources/plugins/bianbu.bibit.intent/plugin.yaml"
test ! -e "$resources/plugins/bianbu.bibit.intent/tools/intent_classify"
test -d "$resources/mcps"

# 清理参照必须还是当年那份入口（退役前 /usr/share/applications 的逐字节副本）。
grep -qx 'Exec=/usr/bin/bianbu-agent-launcher' "$retired"
# 系统级服务：整机一份，随 multi-user.target 开机启动，运行身份由 postinst 的
# drop-in 覆盖，包内默认值是专用账号。
grep -qx 'User=bianbu-runtime' "$unit"
grep -qx 'WantedBy=multi-user.target' "$unit"
! grep -q '%i' "$unit"
grep -qx 'ExecStart=/usr/lib/bianbu-agent-runtime/runtime-user-entrypoint serve' "$unit"
grep -qx 'Environment=BIANBU_RUNTIME_SYSTEM_UNIT=bianbu-agent-runtime.service' "$unit"
grep -qx 'Environment=BIANBU_RUNTIME_SERVICE_USER=bianbu-runtime' "$unit"

test_home=$root/test-home
# The relocatable venv points at /usr/bin/python3.14 in its installed layout.
ln -s /usr/bin/python3.14 "$root/usr/bin/python3.14"
python="$root/usr/lib/bianbu-agent-runtime/venv/bin/python"
HOME="$test_home" "$python" "$provisioner" \
    --profile "$profile" --home "$test_home" --uid 1000 >"$test_home/provision.json"
grep -q '"updated": true' "$test_home/provision.json"

config=$test_home/.bianbu-agent/config.yaml
starter_agent_id=$("$python" -c '
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["starter_agent_id"])
' "$test_home/.bianbu-agent/.managed-profile.json")
case "$starter_agent_id" in
    com.bianbu.[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) exit 1 ;;
esac
starter_agent="$test_home/.bianbu-agent/agents/$starter_agent_id/agent.yaml"
test -r "$config"
test ! -e "$starter_agent"
starter_agent="$profile/starter-agent.yaml"
test ! -e "$test_home/.bianbu-agent/models/custom-bibit/provider.yaml"
test ! -e "$test_home/.bianbu-agent/tools"
"$python" -c "from bianbu_agent.tools.builtins import builtin_source; assert builtin_source('read_file').is_file()"
test ! -e "$test_home/.bianbu-agent/plugins/bianbu.memory/plugin.yaml"
test ! -e "$test_home/.bianbu-agent/plugins/bianbu.bibit.intent/tools/intent_classify"
"$python" -c '
import sys, yaml
agent = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["agent"]
assert agent["model"] == {"provider": "custom:bibit", "name": "deepseek-flash"}
assert "*" not in agent["plugins"]
assert agent["plugins"]
' "$starter_agent"
grep -q '^provider: custom:bibit$' "$resources/models/custom-bibit/provider.yaml"
grep -q '^  api_key: sk-' "$resources/models/custom-bibit/provider.yaml"
test "$(stat -c '%a' "$resources/models/custom-bibit/provider.yaml")" = 644
