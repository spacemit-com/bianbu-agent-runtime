#!/usr/bin/env bash
# dev-provision.sh —— 源码 checkout 的一次性环境准备（幂等，可重复执行）。
#
# 只做"把环境装好"，不启动 Runtime：
#   * apt 依赖（与 debian/control 的 Depends 对齐，含 curl —— 引导 uv 用）
#   * securityd/memoryd 服务拉起与 socket 就绪
#   * 当前用户的 bianbu-agent/bianbu-security 组权限
#   * uv、SearXNG、私有模型记录权限
# 启动 Runtime 用 ./run.sh。
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export BIANBU_SYSTEM_RESOURCE_ROOT="${BIANBU_SYSTEM_RESOURCE_ROOT:-${project_root}/resources}"
export BIANBU_SEC_UDS="${BIANBU_SEC_UDS:-/run/bianbu/securityd.sock}"
export BIANBU_AGENT_MEMORY_SOCKET="${BIANBU_AGENT_MEMORY_SOCKET:-/run/bianbu/agent-memoryd.sock}"
export BIANBU_AGENT_MEMORY_REGISTRY_DIR="${BIANBU_AGENT_MEMORY_REGISTRY_DIR:-/run/bianbu/agent-runtime/memory-registry}"
export BIANBU_SECURITY_CLIENT_DIR="${BIANBU_SECURITY_CLIENT_DIR:-/usr/lib/bianbu-securityd/client}"
export BIBIT_INTENT_BASE_URL="${BIBIT_INTENT_BASE_URL:-http://127.0.0.1:8765}"

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
    echo "准备开发环境需要 root 权限，但未找到 sudo。" >&2
    return 1
  fi
}

install_all_packages() {
  # 与 debian/control 中 bianbu-agent-runtime 的 Depends 保持同步（curl 是引导 uv
  # 用的额外一项），tests/test_dev_provision_script.py 会校验两边一致，避免这里再
  # 出现像 bibit-intent-embed.service 那样硬编码过期内容。
  #
  # 这些包的服务单元（securityd/memoryd/auditd/bibit-intent/searxng…）由各自的包
  # 自己安装并 enable：这里不 enable、不 start，也不改它们的配置或目录，只保证依赖
  # 已安装且是仓库中的最新候选版本，因此始终刷新索引并安装一次（apt-get install
  # 对已安装的包等价于升级）。
  local packages=(
    agent-browser
    auditd
    bianbu-agent-memory
    bianbu-agent-securityd
    bibit-intent
    ca-certificates
    llama.cpp-tools-spacemit
    libfst26
    python3.14
    searxng
    spacemit-ai-gateway
    systemd
    polkitd
    adduser
    curl
  )
  echo "通过 APT 安装/升级全部依赖：${packages[*]}" >&2
  run_as_root apt-get update
  run_as_root apt-get install -y "${packages[@]}"
}

ensure_uv() {
  command -v uv >/dev/null 2>&1 && return 0
  if [[ -z "${UV_INSTALL_DIR:-}" && -z "${HOME:-}" ]]; then
    echo "未找到 uv，且 HOME/UV_INSTALL_DIR 均未设置，无法确定安装目录。" >&2
    return 1
  fi
  uv_install_dir="${UV_INSTALL_DIR:-${HOME}/.local/bin}"
  export PATH="${uv_install_dir}:${PATH}"
  if [[ ! -x "${uv_install_dir}/uv" ]]; then
    command -v curl >/dev/null 2>&1 || {
      echo "未找到 uv，且无法通过 curl 自动安装。请先安装 curl 后重试。" >&2
      return 1
    }
    echo "未找到 uv，正在安装到 ${uv_install_dir}..." >&2
    curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="${uv_install_dir}" UV_NO_MODIFY_PATH=1 sh
  fi
  command -v uv >/dev/null 2>&1 || {
    echo "uv 安装完成，但在 ${uv_install_dir} 中未找到可执行文件。" >&2
    return 1
  }
}

group_list_contains() {
  local groups=$1 expected=$2
  tr ' ' '\n' <<<"${groups}" | grep -qx "${expected}"
}

