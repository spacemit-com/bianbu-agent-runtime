#!/usr/bin/env bash
set -euo pipefail

bibit_root="${BIBIT_INTENT_ROOT:-/opt/bibit-intent}"
bibit_src="${BIBIT_INTENT_SRC:-${bibit_root}/src}"
base_url="${BIBIT_INTENT_BASE_URL:-http://127.0.0.1:8766}"
health_url="${base_url%/}/healthz"
host="${BIBIT_INTENT_HOST:-127.0.0.1}"
port="${BIBIT_INTENT_PORT:-8766}"
start_timeout="${BIBIT_INTENT_START_TIMEOUT:-15}"

if [[ -n "${BIBIT_INTENT_DATA_ROOT:-}" ]]; then
  data_root="${BIBIT_INTENT_DATA_ROOT}"
elif [[ -n "${XDG_DATA_HOME:-}" ]]; then
  data_root="${XDG_DATA_HOME}/bibit-intent"
elif [[ -n "${HOME:-}" ]]; then
  data_root="${HOME}/.local/share/bibit-intent"
else
  data_root="/tmp/bibit-intent-data-$(id -u)"
fi

if [[ ! -d "${bibit_src}/bibit_intent" ||
      ! -f "${bibit_root}/pyproject.toml" ]]; then
  echo "未找到 bibit-intent 源码：${bibit_root}。请先部署最新代码到该目录。" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "启动 Bibit Intent 需要 curl 进行健康检查。" >&2
  exit 127
fi

bibit_is_healthy() {
  local payload
  payload="$(curl --fail --silent --max-time 2 "${health_url}" 2>/dev/null)" || return 1
  [[ "${payload}" == *'"status":"ok"'* &&
     "${payload}" == *'"registered_tools":'* ]]
}

if bibit_is_healthy; then
  echo "Bibit Intent 已就绪：${health_url}"
  exit 0
fi

if [[ -n "${BIBIT_INTENT_STATE_DIR:-}" ]]; then
  state_dir="${BIBIT_INTENT_STATE_DIR}"
elif [[ -n "${XDG_STATE_HOME:-}" ]]; then
  state_dir="${XDG_STATE_HOME}/bibit-intent"
elif [[ -n "${HOME:-}" ]]; then
  state_dir="${HOME}/.local/state/bibit-intent"
else
  state_dir="/tmp/bibit-intent-$(id -u)"
fi
mkdir -p -- "${state_dir}"
chmod 700 -- "${state_dir}"
mkdir -p -- "${data_root}"
chmod 700 -- "${data_root}"
pid_file="${state_dir}/service.pid"
log_file="${state_dir}/service.log"

if [[ -r "${pid_file}" ]]; then
  previous_pid="$(<"${pid_file}")"
  if [[ "${previous_pid}" =~ ^[0-9]+$ ]] && kill -0 "${previous_pid}" 2>/dev/null; then
    echo "Bibit Intent 进程 ${previous_pid} 尚在启动，等待健康检查..."
  else
    rm -f -- "${pid_file}"
  fi
fi

if [[ ! -e "${pid_file}" ]]; then
  echo "正在启动 Bibit Intent：${base_url}"
  bibit_bin="${bibit_root}/.venv/bin/bibit-intent"
  bibit_python="${bibit_root}/.venv/bin/python"
  bibit_main="${bibit_root}/main.py"
  if [[ -x "${bibit_bin}" ]]; then
    start_command=("${bibit_bin}")
  elif [[ -x "${bibit_python}" && -f "${bibit_main}" ]]; then
    start_command=("${bibit_python}" "${bibit_main}")
  else
    if ! command -v uv >/dev/null 2>&1; then
      echo "启动 Bibit Intent 需要 uv、${bibit_bin} 或 ${bibit_python} ${bibit_main}。" >&2
      exit 127
    fi
    start_command=(uv run --project "${bibit_root}" --no-dev bibit-intent)
  fi
  nohup "${start_command[@]}" serve --data-root "${data_root}" \
    --host "${host}" --port "${port}" \
    >>"${log_file}" 2>&1 </dev/null &
  service_pid=$!
  printf '%s\n' "${service_pid}" >"${pid_file}"
fi

for ((attempt = 0; attempt < start_timeout * 10; attempt++)); do
  if bibit_is_healthy; then
    echo "Bibit Intent 已启动：${health_url}"
    exit 0
  fi
  sleep 0.1
done

echo "Bibit Intent 启动后仍不可用；日志：${log_file}" >&2
exit 1
