#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repo_root"

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

[ "$(dpkg-parsechangelog -S Source)" = "bianbu-agent-runtime" ] || fail "unexpected source package name"
debian_version=$(dpkg-parsechangelog -S Version)
upstream_version=${debian_version%%+*}
project_version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
[ "$upstream_version" = "$project_version" ] || fail "Debian upstream version must match pyproject.toml"

binary_arch=$(awk '
    /^Package: bianbu-agent-runtime$/ { in_binary = 1; next }
    in_binary && /^Architecture:/ { print $2; exit }
' debian/control)
[ "$binary_arch" = "any" ] || fail "binary package must be architecture-specific"

if grep -Eq 'Section: unknown|Maintainer:.*<>|<insert|auto-generated package' debian/control; then
    fail "debian/control still contains generated placeholders"
fi

for path in \
    debian/bianbu-agent-wrapper \
    debian/bianbu-agent-runtime-wrapper \
    debian/retired-bianbu-agent.desktop \
    debian/bianbu-agent-runtime.service \
    debian/bianbu-agent-runtime.polkit \
    debian/bianbu-agent-runtime.tmpfiles \
    debian/runtime-user-entrypoint \
    debian/provision-user.py \
    debian/generate-security-manifest.py \
    debian/register-security-manifest.py \
    debian/reset-security-binding.py \
    debian/runtime-admin-token.py \
    debian/bianbu-agent-runtime-token \
    debian/register-runtime-manifest \
    debian/bianbu-agent-runtime-register.service \
    scripts/network-learning-collector.py \
    debian/preinst \
    debian/render-starter-agent.py \
    debian/postinst \
    debian/prerm \
    debian/postrm \
    resources/default-profile/config.yaml \
    resources/default-profile/starter-agent.yaml \
    resources/models/custom-bibit/provider.yaml \
    debian/requirements-extra.txt \
    debian/tests/test-build-dependencies.sh \
    debian/tests/test-development-manifest.sh \
    debian/tests/test-offline-install.sh \
    debian/tests/test-desktop-icon-cleanup.sh \
    debian/tests/test-postrm-cleanup.sh \
    debian/tests/test-starter-profile.sh \
    debian/tests/test-system-profile-dir.sh \
    debian/tests/package-smoke; do
    [ -f "$path" ] || fail "missing $path"
done

# 桌面入口、启动器与桌面安装助手都已退役：本包不再提供任何应用菜单项，控制台按
# 端口打开。retired-bianbu-agent.desktop 留作清理参照，不属于菜单项。
for retired in \
    debian/bianbu-agent-launcher \
    debian/desktop-launcher.py \
    debian/bianbu-agent.desktop; do
    [ ! -e "$retired" ] || fail "retired desktop launcher $retired must be gone"
done

[ -x debian/bianbu-agent-wrapper ] || fail "wrapper is not executable"
[ -x debian/bianbu-agent-runtime-wrapper ] || fail "Runtime wrapper is not executable"
[ -x debian/runtime-user-entrypoint ] || fail "Runtime user entrypoint is not executable"
[ -x debian/provision-user.py ] || fail "user profile provisioner is not executable"
[ -x debian/generate-security-manifest.py ] || fail "Security manifest generator is not executable"
[ -x debian/register-security-manifest.py ] || fail "Security manifest registrar is not executable"
[ -x debian/reset-security-binding.py ] || fail "Security binding reset helper is not executable"
[ -x debian/runtime-admin-token.py ] || fail "Runtime admin token helper is not executable"
[ -x debian/bianbu-agent-runtime-token ] || fail "Runtime admin token command is not executable"
[ -x debian/register-runtime-manifest ] || fail "device registration script is not executable"
[ -x scripts/network-learning-collector.py ] || fail "network learning collector is not executable"
[ -x debian/preinst ] || fail "package preinst is not executable"
[ -x debian/postrm ] || fail "package postrm is not executable"
[ -s resources/models/custom-bibit/provider.yaml ] || fail "Bibit built-in provider default is missing"
[ -x debian/tests/test-build-dependencies.sh ] || fail "build dependency test is not executable"
[ -x debian/tests/test-development-manifest.sh ] || fail "development manifest test is not executable"
[ -x debian/tests/test-offline-install.sh ] || fail "offline install test is not executable"
[ -x debian/tests/test-desktop-icon-cleanup.sh ] || fail "desktop icon cleanup test is not executable"
[ -x debian/tests/test-postrm-cleanup.sh ] || fail "postrm cleanup test is not executable"
[ -x debian/tests/test-starter-profile.sh ] || fail "starter profile test is not executable"
[ -x debian/tests/test-system-profile-dir.sh ] || fail "system profile directory test is not executable"
[ -x debian/tests/package-smoke ] || fail "package smoke test is not executable"

# Removing a test from debian/tests without removing its invocation from
# debian/rules only shows up when dh_auto_test actually runs, which
# DEB_BUILD_OPTIONS=nocheck hides.  Fail here instead.
for referenced in $(sed -n 's|.*\b\(debian/tests/[A-Za-z0-9_-]*\.sh\)\b.*|\1|p' debian/rules | sort -u); do
    [ -f "$referenced" ] || fail "debian/rules runs a missing test: $referenced"
    [ -x "$referenced" ] || fail "debian/rules runs a non-executable test: $referenced"
done
grep -q 'debian/tests/test-offline-install.sh' debian/rules \
    || fail "the offline install test is not part of the package build"

sh -n debian/bianbu-agent-wrapper
sh -n debian/bianbu-agent-runtime-wrapper
sh -n debian/runtime-user-entrypoint
sh -n debian/bianbu-agent-runtime-token
sh -n debian/register-runtime-manifest
sh -n debian/postinst
sh -n debian/preinst
sh -n debian/prerm
sh -n debian/postrm
sh -n debian/tests/test-build-dependencies.sh
sh -n debian/tests/test-development-manifest.sh
sh -n debian/tests/test-offline-install.sh
sh -n debian/tests/test-desktop-icon-cleanup.sh
sh -n debian/tests/test-postrm-cleanup.sh
sh -n debian/tests/test-starter-profile.sh
sh -n debian/tests/test-system-profile-dir.sh
sh -n debian/tests/package-smoke
python3 -c 'compile(open("debian/provision-user.py", encoding="utf-8").read(), "debian/provision-user.py", "exec")'
python3 -c 'compile(open("debian/generate-security-manifest.py", encoding="utf-8").read(), "debian/generate-security-manifest.py", "exec")'
python3 -c 'compile(open("debian/register-security-manifest.py", encoding="utf-8").read(), "debian/register-security-manifest.py", "exec")'
python3 -c 'compile(open("debian/reset-security-binding.py", encoding="utf-8").read(), "debian/reset-security-binding.py", "exec")'
python3 -c 'compile(open("debian/runtime-admin-token.py", encoding="utf-8").read(), "debian/runtime-admin-token.py", "exec")'
python3 -c 'compile(open("debian/render-starter-agent.py", encoding="utf-8").read(), "debian/render-starter-agent.py", "exec")'
python3 -c 'compile(open("scripts/network-learning-collector.py", encoding="utf-8").read(), "scripts/network-learning-collector.py", "exec")'
[ ! -e debian/default-agent.yaml ] || fail "duplicated Debian Agent source must be removed"
grep -q '^ python3\.14,$' debian/control || fail "Python 3.14 dependency is not declared"
grep -q '^ python3-pip,$' debian/control || fail "pip build dependency is not declared"
grep -q '^ curl,$' debian/control || fail "curl build dependency is not declared"
grep -q "'uv>=0.11,<0.12'" debian/rules || fail "uv build bootstrap is not version bounded"
grep -q '^ systemd,' debian/control || fail "systemd runtime dependency is not declared"
grep -q '^ polkitd,' debian/control || fail "polkitd runtime dependency is not declared"
grep -q '^ llama\.cpp-tools-spacemit,$' debian/control \
    || fail "SpacemiT llama.cpp tools runtime dependency is not declared"
grep -Eq '^ bianbu-agent-memory( \(>= [^)]+\))?,$' debian/control \
    || fail "Memory runtime dependency is not declared"
grep -Eq '^ bianbu-agent-securityd( \(>= [^)]+\))?,$' debian/control \
    || fail "Security runtime dependency is not declared"
grep -Eq '^ bibit-intent( \(>= [^)]+\))?,$' debian/control \
    || fail "Bibit Intent runtime dependency is not declared"
# The local model page reads the device models from spacemit-ai-gateway (through
# bibit-intent first and the Gateway second), so the Runtime must depend on the
# package that owns and loads those models instead of assuming it is present.
grep -Eq '^ spacemit-ai-gateway( \(>= [^)]+\))?,$' debian/control \
    || fail "spacemit-ai-gateway runtime dependency is not declared"
# The browser CLI, its wrapper and Chromium live in the agent-browser package
# (gitlab.dc.com/bianbu/agent-browser).  Keep it that way: re-adding a Rust
# toolchain or a second /usr/bin/agent-browser here would break the file
# handover that agent-browser expresses with Breaks/Replaces.
grep -Eq '^ agent-browser( \(>= [^)]+\))?,$' debian/control \
    || fail "agent-browser runtime dependency is not declared"
! grep -q '^ cargo,$' debian/control \
    || fail "the Runtime must not carry a Rust toolchain build dependency"
! grep -q '^ rustc,$' debian/control \
    || fail "the Runtime must not carry a Rust toolchain build dependency"
! grep -q '^ chromium,$' debian/control \
    || fail "Chromium belongs to the agent-browser package, not the Runtime"
[ ! -e debian/agent-browser ] \
    || fail "the in-tree agent-browser copy must not come back"
! grep -q 'agent-browser-wrapper' debian/rules \
    || fail "debian/rules still installs an agent-browser wrapper"
grep -q 'RUNTIME_ALLOWED_DOMAINS ?= api.deepseek.com api.rvcompute.com localhost' debian/rules \
    || fail "packaged Runtime manifest does not allow cloud providers and local Bibit"
runtime_manifest_version=$(sed -n 's/^RUNTIME_MANIFEST_VERSION ?= //p' debian/rules)
test -n "$runtime_manifest_version" && dpkg --compare-versions "$runtime_manifest_version" ge 1.2.23 \
    || fail "signed Runtime manifest generation was not advanced"
grep -q '^    handshake_timeout_seconds: 60$' resources/default-profile/config.yaml \
    || fail "packaged K3 Worker activation timeout is too short"
! grep -q '^ bianbu-agent-memoryd,$' debian/control \
    || fail "Runtime must consume memoryd through the public Memory package dependency"
! grep -q '^ bianbu-agent-memory-sdk,$' debian/control \
    || fail "obsolete Memory SDK Debian package dependency remains"
systemd_install=$(env -u MAKEFLAGS -u MFLAGS -u MAKEOVERRIDES -u MAKELEVEL \
    make -n -f debian/rules override_dh_installsystemd) \
    || fail "systemd service install rule is missing"
printf '%s\n' "$systemd_install" | grep -q 'dh_installsystemd --name=bianbu-agent-runtime' \
    || fail "systemd service must be installed during package installation"
test -r 'debian/bianbu-agent-runtime.service' \
    || fail "system-level Runtime service unit is missing"
! grep -q '%i' 'debian/bianbu-agent-runtime.service' \
    || fail "the system service must not be a per-user template instance"
grep -q '^User=bianbu-runtime$' 'debian/bianbu-agent-runtime.service' \
    || fail "the system service must default to the dedicated Runtime account"
grep -q '^SupplementaryGroups=bianbu-security bianbu-agent bianbu-admin bianbu$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service must receive shared Runtime groups"
grep -q '^Environment=BIANBU_RUNTIME_SYSTEM_UNIT=bianbu-agent-runtime.service$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service must publish its unit name"
grep -q '^Environment=BIANBU_RUNTIME_SERVICE_USER=bianbu-runtime$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service must publish the account it runs as"
grep -q '^Environment=BIANBU_AGENT_MEMORY_ACCOUNT_ID=bianbu-runtime$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service must isolate memory accounts"
grep -q '^Environment=BIANBU_WORKER_MEMORY_MAX=off$' 'debian/bianbu-agent-runtime.service' \
    || fail "packaged unit must not cap per-Worker memory by default"
grep -q '^Environment=BIANBU_WORKER_CPU_QUOTA=off$' 'debian/bianbu-agent-runtime.service' \
    || fail "packaged unit must not cap per-Worker CPU by default"
! grep -q '^MemoryMax=' 'debian/bianbu-agent-runtime.service' \
    || fail "packaged unit must not cap its own control-plane cgroup"
grep -q '^NoNewPrivileges=false$' 'debian/bianbu-agent-runtime.service' \
    || fail "host Workers must be able to enter sudo authentication"
grep -q '^RestrictSUIDSGID=false$' 'debian/bianbu-agent-runtime.service' \
    || fail "host Workers must retain normal setuid sudo semantics"
# 会话 Agent 会自己探测内核状态来决定"我在不在沙箱"。私有挂载命名空间、
# seccomp 过滤器、只读 /usr 都会被它读成沙箱证据，进而拒绝本可以用 sudo 完成
# 的任务（K3 现场排障结论）。host
# Worker 的探针输出必须与普通登录会话一致；bwrap 后端自带 tmpfs、mount
# namespace 和 seccomp profile，不需要父 unit 提供任何一项。
! grep -q '^PrivateTmp=true$' 'debian/bianbu-agent-runtime.service' \
    || fail "the packaged unit must not give host Workers a private mount namespace"
! grep -q '^ProtectSystem=' 'debian/bianbu-agent-runtime.service' \
    || fail "the packaged unit must not remount /usr and /etc read-only for host Workers"
! grep -q '^LockPersonality=true$' 'debian/bianbu-agent-runtime.service' \
    || fail "the packaged unit must not load a seccomp filter for host Workers"
# 内核只允许 no_new_privs（不可撤销，会让 sudo/pkexec/su 永久失效）或
# CAP_SYS_ADMIN 的进程装载 seccomp 过滤器，所以「host Worker 能 sudo」与
# 「host Worker 自装载过滤器」互斥。包内必须保持只有沙箱 Worker 装载审计
# 过滤器，且 host 后端不下发过滤器。
grep -q 'if audit_filter and self.sandboxed:' bianbu_agent/security/runtime_process/worker.py \
    || fail "only sandboxed Workers may load the seccomp audit filter"
! grep -q 'clean_env\["BIANBU_SECCOMP_AUDIT_FILTER"\]' bianbu_agent/security/execution_runtime.py \
    || fail "host Workers must not receive the seccomp audit filter"
grep -q '^a+ /run/bianbu - - - - u:bianbu:rwx,u:bianbu-memory:rwx,g:bianbu-security:rwx,m::rwx$' debian/bianbu-agent-runtime.tmpfiles \
    || fail "shared Runtime root ACL must let securityd and memoryd create their sockets"
! grep -q '^d /run/bianbu/agent-runtime' debian/bianbu-agent-runtime.tmpfiles \
    || fail "tmpfiles must not recreate nested Runtime paths across an ownership boundary"
grep -q 'systemd-tmpfiles --create /usr/lib/tmpfiles.d/bianbu-agent-runtime.conf' debian/register-runtime-manifest \
    || fail "device registration must materialize the shared Runtime registry"
grep -q 'install -d -m 2770 -o root -g bianbu-agent /run/bianbu/agent-runtime/memory-registry' debian/register-runtime-manifest \
    || fail "device registration must enforce memoryd-trusted registry ownership"
! grep -q 'Runtime unit is enabled for the device user' debian/postinst \
    || fail "postinst comment drift"
grep -q 'bianbu-agent-runtime.service.d/10-service-user.conf' debian/postinst \
    || fail "postinst must publish the resolved Runtime run identity as a drop-in"
! grep -q 'systemctl enable bianbu-agent-runtime@' debian/postinst \
    || fail "postinst must enable the shared system unit, not a per-user instance"
! grep -q 'Desktop/Bianbu Agent.desktop' debian/postinst \
    || fail "postinst must not copy an icon onto the user desktop"
! grep -q 'install-desktop-shortcut' debian/postinst debian/rules \
    || fail "the retired desktop shortcut installer must be gone"
! grep -q 'usr/share/applications' debian/rules \
    || fail "the package must not install an application-menu entry"
! grep -q 'bianbu-agent-launcher\|desktop-launcher' debian/rules debian/postinst debian/prerm \
    || fail "the retired desktop launcher must be gone from the maintainer scripts"
! grep -q 'xdg-utils\|xdg-user-dirs' debian/control \
    || fail "the retired desktop launcher dependencies must be gone"

# The package transaction must survive an image build: no manager, no D-Bus and
# no securityd.  Everything that needs them happens on the device instead.
! grep -qE 'reset-security-binding|register-security-manifest|systemd-tmpfiles|/run/bianbu/' debian/postinst \
    || fail "package install must not depend on the live device"
! grep -q 'BIANBU_RUNTIME_SKIP_SYSTEM_INTEGRATION' debian/postinst \
    || fail "package install must not need an opt-in switch to work off-device"
! grep -qi 'bibit' debian/postinst \
    || fail "the Runtime package must not manage the Bibit model store at all"

# 维护脚本里「函数删了、调用还在」只有到 dpkg configure 才炸（0.1.64 的
# ensure_bibit_runtime_permissions 就是这样，包停在 half configured）。这里静态检查：
# 维护脚本里出现的命令词必须是自己定义的函数、shell 关键字或已知外部命令。
python3 - debian/postinst debian/prerm debian/postrm debian/preinst \
    debian/register-runtime-manifest debian/cleanup-retired-desktop-icon \
    debian/configure-runtime-service-user \
    <<'GUARD' \
    || fail "maintainer script calls an undefined helper or command"
import re
import sys
from pathlib import Path

KEYWORDS = {
    ".", ":", "[", "break", "case", "cd", "command", "continue", "do", "done",
    "elif", "else", "esac", "eval", "exec", "exit", "export", "fi", "for",
    "function", "if", "in", "local", "read", "return", "set", "shift", "test",
    "then", "time", "trap", "type", "umask", "unset", "until", "while",
}
EXTERNALS = {
    "addgroup", "adduser", "awk", "chmod", "cmp", "cut", "echo", "getent",
    "grep", "id", "install", "mv", "printf", "rm", "rmdir", "runuser", "sed",
    "sha256sum", "sleep", "sort", "systemctl", "tr", "usermod",
}
ENDING_KEYWORDS = {"fi", "esac", "done", "else", "then", "do", ";;", "break", "continue"}

problems = []
for argument in sys.argv[1:]:
    script = Path(argument)
    if not script.is_file():
        continue
    source = script.read_text()
    defined = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)", source, re.M))
    heredoc = None
    for number, line in enumerate(source.splitlines(), start=1):
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        marker = re.match(r".*<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*$", line)
        if marker:
            heredoc = marker.group(1)
            continue
        if re.match(r"^\s*#", line) or re.match(r"^\s*[){}]|^\s*;;|^\s*\*\)", line):
            continue
        if line.strip() in ENDING_KEYWORDS:
            continue
        command = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(\s|$)", line)
        if command is None:
            continue
        name = command.group(1)
        if name in KEYWORDS or name in EXTERNALS or name in defined:
            continue
        problems.append(f"{script}:{number}: {name}")
