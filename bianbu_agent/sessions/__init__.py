"""Public contracts loaded independently from persistence implementations."""

from importlib import import_module

_EXPORTS = {
    "SessionAgentMismatchError": "bianbu_agent.sessions.contracts",
    "SessionHandle": "bianbu_agent.sessions.contracts",
    "SessionStore": "bianbu_agent.sessions.contracts",
    "SessionEvent": "bianbu_agent.sessions.events",
    "session_event_for_inbox_claim": "bianbu_agent.sessions.events",
    "session_event_for_inbox_splice": "bianbu_agent.sessions.events",
    "session_event_for_run_event": "bianbu_agent.sessions.events",
    "session_event_for_run_result": "bianbu_agent.sessions.events",
    "session_event_for_run_start": "bianbu_agent.sessions.events",
    "session_event_to_dict": "bianbu_agent.sessions.events",
    "SessionInboxMessage": "bianbu_agent.sessions.inbox",
    "SessionInboxSnapshot": "bianbu_agent.sessions.inbox",
    "SessionManager": "bianbu_agent.sessions.manager",
    "SessionLogStore": "bianbu_agent.sessions.log_store",
    "StoredSession": "bianbu_agent.sessions.models",
    "JSONSessionStore": "bianbu_agent.sessions.stores",
    "SQLiteSessionStore": "bianbu_agent.sessions.stores",
}


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = [
    "JSONSessionStore",
    "SQLiteSessionStore",
    "SessionAgentMismatchError",
    "SessionEvent",
    "SessionHandle",
    "SessionInboxMessage",
    "SessionInboxSnapshot",
    "SessionManager",
    "SessionLogStore",
    "SessionStore",
    "StoredSession",
    "session_event_for_run_event",
    "session_event_for_inbox_claim",
    "session_event_for_inbox_splice",
    "session_event_for_run_result",
    "session_event_for_run_start",
    "session_event_to_dict",
]