join_service_groups() {
  # 包只建组、只加服务账号（如 bianbu-memory）；把"跑 Runtime 的用户"加进访问组是
  # 开发环境的活。socket 与 registry 的权限就挂在这两个组上：
  #   /run/bianbu/agent-memoryd.sock        bianbu-memory:bianbu-agent
  #   /run/bianbu/securityd.sock            bianbu-security:bianbu-security
  #   /run/bianbu/agent-runtime/memory-registry  root:bianbu-agent
  local runtime_user missing_groups=() group
  runtime_user="$(id -un)"
  for group in bianbu-agent bianbu-security; do
    group_list_contains "$(id -nG "${runtime_user}")" "${group}" \
      || missing_groups+=("${group}")
  done
  [[ ${#missing_groups[@]} -gt 0 ]] || return 0
  run_as_root usermod -a -G \
    "$(IFS=,; echo "${missing_groups[*]}")" "${runtime_user}"
  echo "已把 ${runtime_user} 加入 ${missing_groups[*]} 组；组权限要重新登录（或重开终端）后生效。" >&2
}

wait_for_socket() {
  local label=$1 path=$2 timeout=${3:-30} attempt
  for ((attempt = 0; attempt < timeout * 10; attempt++)); do
    [[ -S "${path}" ]] && return 0
    sleep 0.1
  done
  echo "${label} 未就绪：${path}" >&2
  return 1
}

ensure_service_running() {
  local unit=$1
  # 单元由各自的包安装并 enable，这里只保证"现在在跑"：装包触发的启动是异步的，
  # 服务也可能处于 failed/stopped，先看状态，没跑才 start。
  systemctl is-active --quiet "${unit}" 2>/dev/null \
    || run_as_root systemctl start "${unit}"
}

start_services() {
  [[ -d "${BIANBU_SECURITY_CLIENT_DIR}" ]] || {
    echo "安全客户端不存在：${BIANBU_SECURITY_CLIENT_DIR}" >&2
    return 1
  }
  ensure_service_running bianbu-securityd.service
  wait_for_socket "Security 服务" "${BIANBU_SEC_UDS}" 30

  ensure_service_running bianbu-agent-memoryd.service
  wait_for_socket "Agent Memory 服务" "${BIANBU_AGENT_MEMORY_SOCKET}" 30
  [[ -d "${BIANBU_AGENT_MEMORY_REGISTRY_DIR}" ]] || {
    echo "Agent Memory registry 不存在：${BIANBU_AGENT_MEMORY_REGISTRY_DIR}" >&2
    return 1
  }

  # bibit-intent 是独立服务：它的单元由 bibit-intent 包自己安装并全局启用，
  # bibit-intent.service 再 Wants 自己的 models 单元。这里不 daemon-reload/
  # enable/start，也不绑定对方的内部单元名（旧版曾硬编码
  # bibit-intent-embed/llm.service，上游改造后直接报 unit does not exist），
  # 只探一次它对外的 /healthz 作状态汇报；未就绪不影响 Runtime 启动。
  local payload
  payload="$(curl --fail --silent --max-time 2 \
    "${BIBIT_INTENT_BASE_URL%/}/healthz" 2>/dev/null)" || payload=""
  if [[ "${payload}" == *'"status":"ok"'* \
    && "${payload}" == *'"registered_tools":'* ]]; then
    echo "bibit-intent 已就绪：${BIBIT_INTENT_BASE_URL%/}/healthz" >&2
  else
    echo "bibit-intent 暂未就绪：${BIBIT_INTENT_BASE_URL%/}/healthz" >&2
    echo "它由 bibit-intent 包自己的 user 单元管理，可执行 systemctl --user status bibit-intent.service 查看。" >&2
  fi
}

fix_model_record_permissions() {
  # Git 只保留可执行位，私有模型记录在全新 clone 里会变成组/全局可读，被 Runtime 拒绝。
  local model_root="${project_root}/.bianbu-agent/models" model_record
  [[ -d "${model_root}" ]] || return 0
  while IFS= read -r -d '' model_record; do
    chmod 600 -- "${model_record}"
  done < <(find "${model_root}" -type f \( -name '*.yaml' -o -name '*.lock' \) -print0)
}

install_all_packages
ensure_uv
join_service_groups
start_services
"${project_root}/scripts/ensure-searxng.sh"
fix_model_record_permissions
echo "开发环境已就绪：启动 Runtime 用 ./run.sh。" >&2
