#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
postrm=$repo_root/debian/postrm
work=$(mktemp -d)
cleanup() {
    rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

state_dir=$work/var-lib-bianbu-agent-runtime
profile_dir=$work/var-lib-bianbu-agent-profiles
etc_dir=$work/etc-bianbu-agent
run_dir=$work/run-bianbu-agent-runtime
systemd_dir=$work/etc-systemd
service_user_dir=$work/etc-bianbu-agent-runtime
home=$work/home/bianbu

prepare_runtime_state() {
    rm -rf "$state_dir" "$profile_dir" "$etc_dir" "$run_dir" "$home" \
        "$systemd_dir" "$service_user_dir"
    install -d "$etc_dir" "$run_dir/memory-registry" \
        "$home/.bianbu-agent/sessions" \
        "$profile_dir/0123456789abcdef/agents" \
        "$state_dir/profiles/0123456789abcdef/agents" \
        "$systemd_dir/bianbu-agent-runtime.service.d" \
        "$service_user_dir"
    printf '%s\n' com.bianbu.012345abcdef >"$profile_dir/0123456789abcdef/starter-agent-id"
    printf '%s\n' retired >"$state_dir/profiles/0123456789abcdef/starter-agent-id"
    printf '%s\n' bianbu >"$state_dir/desktop-user"
    printf '%s\n' digest >"$state_dir/registered-manifest.sha256"
    printf '%s\n' retired >"$state_dir/active-users"
    printf '%s\n' env >"$etc_dir/runtime.env"
    printf '%s\n' token >"$etc_dir/runtime-admin-token"
    printf '%s\n' session >"$home/.bianbu-agent/sessions/session.json"
    printf '%s\n' '[Service]' "User=bianbu" \
        >"$systemd_dir/bianbu-agent-runtime.service.d/10-service-user.conf"
    printf '%s\n' bianbu >"$service_user_dir/service-user"
}

run_postrm() {
    BIANBU_POSTRM_STATE_DIR="$state_dir" \
    BIANBU_POSTRM_PROFILE_DIR="$profile_dir" \
    BIANBU_POSTRM_ETC_DIR="$etc_dir" \
    BIANBU_POSTRM_RUN_DIR="$run_dir" \
    BIANBU_POSTRM_SYSTEMD_DIR="$systemd_dir" \
    BIANBU_POSTRM_SERVICE_USER_DIR="$service_user_dir" \
        "$postrm" "$1"
}

prepare_runtime_state
run_postrm remove

test ! -e "$state_dir/desktop-user"
test ! -e "$state_dir/registered-manifest.sha256"
test ! -e "$state_dir/active-users"
test ! -e "$state_dir/profiles"
test ! -e "$profile_dir"
# 运行身份是安装时生成的配置：包被移除后不能留下指向已不存在用户的 drop-in。
test ! -e "$systemd_dir/bianbu-agent-runtime.service.d/10-service-user.conf"
test ! -e "$systemd_dir/bianbu-agent-runtime.service.d"
test ! -e "$service_user_dir/service-user"
test ! -e "$service_user_dir"
test -e "$etc_dir/runtime.env"
test -e "$etc_dir/runtime-admin-token"
test -d "$run_dir/memory-registry"
test "$(cat "$home/.bianbu-agent/sessions/session.json")" = session

prepare_runtime_state
run_postrm purge

test ! -e "$state_dir"
test ! -e "$profile_dir"
test ! -e "$etc_dir"
test ! -e "$run_dir"
test "$(cat "$home/.bianbu-agent/sessions/session.json")" = session

run_postrm purge

printf '%s\n' 'postrm cleanup test passed.'
