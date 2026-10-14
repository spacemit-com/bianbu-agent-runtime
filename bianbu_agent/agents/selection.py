"""Trusted task-local capability selection, never populated from user metadata."""
from contextvars import ContextVar
from bianbu_agent.agents.contracts import AgentSpec

task_agent: ContextVar[AgentSpec | None] = ContextVar('task_agent', default=None)
