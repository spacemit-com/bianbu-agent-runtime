#!/usr/bin/env bash
set -euo pipefail

runtime_root="${BIANBU_RUNTIME_ROOT:-/home/bianbu/bianbu-agent-runtime-agent-memory}"
memory_root="${BIANBU_MEMORY_ROOT:-/home/bianbu/bianbu-agent-memory}"
runtime_config="${BIANBU_RUNTIME_CONFIG:-$runtime_root/config/development.yaml}"
state_dir="${BIANBU_RUNTIME_STATE:-/home/bianbu/.local/state/bianbu-agent-memory-runtime-9899}"
socket_path="$state_dir/agent-memoryd.sock"
database_path="$state_dir/memory.db"
key_package_path="$state_dir/memory.keypkg"
account_secret_file="$state_dir/account-secret"
confirm_token_file="$state_dir/confirm-token"
memory_pid_file="$state_dir/memoryd.pid"
dplus_pid_file="$state_dir/dplus.pid"
runtime_pid_file="$state_dir/runtime.pid"
memory_log="$state_dir/memoryd.log"
dplus_log="$state_dir/dplus.log"
runtime_log="$state_dir/runtime.log"
dplus_port="18921"
runtime_port="9899"
memory_account_id="${BIANBU_AGENT_MEMORY_ACCOUNT_ID:-default}"
memory_agent_name="${BIANBU_AGENT_MEMORY_AGENT_NAME:-default-agent}"
dplus_python="${BIANBU_MEMORY_DPLUS_PYTHON:-/home/bianbu/mem0-venv/bin/python}"
dplus_runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bianbu-agent-memory-dplus-9899"

export BIANBU_SYSTEM_RESOURCE_ROOT="${BIANBU_SYSTEM_RESOURCE_ROOT:-$runtime_root/resources}"

