"""Agent execution backends.

The clinical workflow is backend-agnostic: it hands each agent a compiled
prompt, a task brief, and a capability-scoped ToolGateway, and expects the
agent to finish by calling its submit_* tool.

Backends:
  * MockBackend (agents/mock.py)  — deterministic, offline, zero deps.
  * ManagedBackend (agents/live.py) — Claude Managed Agents (beta): the
    agent loop runs on Anthropic's orchestration layer; every tool call
    comes back to this process as an `agent.custom_tool_use` event and is
    executed host-side by the same gateway.
"""

from __future__ import annotations

from typing import Protocol

from ..tools import ToolGateway


class Backend(Protocol):
    name: str

    def run_agent(self, agent_name: str, prompt: str, brief: str,
                  gateway: ToolGateway, config: dict) -> dict:
        """Drive one agent until it finishes (gateway.finished) or budgets
        run out. Returns {status, notes, usage}."""
        ...

    def run_skillsmith(self, prompt: str, signals: dict) -> list[dict]:
        """Return candidate skill changes:
        [{skill, rule_marker, rule_text, hypothesis:{problem, change,
          predicted_effect}}]"""
        ...


def get_backend(name: str, **kwargs):
    if name == "mock":
        from .mock import MockBackend
        return MockBackend()
    if name == "managed":
        from .live import ManagedBackend
        return ManagedBackend(**kwargs)
    raise ValueError(f"unknown backend '{name}' (expected 'mock' or 'managed')")
