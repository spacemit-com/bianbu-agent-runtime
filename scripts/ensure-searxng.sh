#!/usr/bin/env bash
set -euo pipefail

os_release_file="${BIANBU_AGENT_OS_RELEASE_FILE:-/etc/os-release}"
health_url="http://127.0.0.1:8888/healthz"

if [[ ! -r "${os_release_file}" ]]; then
  exit 0
fi

ID=""
NAME=""
# shellcheck disable=SC1090
source "${os_release_file}"

if [[ "${ID:-}" != "bianbu" && "${NAME:-}" != *BianBu* ]]; then
  exit 0
fi

architecture="$(dpkg --print-architecture)"
if [[ "${architecture}" != "riscv64" ]]; then
  echo "Bianbu 自动安装 SearXNG 仅支持 riscv64，当前架构：${architecture}" >&2
  exit 1
fi

searxng_is_healthy() {
  curl --fail --silent --max-time 2 "${health_url}" >/dev/null 2>&1
}

if searxng_is_healthy; then
  echo "SearXNG 已就绪：${health_url}"
  exit 0
fi

package_status="$(dpkg-query -W -f='${Status}' searxng 2>/dev/null || true)"
if [[ "${package_status}" == "install ok installed" ]]; then
  echo "SearXNG 已安装，正在启动服务..."
  sudo systemctl enable --now searxng
else
  echo "正在从 Bianbu 软件源安装 SearXNG..."
  sudo apt install -y searxng
fi

for _attempt in {1..10}; do
  if searxng_is_healthy; then
    echo "SearXNG 已就绪：${health_url}"
    exit 0
  fi
  sleep 1
done

echo "SearXNG 启动后仍不可用，请运行：systemctl status searxng" >&2
exit 1
