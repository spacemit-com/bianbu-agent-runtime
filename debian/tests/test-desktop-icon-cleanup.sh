#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
helper=$repo_root/debian/cleanup-retired-desktop-icon
work=$(mktemp -d)
cleanup() {
    rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

template=$work/usr-share-applications/bianbu-agent.desktop
device_home=$work/home/device-user
desktop_dir=$device_home/Desktop
user_marker=$work/etc-bianbu-agent-runtime/service-user
legacy_marker=$work/var-lib-bianbu-agent-runtime/desktop-user
missing_marker=$work/absent-marker
# 桌面上的副本有两种名字：退役安装器用显示名，从开始菜单复制到桌面用 desktop 文件名。
legacy_name='Bianbu Agent.desktop'
entry_name='bianbu-agent.desktop'

prepare() {
    rm -rf "$work/home" "$work/etc-bianbu-agent-runtime" \
        "$work/var-lib-bianbu-agent-runtime" "$work/usr-share-applications"
    install -d "$desktop_dir" "$(dirname -- "$user_marker")" \
        "$(dirname -- "$template")"
    # 记录文件里的账号必须真实存在：脚本从 getent 取 uid 与家目录，这里用当前
    # 账号，再把要检查的家目录指到临时目录。
    printf '%s\n' "$(id -un)" >"$user_marker"
    printf '%s\n' '[Desktop Entry]' 'Name=Bianbu Agent' >"$template"
}

run_helper() {
    BIANBU_DESKTOP_ICON_TEMPLATE=${1:-$template} \
    BIANBU_DESKTOP_ICON_HOME=$device_home \
    BIANBU_SERVICE_USER_FILE=$user_marker \
    BIANBU_DESKTOP_USER_FILE=$missing_marker \
        "$helper"
}

for name in "$legacy_name" "$entry_name"; do
    # 旧版本自动装的副本：清掉。
    prepare
    cp "$template" "$desktop_dir/$name"
    run_helper
    test ! -e "$desktop_dir/$name"

    # 用户自己改过的副本：不动。
    prepare
    printf '%s\n' '[Desktop Entry]' 'Name=My Agent' >"$desktop_dir/$name"
    run_helper
    test -e "$desktop_dir/$name"

    # 指向别处的符号链接：不动（不跟随）。
    prepare
    ln -s /etc/hostname "$desktop_dir/$name"
    run_helper
    test -L "$desktop_dir/$name"

    # 中文桌面目录同样处理。
    prepare
    install -d "$device_home/桌面"
    cp "$template" "$device_home/桌面/$name"
    run_helper
    test ! -e "$device_home/桌面/$name"

    # 用户把桌面目录改成别的名字（旧版本按 xdg-user-dir 装到那里）：读配置找到并清掉。
    prepare
    install -d "$device_home/.config" "$device_home/MyDesktop"
    printf '%s\n' 'XDG_DESKTOP_DIR="$HOME/MyDesktop"' >"$device_home/.config/user-dirs.dirs"
    cp "$template" "$device_home/MyDesktop/$name"
    run_helper
    test ! -e "$device_home/MyDesktop/$name"
done

# 两个名字同时存在：都清掉。
prepare
cp "$template" "$desktop_dir/$legacy_name"
cp "$template" "$desktop_dir/$entry_name"
run_helper
test ! -e "$desktop_dir/$legacy_name"
test ! -e "$desktop_dir/$entry_name"

# 真机现场：桌面上的 `bianbu-agent.desktop` 与退役入口逐字节相同，升级时必须消失。
prepare
cp "$repo_root/debian/retired-bianbu-agent.desktop" "$desktop_dir/$entry_name"
run_helper "$repo_root/debian/retired-bianbu-agent.desktop"
test ! -e "$desktop_dir/$entry_name"

# 只有退役的 desktop-user 记录、没有 service-user：照样清掉。
prepare
install -d "$(dirname -- "$legacy_marker")"
printf '%s\n' "$(id -un)" >"$legacy_marker"
cp "$template" "$desktop_dir/$entry_name"
BIANBU_DESKTOP_ICON_TEMPLATE=$template \
BIANBU_DESKTOP_ICON_HOME=$device_home \
BIANBU_SERVICE_USER_FILE=$missing_marker \
BIANBU_DESKTOP_USER_FILE=$legacy_marker \
    "$helper"
test ! -e "$desktop_dir/$entry_name"

# 未知账号：跳过，不报错。
prepare
cp "$template" "$desktop_dir/$entry_name"
printf '%s\n' no-such-user-4a1b >"$user_marker"
run_helper
test -e "$desktop_dir/$entry_name"

# 包内模板不存在（包已被拆掉）：直接返回，不报错，也不动任何东西。
prepare
cp "$template" "$desktop_dir/$entry_name"
rm -f "$template"
run_helper
test -e "$desktop_dir/$entry_name"

printf '%s\n' 'desktop icon cleanup test passed.'
