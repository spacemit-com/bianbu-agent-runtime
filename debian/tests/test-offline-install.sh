#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
postinst=$repo_root/debian/postinst
register=$repo_root/debian/register-runtime-manifest
register_unit=$repo_root/debian/bianbu-agent-runtime-register.service
runtime_unit=$repo_root/debian/bianbu-agent-runtime.service
network_unit=$repo_root/debian/bianbu-agent-network-learning.service

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

# A package install has to work on a plain filesystem: pbuilder, a chroot, a
# container, CI and firmware builds have no running systemd, no D-Bus and no
# securityd.  The maintainer script therefore may not reach any of that and may
# not have a failure path at all, so the transaction always finishes.
grep -q '^exit 0$' "$postinst" || fail "postinst must end successfully"
! grep -q 'exit 1' "$postinst" || fail "postinst must not abort a package transaction"
! grep -Eq 'return 1' "$postinst" || fail "postinst must not fail on a missing live prerequisite"
for forbidden in 'register-security-manifest' 'reset-security-binding' 'systemd-tmpfiles' \
                 'rollback_root' 'BIANBU_RUNTIME_SKIP_SYSTEM_INTEGRATION' '/run/bianbu/'; do
    ! grep -q -- "$forbidden" "$postinst" \
        || fail "postinst must not own the device-side step: $forbidden"
done
# The membership is best effort at install time: securityd owns the group and
# may simply not be configured yet.
grep -q 'adduser bianbu-runtime bianbu-security' "$postinst" \
    || fail "postinst must still grant the securityd membership when it can"
grep -q 'the Runtime start path grants it' "$postinst" \
    || fail "postinst must defer the missing membership instead of failing"
# systemd is only ever asked best effort, and the answer stays invisible.
grep -q 'systemctl daemon-reload >/dev/null 2>&1 || true' "$postinst" \
    || fail "postinst must not depend on a running manager"
grep -q 'systemctl start bianbu-agent-runtime.service >/dev/null 2>&1 || true' "$postinst" \
    || fail "postinst must not report a missing manager as an install error"

# Everything that needs the live device lives in one device-side entry point
# that the Runtime units pull in.
[ -x "$register" ] || fail "device registration script is not executable"
for required in 'bianbu-security group is missing' 'adduser bianbu-runtime bianbu-security' \
                '/run/bianbu/network-learning' '/run/bianbu/agent-runtime/memory-registry' \
                'reset-security-binding.py' 'register-security-manifest.py' \
                'registered-manifest.sha256' '--if-missing'; do
    grep -q -- "$required" "$register" \
        || fail "device registration script lost: $required"
done
grep -q '^ExecStart=/usr/lib/bianbu-agent-runtime/register-runtime-manifest$' "$register_unit" \
    || fail "registration unit must call the device registration script"
grep -q '^Type=oneshot$' "$register_unit" \
    || fail "registration unit must be a oneshot job"
! grep -q '^\[Install\]' "$register_unit" \
    || fail "registration unit must be pulled in by the Runtime, not installed enabled"
grep -q '^After=.*bianbu-agent-runtime-register.service' "$runtime_unit" \
    || fail "Runtime must start after device registration"
grep -q '^Requires=.*bianbu-agent-runtime-register.service' "$runtime_unit" \
    || fail "Runtime must require device registration"
grep -q '^After=.*bianbu-agent-runtime-register.service' "$network_unit" \
    || fail "network learning collector must start after device registration"

# Behavioural check: run the install-time helpers against a fake host that has
# no login session, no securityd group and a failing manager.  The install must
# still succeed and stay silent.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM
fake_bin=$work/bin
log=$work/calls.log
mkdir -p "$fake_bin"
: > "$log"

