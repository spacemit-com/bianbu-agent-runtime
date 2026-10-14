#!/usr/bin/env bash
# run.sh —— 源码 checkout 的统一启动入口：铺开发配置，然后 exec 源码 Runtime。
#
# 这里只有两件事：配置（开发默认环境变量 + 合并出启动用的配置 + 开发自签安全身份）
# 和启动。装依赖、加组、拉起 securityd/memoryd 等一次性环境准备见
# scripts/dev-provision.sh。
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_path="${BIANBU_AGENT_CONFIG:-${project_root}/config/development.yaml}"
export BIANBU_SYSTEM_RESOURCE_ROOT="${BIANBU_SYSTEM_RESOURCE_ROOT:-${project_root}/resources}"

run_as_root() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    # sudo-rs only reads SUDO_ASKPASS with -A, so non-interactive runs (CI, ssh)
    # pass the password through the askpass helper instead of a terminal prompt.
    if [[ -n "${SUDO_ASKPASS:-}" ]]; then
      sudo -A "$@"
    else
      sudo "$@"
    fi
  else
    echo "注册安全身份或让出端口需要 root 权限，但未找到 sudo。" >&2
    return 1
  fi
}

# ── 1. 开发默认配置：以下默认值都可以用同名环境变量覆盖 ─────────────────────
if [[ "${BIANBU_RUNTIME_MODE:-development}" == "production" \
  && "${BIANBU_RUNTIME_MANAGED:-}" != "1" ]]; then
  echo "生产模式禁止通过 run.sh 裸启动；请由受信 Runtime Manager/systemd 启动。" >&2
  exit 78
fi

export BIANBU_SECURITY=1
export BIANBU_SANDBOX="${BIANBU_SANDBOX:-off}"
export BIANBU_RUNTIME_SECURITY="${BIANBU_RUNTIME_SECURITY:-required}"
# 不为 Worker 施加 cgroup MemoryMax：securityd 的默认 2 GiB 在单板设备上没有
# 需要保护的其它租户，长会话却可能被它杀掉。需要复现限额/OOM 时显式给一个
# 尺寸即可（例如 BIANBU_WORKER_MEMORY_MAX=2G ./run.sh）。
export BIANBU_WORKER_MEMORY_MAX="${BIANBU_WORKER_MEMORY_MAX:-off}"
# 同理不把 Worker 限到 1 核：8 核板子上 Hook 子进程、engine 构建与工具调用
# 本来就在抢同一颗核，放开后它们才能真正并行。给值即可恢复（例如
# BIANBU_WORKER_CPU_QUOTA=200% ./run.sh）。
export BIANBU_WORKER_CPU_QUOTA="${BIANBU_WORKER_CPU_QUOTA:-off}"
export BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER="${BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER:-0}"
export BIANBU_RUNTIME_SECURITY_ID="${BIANBU_RUNTIME_SECURITY_ID:-com.bianbu.runtime.development}"
export BIANBU_AGENT_ID="${BIANBU_AGENT_ID:-${BIANBU_RUNTIME_SECURITY_ID}}"
export BIANBU_PA_AGENT_ID="${BIANBU_PA_AGENT_ID:-${BIANBU_RUNTIME_SECURITY_ID}}"
# 开发态安全调用默认走系统 securityd；只连会话总线上的开发实例时才显式设为 1。
export BIANBU_SEC_UDS="${BIANBU_SEC_UDS:-/run/bianbu/securityd.sock}"
export BIANBU_SEC_SESSION="${BIANBU_SEC_SESSION:-0}"
export BIANBU_SECURITY_TEST_ADMIN="${BIANBU_SECURITY_TEST_ADMIN:-1}"
export BIANBU_AGENT_MEMORY_ENABLED="${BIANBU_AGENT_MEMORY_ENABLED:-true}"
export BIANBU_AGENT_MEMORY_REGISTRATION="${BIANBU_AGENT_MEMORY_REGISTRATION:-1}"
export BIANBU_AGENT_MEMORY_SOCKET="${BIANBU_AGENT_MEMORY_SOCKET:-/run/bianbu/agent-memoryd.sock}"
export BIANBU_AGENT_MEMORY_REGISTRY_DIR="${BIANBU_AGENT_MEMORY_REGISTRY_DIR:-/run/bianbu/agent-runtime/memory-registry}"
export BIBIT_INTENT_ROOT="${BIBIT_INTENT_ROOT:-/opt/bibit-intent}"
export BIBIT_INTENT_BASE_URL="${BIBIT_INTENT_BASE_URL:-http://127.0.0.1:8765}"

# 源码 checkout 的安全客户端来自同级的 bianbu-agent-security 树（worktree 也能找到）。
# Runtime 跑 CPython 3.14，而发行版 dbus-python 扩展是 3.12 构建的，所以桥接脚本
# 需要这个目录出现在 PYTHONPATH 上。
if [[ -z "${BIANBU_SECURITYD_HOME:-}" ]]; then
  securityd_scan="${project_root}"
  while [[ "${securityd_scan}" != "/" ]]; do
    if [[ -d "${securityd_scan}/bianbu-agent-security/impl/bianbu-securityd" ]]; then
      export BIANBU_SECURITYD_HOME="$(cd \
        "${securityd_scan}/bianbu-agent-security/impl/bianbu-securityd" && pwd -P)"
      break
    fi
    securityd_scan="$(dirname "${securityd_scan}")"
  done
fi
if [[ -z "${BIANBU_SECURITY_CLIENT_DIR:-}" ]]; then
  if [[ -d "/usr/lib/bianbu-securityd/client" ]]; then
    export BIANBU_SECURITY_CLIENT_DIR="/usr/lib/bianbu-securityd/client"
  elif [[ -n "${BIANBU_SECURITYD_HOME:-}" && -d "${BIANBU_SECURITYD_HOME}/client" ]]; then
    export BIANBU_SECURITY_CLIENT_DIR="${BIANBU_SECURITYD_HOME}/client"
  fi
