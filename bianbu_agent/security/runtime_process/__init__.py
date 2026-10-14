"""双进程 Agent Runtime 的控制面、Worker 与通信契约。"""

from bianbu_agent.security.runtime_process.protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    decode_message,
    encode_message,
)

__all__ = ["PROTOCOL_VERSION", "ProtocolError", "decode_message", "encode_message"]