cat > "$fake_bin/install" <<'FAKE'
#!/bin/sh
printf 'install %s\n' "$*" >> "$FAKE_LOG"
FAKE
cat > "$fake_bin/getent" <<'FAKE'
#!/bin/sh
# A firmware build has none of these: no securityd group, no Runtime account
# and no login user.
exit 1
FAKE
cat > "$fake_bin/systemctl" <<'FAKE'
#!/bin/sh
printf 'systemctl %s\n' "$*" >> "$FAKE_LOG"
printf '%s\n' 'System has not been booted with systemd as init system (PID 1). Can'"'"'t operate.' >&2
exit 1
FAKE
cat > "$fake_bin/runuser" <<'FAKE'
#!/bin/sh
printf '%s\n' 'Failed to connect to bus: No such file or directory' >&2
exit 1
FAKE
cat > "$fake_bin/adduser" <<'FAKE'
#!/bin/sh
printf 'adduser %s\n' "$*" >> "$FAKE_LOG"
FAKE
cat > "$fake_bin/addgroup" <<'FAKE'
#!/bin/sh
printf 'addgroup %s\n' "$*" >> "$FAKE_LOG"
FAKE
cat > "$fake_bin/usermod" <<'FAKE'
#!/bin/sh
printf 'usermod %s\n' "$*" >> "$FAKE_LOG"
FAKE
cat > "$fake_bin/logname" <<'FAKE'
#!/bin/sh
# A firmware build has no login session to resolve.
exit 1
FAKE
chmod +x "$fake_bin"/*

harness() {
    function=$1
    {
        printf '%s\n' 'data_root=/var/lib/bianbu-agent-runtime'
        printf '%s\n' 'service_user_dir=${BIANBU_POSTINST_SERVICE_USER_DIR:-/etc/bianbu-agent-runtime}'
        printf '%s\n' 'systemd_drop_in_dir=${BIANBU_POSTINST_SYSTEMD_DIR:-/etc/systemd/system}'
        sed -n "/^$function() {\$/,/^}\$/p" "$postinst"
        printf '%s\n' 'warn() { printf "%s\\n" "bianbu-agent-runtime: $*" >&2; }'
        printf '%s\n' "$function"
    } | env -u SUDO_USER -u PKEXEC_UID PATH="$fake_bin:$PATH" FAKE_LOG="$log" \
        BIANBU_POSTINST_SERVICE_USER_DIR="$work/etc-bianbu-agent-runtime" \
        BIANBU_POSTINST_SYSTEMD_DIR="$work/etc-systemd" sh
}

: > "$log"
harness ensure_runtime_user 2> "$work/user.err" \
    || fail "filesystem-only install must create the Runtime account without securityd"
grep -q 'adduser --system' "$log" || fail "filesystem-only install must create the Runtime user"
[ ! -s "$work/user.err" ] || fail "missing securityd group must stay silent at install time"

: > "$log"
harness configure_system_service_user 2> "$work/service-user.err" \
    || fail "install must tolerate an image without a login session"
[ ! -s "$work/service-user.err" ] || fail "absent login session must not be reported as an install error"
! grep -q 'Desktop' "$log" || fail "install must not copy an icon onto a user desktop"
[ ! -e "$work/etc-bianbu-agent-runtime/service-user" ] \
    || fail "install must not invent a run identity when no device user exists"

# On a real device the resolved account has to be published as a systemd
# drop-in: User= cannot read an environment file, and the packaged default is
# the dedicated bianbu-runtime account.
home_dir=$work/home/bianbu
mkdir -p "$home_dir"
# The fake install only records its arguments, so create the two directories the
# postinst would create itself; the log assertions below still prove it asked.
mkdir -p "$work/etc-bianbu-agent-runtime" \
    "$work/etc-systemd/bianbu-agent-runtime.service.d"
cat > "$fake_bin/getent" <<FAKE
#!/bin/sh
case "\$*" in
    'passwd 1000'|'passwd bianbu') printf '%s\n' "bianbu:x:1000:1000::${home_dir}:/bin/bash" ;;
    'group bianbu-agent') printf '%s\n' 'bianbu-agent:x:1001:' ;;
    *) exit 1 ;;
esac
FAKE
chmod +x "$fake_bin/getent"

: > "$log"
harness configure_system_service_user 2> "$work/identity.err" \
    || fail "install must publish the resolved Runtime run identity"
[ ! -s "$work/identity.err" ] || fail "publishing the run identity must stay silent"
grep -qx 'bianbu' "$work/etc-bianbu-agent-runtime/service-user" \
    || fail "install must record the Runtime service user"
grep -q 'install -d -m 0755 .*etc-bianbu-agent-runtime' "$log" \
    || fail "install must create the run-identity directory"
grep -q 'install -d -m 0755 .*bianbu-agent-runtime.service.d' "$log" \
    || fail "install must create the systemd drop-in directory"
cat > "$work/expected-drop-in" <<'EXPECT'
[Service]
User=bianbu
Environment=BIANBU_RUNTIME_SERVICE_USER=bianbu
Environment=BIANBU_AGENT_MEMORY_ACCOUNT_ID=bianbu
EXPECT
cmp "$work/expected-drop-in" \
    "$work/etc-systemd/bianbu-agent-runtime.service.d/10-service-user.conf" \
    || fail "the drop-in must set User= and the Runtime identity variables"

: > "$log"
harness activate_installed_units 2> "$work/units.err" \
    || fail "install must tolerate a host without a running systemd manager"
grep -q 'systemctl daemon-reload' "$log" || fail "install must still refresh the manager when it exists"
[ ! -s "$work/units.err" ] || fail "a missing systemd manager must not be reported as an install error"


# 用 root 直接安装（没有 SUDO_USER 与登录会话）时必须仍然解析到设备用户，否则
# Runtime 会退回到包内的 bianbu-runtime 账号，host 专家不再以设备用户身份工作。
: > "$log"
harness configure_system_service_user 2> "$work/root-install.err" \
    || fail "a root install must still resolve the device user"
[ ! -s "$work/root-install.err" ] || fail "a root install must stay silent"
grep -qx 'bianbu' "$work/etc-bianbu-agent-runtime/service-user" \
    || fail "a root install must record the device user"

# uid 1000 被别的账号占用时退一步取 uid 最小的普通账号。
worker_home=$work/home/worker
mkdir -p "$worker_home"
cat > "$fake_bin/getent" <<FAKE
#!/bin/sh
case "\$*" in
    'passwd') printf '%s\n' \
        'daemon2:x:1009:1009::/home/daemon2:/bin/bash' \
        "worker:x:1007:1007::${worker_home}:/bin/bash" ;;
    'passwd worker') printf '%s\n' "worker:x:1007:1007::${worker_home}:/bin/bash" ;;
    'group bianbu-agent') printf '%s\n' 'bianbu-agent:x:1001:' ;;
    *) exit 1 ;;
esac
FAKE
chmod +x "$fake_bin/getent"

: > "$log"
rm -f "$work/etc-bianbu-agent-runtime/service-user"
harness configure_system_service_user 2> "$work/worker.err" \
    || fail "install must fall back to the lowest regular account"
[ ! -s "$work/worker.err" ] || fail "resolving the fallback account must stay silent"
grep -qx 'worker' "$work/etc-bianbu-agent-runtime/service-user" \
    || fail "install must prefer the lowest uid when 1000 is taken"

# 记录里的账号已经被删掉：不能继续用它，要重新解析，否则 drop-in 指向不存在的用户。
printf '%s\n' ghost >"$work/etc-bianbu-agent-runtime/service-user"
cat > "$fake_bin/getent" <<FAKE
#!/bin/sh
case "\$*" in
    'passwd 1000') printf '%s\n' "bianbu:x:1000:1000::${home_dir}:/bin/bash" ;;
    'passwd bianbu') printf '%s\n' "bianbu:x:1000:1000::${home_dir}:/bin/bash" ;;
    'group bianbu-agent') printf '%s\n' 'bianbu-agent:x:1001:' ;;
    *) exit 1 ;;
esac
FAKE
chmod +x "$fake_bin/getent"

: > "$log"
harness configure_system_service_user 2> "$work/ghost.err" \
    || fail "install must recover from a stale run-identity record"
[ ! -s "$work/ghost.err" ] || fail "recovering from a stale identity must stay silent"
grep -qx 'bianbu' "$work/etc-bianbu-agent-runtime/service-user" \
    || fail "install must replace a run identity whose account no longer exists"


# 退役桌面图标的清理放在新包自己的脚本里（升级跑的是旧包的 prerm）：configure 必须
# 调用包内清理脚本，脚本不在时只跳过清理，不能让安装失败或出声。
cat > "$fake_bin/cleanup-retired-desktop-icon" <<'FAKE'
#!/bin/sh
printf 'cleanup-retired-desktop-icon\n' >>"$FAKE_LOG"
FAKE
chmod +x "$fake_bin/cleanup-retired-desktop-icon"

: > "$log"
BIANBU_POSTINST_DESKTOP_CLEANUP="$fake_bin/cleanup-retired-desktop-icon" \
    harness cleanup_retired_desktop_icon 2> "$work/icon.err" \
    || fail "configure must clean the retired desktop icon"
grep -qx 'cleanup-retired-desktop-icon' "$log" \
    || fail "configure must call the retired desktop icon cleanup helper"
[ ! -s "$work/icon.err" ] || fail "cleaning the retired desktop icon must stay silent"

: > "$log"
BIANBU_POSTINST_DESKTOP_CLEANUP="$work/absent-cleanup-helper" \
    harness cleanup_retired_desktop_icon 2> "$work/icon-missing.err" \
    || fail "a missing cleanup helper must not fail the install"
[ ! -s "$work/icon-missing.err" ] || fail "a missing cleanup helper must stay silent"

printf '%s\n' 'Offline install test passed.'