fi
if [[ -n "${BIANBU_SECURITY_CLIENT_DIR:-}" ]]; then
  export PYTHONPATH="${BIANBU_SECURITY_CLIENT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
fi

# 不要让 shell 级的 pip/uv find-links 覆盖这个 locked 项目：过期的 PyTorch wheel URL
# 会让每次 `uv run` 在应用启动前就失败，而 PyTorch 并不是项目依赖。
unset UV_FIND_LINKS

# ── 2. 把 Memory SDK 接进源码 venv ─────────────────────────────────────────
# SDK 由 apt 装在系统 dist-packages：打包 Runtime 的 venv 带 --system-site-packages，
# 直接就能 import；源码 venv 没有这个开关。而 Worker 起的 Command Hook 与 MCP 子进程
# 环境是被裁剪过的（拿不到 PYTHONPATH），可执行文件却都是这个 venv 的解释器，所以这里
# 只把 SDK 一个目录写成 venv 级 import 路径（host 与沙箱内两个路径都写，各取所需）。
memory_sdk_dir="${BIANBU_AGENT_MEMORY_SDK_DIR:-/usr/lib/python3/dist-packages/bianbu_agent_sdk}"
memory_python_root="${project_root}/.bianbu-agent/runtime/python-packages"
mkdir -p "${memory_python_root}"
ln -sfnT "${memory_sdk_dir}" "${memory_python_root}/bianbu_agent_sdk"
site_packages="$(uv run --locked --no-dev python -c \
  'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
printf '%s\n' "${memory_python_root}" "/home/agent/runtime/python-packages" \
  > "${site_packages}/bianbu-agent-memory-sdk.pth"

# ── 3. 生成启动配置：合并安全清单、把 Worker 指到源码入口、备好 Starter Agent ──
runtime_config="${BIANBU_ALL_CONFIG:-${project_root}/.bianbu-agent/config-all.yaml}"
manifest_root="${BIANBU_ALL_SECURITY_MANIFEST_ROOT:-${project_root}/.bianbu-agent/runtime/development-security-manifest}"
uv run --locked --no-dev python "${project_root}/scripts/prepare-development-security-manifest.py" \
  --project-root "${project_root}" \
  --config "${config_path}" \
  --runtime-config-output "${runtime_config}" \
  --output "${manifest_root}"
config_path="${runtime_config}"

starter_agent_id="$(uv run --locked --no-dev python \
  "${project_root}/scripts/provision-starter-agent.py" \
  --config "${config_path}" \
  --template "${project_root}/resources/default-profile/starter-agent.yaml")"
echo "Starter Agent 已就绪：${starter_agent_id}" >&2
export COM_BIANBU_AGENT_ID="${COM_BIANBU_AGENT_ID:-${starter_agent_id}}"
export BIANBU_WORKER_AGENT_ID="${BIANBU_WORKER_AGENT_ID:-${COM_BIANBU_AGENT_ID}}"

# ── 4. 注册开发自签安全身份（需要 root）─────────────────────────────────────
# 自签清单每次启动现生成（key 也是新的），所以注册跟生成必须同源，不能挪到一次性准备。
manifest_package="${manifest_root}/${BIANBU_RUNTIME_SECURITY_ID}"
manifest_stage="${BIANBU_SECURITY_MANIFEST_STAGE:-/run/bianbu/runtime-manifests/${BIANBU_RUNTIME_SECURITY_ID}}"
run_as_root install -d -m 0750 -o bianbu-security -g bianbu-security "${manifest_stage}"
for manifest_file in manifest.toml manifest.toml.sig pubkey.hex; do
  run_as_root install -m 0640 -o bianbu-security -g bianbu-security \
    "${manifest_package}/${manifest_file}" "${manifest_stage}/${manifest_file}"
done
run_as_root env \
  BIANBU_SEC_SESSION="${BIANBU_SEC_SESSION}" \
  BIANBU_SECURITY_CLIENT_DIR="${BIANBU_SECURITY_CLIENT_DIR:-}" \
  "${project_root}/debian/reset-security-binding.py" "${BIANBU_RUNTIME_SECURITY_ID}" source-run
run_as_root env \
  BIANBU_SEC_SESSION="${BIANBU_SEC_SESSION}" \
  BIANBU_SECURITY_CLIENT_DIR="${BIANBU_SECURITY_CLIENT_DIR:-}" \
  BIANBU_SECURITY_TEST_ADMIN="${BIANBU_SECURITY_TEST_ADMIN}" \
  "${project_root}/debian/register-security-manifest.py" "${manifest_stage}" \
  --expected-agent-id "${BIANBU_RUNTIME_SECURITY_ID}"

# ── 5. 让出端口：停掉打包实例与同一数据目录下的残留实例 ─────────────────────
runtime_instance="${BIANBU_INSTALLED_RUNTIME_UNIT:-bianbu-agent-runtime.service}"
if systemctl is-active --quiet "${runtime_instance}" 2>/dev/null; then
  echo "停止占用源码 Runtime 端口的已安装系统服务：${runtime_instance}" >&2
  run_as_root systemctl stop "${runtime_instance}"
fi
uv run --locked --no-dev python \
  "${project_root}/scripts/stop-development-runtime.py" \
  --config "${config_path}" --timeout 3

# ── 6. 启动 ───────────────────────────────────────────────────────────────
exec uv run --locked --no-dev bianbu-agent \
  --config "${config_path}" "$@"