if problems:
    print("\n".join(problems), file=sys.stderr)
    sys.exit(1)
GUARD
grep -q '^ExecStartPre=+/usr/bin/install -d -m 2770 -o root -g bianbu /run/bianbu/agent-runtime$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service template must repair the shared Runtime directory at start"
grep -q '^ExecStartPre=+/usr/bin/install -d -m 2770 -o root -g bianbu-agent /run/bianbu/agent-runtime/memory-registry$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service template must repair the Memory registry at start"
! grep -q '/opt/bibit-intent' 'debian/bianbu-agent-runtime.service' \
    || fail "the Runtime unit must not create, chown or write the Bibit stores"
grep -q '^ExecStartPre=/usr/lib/bianbu-agent-runtime/runtime-user-entrypoint provision$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service template must provision the login home"
grep -q '^ExecStart=/usr/lib/bianbu-agent-runtime/runtime-user-entrypoint serve$' 'debian/bianbu-agent-runtime.service' \
    || fail "system service template must serve the login home"
test -x debian/runtime-user-entrypoint \
    || fail "per-user Runtime entrypoint must be executable"
! grep -q '^/etc/bianbu-agent/config.yaml$' debian/preinst \
    || fail "dpkg conffile must not be moved during upgrade"
grep -q 'HOME}/.bianbu-agent/config.yaml' debian/bianbu-agent-wrapper \
    || fail "CLI wrapper must keep the login user's config as its fallback"
