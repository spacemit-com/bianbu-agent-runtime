"""Run one security client call under the system Python D-Bus ABI."""
from __future__ import annotations
import json, os, sys

def _plain(value):
    if isinstance(value, dict): return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None: return value
    return str(value)

def main():
    request = json.loads(sys.argv[1])
    import bianbu_security_client as client
    # The helper is a short-lived CPython 3.12 process, so bind its actual
    # D-Bus caller before every request in local development.  Production
    # installations use the native in-process client and never set this flag.
    if os.environ.get("BIANBU_SECURITY_TEST_ADMIN") == "1":
        # Runtime calls use the stable security namespace identity, while the
        # first D-Bus argument is not always an agent id (e.g. lease calls).
        agent_id = os.environ.get("BIANBU_PA_AGENT_ID", "").strip()
        if agent_id and hasattr(client, "bind_agent"):
            client.bind_agent(agent_id)
    value = getattr(client, request["method"])(*request.get("args", ()), **request.get("kwargs", {}))
    print(json.dumps(_plain(value), ensure_ascii=False))

if __name__ == "__main__":
    main()