mkdir -p "$state_dir"
chmod 700 "$state_dir"
expected_dplus_runtime_dir="/run/user/$(id -u)/bianbu-agent-memory-dplus-9899"
[[ "$dplus_runtime_dir" == "$expected_dplus_runtime_dir" ]] || {
  echo "Unsafe D+ runtime directory: $dplus_runtime_dir" >&2
  exit 1
}
rm -rf "$dplus_runtime_dir"
mkdir -p "$dplus_runtime_dir"
chmod 700 "$dplus_runtime_dir"
for secret in "$account_secret_file" "$confirm_token_file"; do
  if [[ ! -s "$secret" ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$secret"
    chmod 600 "$secret"
  fi
done

(cd "$memory_root" && cargo build --workspace --release)
runtime_python="$runtime_root/.venv/bin/python"
uv_command="$(command -v uv 2>/dev/null || true)"
if [[ -z "$uv_command" && -x "$HOME/.local/bin/uv" ]]; then
  uv_command="$HOME/.local/bin/uv"
fi
if "$runtime_python" -m pip --version >/dev/null 2>&1; then
  "$runtime_python" -m pip install -q -e "$memory_root/sdk/python"
elif [[ -n "$uv_command" ]]; then
  "$uv_command" pip install --python "$runtime_python" -q -e "$memory_root/sdk/python"
else
  echo "Neither pip nor uv is available to install the AgentMemory SDK" >&2
  exit 1
fi

stop_owned() {
  local pid_file="$1" expected="$2"
  [[ -s "$pid_file" ]] || return 0
  local pid command_line
  pid="$(cat "$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
  [[ "$command_line" == *"$expected"* ]] || {
    echo "Refusing to stop unrelated process: pid=$pid expected=$expected" >&2
    exit 1
  }
  kill -TERM "$pid"
  for _ in $(seq 1 200); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.05
  done
  echo "Process did not stop: pid=$pid" >&2
  exit 1
}

stop_owned_listener() {
  local port="$1" expected="$2" listener_pid command_line
  listener_pid="$(ss -ltnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1)"
  [[ -n "$listener_pid" ]] || return 0
  command_line="$(tr '\0' ' ' <"/proc/$listener_pid/cmdline")"
  [[ "$command_line" == *"$expected"* ]] || {
    echo "Refusing to stop unrelated listener: port=$port pid=$listener_pid expected=$expected" >&2
    exit 1
  }
  printf '%s\n' "$listener_pid" >"$runtime_pid_file"
  stop_owned "$runtime_pid_file" "$expected"
}

stop_owned "$runtime_pid_file" "$runtime_config"
stop_owned_listener "$runtime_port" "$runtime_config"
stop_owned "$dplus_pid_file" "bianbu_agent_sdk.memory.dplus_service"
stop_owned "$memory_pid_file" "$socket_path"

migration_arguments=()
if [[ -f "$database_path" && ! -f "$key_package_path" ]]; then
  migration_arguments+=(--migrate-plaintext)
fi
if [[ ! -f "$key_package_path" ]]; then
  migration_arguments+=(--initialize-key-package)
fi
nohup env BIANBU_MEMORY_DEV_CONFIRM_TOKEN="$(cat "$confirm_token_file")" \
  "$memory_root/target/release/bianbu-agent-memoryd" \
    --development-identity \
    --socket "$socket_path" \
    --database "$database_path" \
    --key-package "$key_package_path" \
    --account-secret-file "$account_secret_file" \
    --development-account-id "$memory_account_id" \
    --development-agent-name "$memory_agent_name" \
    "${migration_arguments[@]}" \
  >>"$memory_log" 2>&1 </dev/null &
memory_pid=$!
printf '%s\n' "$memory_pid" >"$memory_pid_file"
python3 - "$socket_path" "$memory_pid" <<'PY'
import os
import socket
import sys
import time

path = sys.argv[1]
pid = int(sys.argv[2])
for _ in range(400):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise SystemExit("memoryd exited before its socket became ready")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(path)
    except OSError:
        time.sleep(0.05)
    else:
        break
    finally:
        client.close()
else:
    raise SystemExit("memoryd socket did not become ready")
PY

nohup env \
  PYTHONPATH="$memory_root/sdk/python/src" \
  MEM0_TELEMETRY=false \
  BIANBU_AGENT_MEMORY_SOCKET="$socket_path" \
  BIANBU_MEMORY_DPLUS_STATE_DIR="$dplus_runtime_dir" \
  BIANBU_MEMORY_DPLUS_INFER_MODE="${BIANBU_MEMORY_DPLUS_INFER_MODE:-rules}" \
  BIANBU_MEMORY_DPLUS_EMBEDDER_BASE_URL="${BIANBU_MEMORY_DPLUS_EMBEDDER_BASE_URL:-http://127.0.0.1:18801/v1}" \
  "$dplus_python" -m bianbu_agent_sdk.memory.dplus_service \
    --socket "$socket_path" --port "$dplus_port" \
  >>"$dplus_log" 2>&1 </dev/null &
dplus_pid=$!
printf '%s\n' "$dplus_pid" >"$dplus_pid_file"
for _ in $(seq 1 600); do
  curl -fsS --max-time 2 "http://127.0.0.1:$dplus_port/health" >/dev/null 2>&1 && break
  kill -0 "$dplus_pid" 2>/dev/null || { tail -n 100 "$dplus_log"; exit 1; }
  sleep 0.1
done
curl -fsS --max-time 5 "http://127.0.0.1:$dplus_port/health" >/dev/null
curl -fsS --max-time 120 \
  -H 'Content-Type: application/json' \
  -d '{}' \
  "http://127.0.0.1:$dplus_port/v1/clear" >/dev/null
curl -fsS --max-time 120 \
  -H 'Content-Type: application/json' \
  -d '{"limit":100}' \
  "http://127.0.0.1:$dplus_port/v1/rebuild" >/dev/null

nohup env \
  BIANBU_AGENT_MEMORY_ENABLED=true \
  BIANBU_AGENT_MEMORY_ACCOUNT_ID="$memory_account_id" \
  BIANBU_AGENT_MEMORY_AGENT_NAME="$memory_agent_name" \
  BIANBU_AGENT_MEMORY_SOCKET="$socket_path" \
  BIANBU_MEMORY_CONFIRM_TOKEN_FILE="$confirm_token_file" \
  BIANBU_MEMORY_ENGINE=dplus \
  BIANBU_MEMORY_DPLUS_URL="http://127.0.0.1:$dplus_port" \
  "$runtime_root/.venv/bin/bianbu-agent" --config "$runtime_config" serve \
  >>"$runtime_log" 2>&1 </dev/null &
runtime_pid=$!
printf '%s\n' "$runtime_pid" >"$runtime_pid_file"
for _ in $(seq 1 400); do
  listener_pid="$(ss -ltnp "sport = :$runtime_port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1)"
  if [[ "$listener_pid" == "$runtime_pid" ]] \
    && curl -fsS --max-time 2 "http://127.0.0.1:$runtime_port/chat" >/dev/null 2>&1; then
    cat <<EOF
K3_AGENT_MEMORY_STACK_READY
Web:        http://10.0.90.237:$runtime_port/chat
Runtime:    $runtime_pid
Memoryd:    $memory_pid
D+:         $dplus_pid (http://127.0.0.1:$dplus_port)
Socket:     $socket_path
Database:   $database_path (SQLCipher)
KeyPackage: $key_package_path
State:      $state_dir
EOF
    exit 0
  fi
  kill -0 "$runtime_pid" 2>/dev/null || { tail -n 100 "$runtime_log"; exit 1; }
  sleep 0.1
done
tail -n 100 "$runtime_log"
exit 1