grep -q '^    service_user_file=\${BIANBU_SERVICE_USER_FILE:-/etc/bianbu-agent-runtime/service-user}$' debian/bianbu-agent-wrapper \
    || fail "CLI wrapper must resolve the account the system service runs as"
grep -q 'if \[ -z "\${BIANBU_AGENT_CONFIG:-}" \]; then' debian/bianbu-agent-wrapper \
    || fail "an explicit BIANBU_AGENT_CONFIG must win over the service-user default"
grep -q 'systemctl enable bianbu-agent-runtime.service' debian/postinst \
    || fail "package install must enable the shared system-level Runtime service"
grep -q 'systemctl start bianbu-agent-runtime.service' debian/postinst \
    || fail "package install must start the shared system-level Runtime service"
! grep -q 'runtime_instance\|multi-user.target.wants\|list-unit-files' debian/postinst \
    || fail "package install must not manage per-user Runtime template instances"
! grep -q 'clear-user-data\|clear_runtime_user_data' debian/postinst \
    || fail "package install, reinstall, and upgrade must preserve per-user Runtime data"
! grep -Eq 'rm -rf[^\n]*\.bianbu-agent' debian/postinst \
    || fail "package postinst must never recursively delete the per-user Runtime profile"
grep -q '^service_user_dir=\${BIANBU_POSTINST_SERVICE_USER_DIR:-/etc/bianbu-agent-runtime}$' debian/postinst \
    || fail "package install must record the resolved Runtime run identity"
