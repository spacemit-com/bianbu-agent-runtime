#!/usr/bin/env bash
set -euo pipefail

runtime_root="${BIANBU_RUNTIME_ROOT:-/home/bianbu/bianbu-agent-runtime-agent-memory}"
runtime_config="${BIANBU_RUNTIME_CONFIG:-$runtime_root/config/development.yaml}"
state_dir="${BIANBU_RUNTIME_STATE:-/home/bianbu/.local/state/bianbu-agent-runtime-agent-memory}"
socket_path="${BIANBU_AGENT_MEMORY_SOCKET:-/home/bianbu/.local/state/bianbu-agent-memory-manual/agent-memoryd.sock}"
dplus_url="${BIANBU_MEMORY_DPLUS_URL:-http://127.0.0.1:18920}"
runtime_log="$state_dir/runtime.log"
runtime_pid_file="$state_dir/runtime.pid"
confirm_token_file="${BIANBU_MEMORY_CONFIRM_TOKEN_FILE:-$state_dir/confirm-token}"
port="9899"
memory_account_id="${BIANBU_AGENT_MEMORY_ACCOUNT_ID:-default}"
memory_agent_name="${BIANBU_AGENT_MEMORY_AGENT_NAME:-default-agent}"

export BIANBU_SYSTEM_RESOURCE_ROOT="${BIANBU_SYSTEM_RESOURCE_ROOT:-$runtime_root/resources}"

mkdir -p "$state_dir"
chmod 700 "$state_dir"

[[ -x "$runtime_root/.venv/bin/bianbu-agent" ]] || {
  echo "Runtime is not synchronized: $runtime_root/.venv" >&2
  exit 1
}
[[ -f "$runtime_config" ]] || {
  echo "Runtime config was not found: $runtime_config" >&2
  exit 1
}
[[ -S "$socket_path" ]] || {
  echo "AgentMemory socket was not found: $socket_path" >&2
  exit 1
}
curl -fsS --max-time 5 "$dplus_url/health" >/dev/null
if [[ -z "${BIANBU_MEMORY_CONFIRM_TOKEN:-}" ]]; then
  [[ -f "$confirm_token_file" && -r "$confirm_token_file" && -s "$confirm_token_file" ]] || {
    echo "AgentMemory confirmation token file is missing, unreadable, or empty: $confirm_token_file" >&2
    exit 1
  }
fi

if [[ -s "$runtime_pid_file" ]]; then
  old_pid="$(cat "$runtime_pid_file")"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    command_line="$(tr '\0' ' ' <"/proc/$old_pid/cmdline")"
    if [[ "$command_line" != *"$runtime_root"* && "$command_line" != *"$runtime_config"* ]]; then
      echo "Refusing to stop unrelated process: pid=$old_pid" >&2
      exit 1
    fi
    kill -TERM "$old_pid"
    for _ in $(seq 1 200); do
      kill -0 "$old_pid" 2>/dev/null || break
      sleep 0.05
    done
    kill -0 "$old_pid" 2>/dev/null && {
      echo "Runtime did not stop: pid=$old_pid" >&2
      exit 1
    }
  fi
fi

nohup env \
  BIANBU_AGENT_MEMORY_ENABLED="${BIANBU_AGENT_MEMORY_ENABLED:-true}" \
  BIANBU_AGENT_MEMORY_ACCOUNT_ID="$memory_account_id" \
  BIANBU_AGENT_MEMORY_AGENT_NAME="$memory_agent_name" \
  BIANBU_AGENT_MEMORY_SOCKET="$socket_path" \
  BIANBU_MEMORY_CONFIRM_TOKEN_FILE="$confirm_token_file" \
  BIANBU_MEMORY_ENGINE="${BIANBU_MEMORY_ENGINE:-dplus}" \
  BIANBU_MEMORY_DPLUS_URL="$dplus_url" \
  BIANBU_MEMORY_DPLUS_SEARCH_TIMEOUT="${BIANBU_MEMORY_DPLUS_SEARCH_TIMEOUT:-1.2}" \
  "$runtime_root/.venv/bin/bianbu-agent" \
    --config "$runtime_config" serve \
  >>"$runtime_log" 2>&1 </dev/null &
runtime_pid=$!
printf '%s\n' "$runtime_pid" >"$runtime_pid_file"

for _ in $(seq 1 300); do
  listener_pid="$(
    ss -ltnp "sport = :$port" 2>/dev/null \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
      | head -n 1
  )"
  if kill -0 "$runtime_pid" 2>/dev/null \
    && [[ "$listener_pid" == "$runtime_pid" ]] \
    && curl -fsS --max-time 2 "http://127.0.0.1:$port/chat" >/dev/null 2>&1; then
    cat <<EOF
K3_AGENT_MEMORY_RUNTIME_READY
Web:     http://10.0.90.237:$port/chat
Runtime: $runtime_pid
Backend: ${BIANBU_AGENT_MEMORY_ENABLED:-true}
Socket:  $socket_path
Log:     $runtime_log
EOF
    exit 0
  fi
  if ! kill -0 "$runtime_pid" 2>/dev/null; then
    tail -n 100 "$runtime_log"
    exit 1
  fi
  sleep 0.1
done

tail -n 100 "$runtime_log"
echo "Runtime did not become ready: pid=$runtime_pid" >&2
exit 1