grep -q '^systemd_drop_in_dir=\${BIANBU_POSTINST_SYSTEMD_DIR:-/etc/systemd/system}$' debian/postinst \
    || fail "package install must publish the run identity into the system manager"
grep -q '^User=\$user$' debian/postinst \
    || fail "the Runtime drop-in must set User= (it cannot read an environment file)"
grep -q 'systemctl stop bianbu-agent-runtime.service' debian/prerm \
    || fail "package removal must stop the shared system-level Runtime service"
grep -q "systemctl stop 'bianbu-agent-runtime@\*.service'" debian/prerm \
    || fail "package removal must stop retired per-user Runtime instances"
grep -q '^WantedBy=multi-user.target$' debian/bianbu-agent-runtime.service \
    || fail "the Runtime service must start at boot without a login"
# 退役桌面图标的清理只能由新包自己的脚本完成：升级时执行的是旧包的 prerm，所以
# postinst 必须在 configure 时清一遍，prerm 负责卸载。两处调用同一个包内脚本。
[ -x debian/cleanup-retired-desktop-icon ] \
    || fail "the retired desktop icon cleanup helper is not executable"
grep -q "for name in 'Bianbu Agent.desktop' 'bianbu-agent.desktop'; do" debian/cleanup-retired-desktop-icon \
    || fail "the cleanup helper must drop the retired display name and the desktop entry file name"
grep -q 'cleanup-retired-desktop-icon' debian/rules \
    || fail "the package must install the retired desktop icon cleanup helper"
for script in debian/postinst debian/prerm; do
    grep -q 'cleanup_retired_desktop_icon() {' "$script" \
        || fail "$script must define the retired desktop icon cleanup call"
    grep -q '^    cleanup_retired_desktop_icon$' "$script" \
        || fail "$script must call the retired desktop icon cleanup"
done
grep -q 'if \[ "$1" = remove \] || \[ "$1" = upgrade \]; then' debian/prerm \
    || fail "package upgrade and removal must both clean up the retired desktop icon"
grep -q '10-service-user.conf' debian/postrm \
    || fail "package removal must drop the generated Runtime run-identity drop-in"
grep -q 'service-user' debian/postrm \
    || fail "package removal must drop the generated Runtime run-identity record"
grep -q 'systemctl daemon-reload' debian/postinst \
    || fail "package install must reload the system manager"
grep -q 'cp -a resources/\. "$(PACKAGE_ROOT)/usr/share/bianbu-agent-runtime/"' debian/rules \
    || fail "package must install the shipped resource tree into the system directory"
grep -q 'for kind in agents mcps models plugins skills' debian/rules \
    || fail "package must install resource kinds into the system directory"
grep -q 'usr/share/bianbu-agent-runtime/\$\$kind' debian/rules \
    || fail "package must install MCP definitions into the shared system directory"
for mcp_name in bibit-readonly bibit-control bibit-admin; do
    [ -r "resources/plugins/bianbu.bibit.intent/mcps/$mcp_name/mcp.json" ] \
        || fail "package must provide the Bibit Intent $mcp_name MCP definition"
done
for agent_dir in .bianbu-agent/agents/*; do
    [ -d "$agent_dir" ] || continue
    [ -r "$agent_dir/agent.yaml" ] \
        || fail "source profile contains an incomplete Agent package: $agent_dir"
done
[ ! -e .bianbu-agent/agents/bianbu-agent-default ] \
    || fail "obsolete fixed starter Agent directory must be removed"
grep -q '60-bianbu-agent-runtime.rules' debian/rules \
    || fail "package does not install the scoped Runtime polkit rule"
grep -q 'var RUNTIME_UNIT = "bianbu-agent-runtime.service";' debian/bianbu-agent-runtime.polkit \
    || fail "polkit rule must bind Worker scopes to the packaged Runtime service"
grep -q 'subject.system_unit' debian/bianbu-agent-runtime.polkit \
    || fail "polkit rule does not identify the Runtime control-plane unit"
# 桌面入口退役后没有任何会话内调用者需要拉起这个 unit：polkit 只放行控制面自己创建的
# Worker scope，不再给本地活动用户开 start/restart。
! grep -q 'subject.local' debian/bianbu-agent-runtime.polkit \
    || fail "polkit rule must not delegate unit management to a local session"
! grep -q 'requestedOwner\|runtimeOwner' debian/bianbu-agent-runtime.polkit \
    || fail "polkit rule must not key off a per-user Runtime owner any more"
if grep -q '^NoNewPrivileges=false' debian/bianbu-agent-runtime.service; then
    ! grep -q 'subject\.no_new_privileges' debian/bianbu-agent-runtime.polkit \
        || fail "polkit rule requires no_new_privileges while the unit disables it"
fi
grep -q '\\.scope' debian/bianbu-agent-runtime.polkit \
    || fail "polkit rule does not restrict Worker scope names"
! grep -q '(service|scope)' debian/bianbu-agent-runtime.polkit \
    || fail "polkit rule must not authorize transient services"
! grep -q 'User=root\|Group=root\|--uid\|--gid' debian/bianbu-agent-runtime.polkit \
    || fail "polkit rule contains a privilege-changing Worker path"
grep -q 'register-security-manifest.py' debian/register-runtime-manifest \
    || fail "device registration does not publish the signed Runtime manifest"
grep -q '^security_agent_id=com.bianbu.runtime$' debian/register-runtime-manifest \
    || fail "device registration must reset/register the fixed Runtime security identity"
grep -q 'security-manifest/\$security_agent_id' debian/register-runtime-manifest \
    || fail "device registration must load the fixed Runtime security manifest"
! grep -q '"$registrar" --optional "$manifest"' debian/register-runtime-manifest \
    || fail "production package must not defer Runtime manifest registration"
grep -q '^ExecStart=/usr/lib/bianbu-agent-runtime/register-runtime-manifest$' debian/bianbu-agent-runtime-register.service \
    || fail "the registration unit must run the device registration script"
grep -q '^Requires=.*bianbu-agent-runtime-register.service' debian/bianbu-agent-runtime.service \
    || fail "the Runtime unit must require device registration before starting"
grep -q 'register-runtime-manifest' debian/rules \
    || fail "package must install the device registration script"
grep -q 'bianbu-agent-runtime-register.service' debian/rules \
    || fail "package must install the device registration unit"
grep -q 'runtime_token=/usr/lib/bianbu-agent-runtime/runtime-admin-token.py' debian/postinst \
    || fail "package install must provision the Runtime admin credential"
grep -q '\$runtime_token" --ensure' debian/postinst \
    || fail "package install must run the Runtime admin credential helper"
grep -q 'runtime-admin-token.py' debian/rules \
    || fail "package must install the Runtime admin credential helper"
grep -q 'network-learning-collector.py.*usr/lib/bianbu-agent-runtime/network-learning-collector.py' debian/rules \
    || fail "package must install the network learning collector"
grep -q 'No signed Security Runtime manifest supplied; refusing production package build' debian/rules \
    || fail "package build does not fail closed without a signed Runtime manifest"
grep -q '^Environment=BIANBU_RUNTIME_ROLE=control$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not select the Runtime control role"
grep -q '^Environment=BIBIT_LLAMA_SERVER_BIN=/usr/bin/llama-server$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not select the packaged SpacemiT llama-server"
! grep -q 'bianbu-agent-runtime-control@\.service' debian/rules \
    || fail "the retired per-user control-plane alias must not be installed"
grep -q '^Environment=BIANBU_RUNTIME_MODE=production$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not select production runtime mode"
grep -q '^Environment=BIANBU_RUNTIME_MANAGED=1$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not carry the trusted manager marker"
grep -q '^Environment=BIANBU_AGENT_MEMORY_REGISTRATION=1$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not enable the managed Memory registration"
! grep -q '^Environment=BIANBU_AGENT_MEMORY_AGENT_NAME=' 'debian/bianbu-agent-runtime.service' \
    || fail "control service must not bind Memory to a global Agent"
grep -q '^Environment=BIANBU_RUNTIME_SECURITY_ID=com.bianbu.runtime$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service must declare the fixed Runtime security identity"
grep -q '^Environment=BIANBU_AGENT_ID=com.bianbu.runtime$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service must bind securityd to the fixed Runtime identity"
grep -q '^WorkingDirectory=~$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service must use the instance user's passwd home"
# ReadWritePaths= 也会让 systemd 为整个 cgroup 树建私有 mount 命名空间：板上实测
# 去掉 PrivateTmp=/ProtectSystem= 后，host Worker 的 mnt ns 仍未回到 init，直到
# 这一条也删除。memory-registry 本来靠 2770 root:bianbu-agent 与 SupplementaryGroups
# 就能写，不需要 remount。
! grep -q '^ReadWritePaths=' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service must not create a private mount namespace for host Workers"
grep -q '^Environment=BIANBU_SECURITY=1$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not require Security Runtime"
grep -q '^Environment=BIANBU_RUNTIME_SECURITY=required$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service allows optional Security Runtime"
grep -q '^Environment=BIANBU_SECURITY_CLIENT_DIR=/usr/lib/bianbu-securityd/client$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not select the packaged security client"
grep -q '^Environment=BIANBU_SEC_UDS=/run/bianbu/securityd.sock$' 'debian/bianbu-agent-runtime.service' \
    || fail "systemd service does not select the production security UDS"
! grep -q '^/usr/lib/systemd/user/bianbu-agent-runtime.service$' debian/rules \
    || fail "the package must not install the obsolete systemd user unit"
file bianbu_agent/console/web/static/logo.jpg | grep -q 'JPEG image data' || fail "logo is not a JPEG image"
# 退役入口必须与当年装进 /usr/share/applications 的字节一致，否则桌面副本匹配不上。
grep -qx 'Exec=/usr/bin/bianbu-agent-launcher' debian/retired-bianbu-agent.desktop \
    || fail "the retired desktop entry reference no longer matches what was installed"
if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate debian/retired-bianbu-agent.desktop
fi
dpkg-source --before-build .

printf 'Debian source package contract is valid.\n'
