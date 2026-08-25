"""Claude Managed Agents backend (beta).

The agent loop runs on Anthropic's orchestration layer; OpenCSR keeps the
data plane host-side. Each clinical agent is a persisted, versioned Agent
object whose ONLY tools are OpenCSR's custom clinical tools — no bash, no
filesystem, no web. Every tool call arrives as an `agent.custom_tool_use`
event and is executed by the governed ToolGateway in this process; the
sandbox never sees the trial data store.

Versioning is the improvement mechanism: agent system prompts are compiled
from skills, and when a skill is promoted the next run updates the agent
(`POST /v1/agents/{id}`), creating a new immutable version — with rollback
by pinning the prior version.

Requires:  pip install anthropic   and an ANTHROPIC_API_KEY (or `ant auth
login` profile). Sessions are kept (not archived) by default so you can
inspect the full trace in the Anthropic Console.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..domain import sha256_text
from ..tools import tool_defs_for
from .registry import AGENTS, build_agent_prompt

REGISTRY_PATH = Path(__file__).resolve().parent.parent.parent / ".opencsr" / "live_registry.json"
ENV_NAME = "opencsr-clinical-v1"


class ManagedBackend:
    name = "managed"

    def __init__(self, model: str | None = None, budget_usd: float = 2.5,
                 keep_sessions: bool = True, max_reconnects: int = 4,
                 verbose: bool = True):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "The live backend needs the Anthropic SDK: pip install anthropic"
            ) from e
        self._anthropic = anthropic
        # Key resolution: ANTHROPIC_API_KEY env var (or an `ant auth login`
        # profile) as usual; otherwise a local key file at
        # .opencsr/api_key (gitignored) so the key never has to be pasted
        # into a chat or shell history.
        api_key = None
        key_file = REGISTRY_PATH.parent / "api_key"
        if not os.environ.get("ANTHROPIC_API_KEY") and key_file.exists():
            api_key = key_file.read_text().strip() or None
        self.client = anthropic.Anthropic(api_key=api_key) if api_key \
            else anthropic.Anthropic()
        self.model = model or os.environ.get("OPENCSR_MODEL", "claude-opus-5")
        self.budget_usd = budget_usd
        self.keep_sessions = keep_sessions
        self.max_reconnects = max_reconnects
        self.verbose = verbose
        self._registry = self._load_registry()

    # ---------------------------- registry ------------------------------ #
    def _load_registry(self) -> dict:
        if REGISTRY_PATH.exists():
            return json.loads(REGISTRY_PATH.read_text())
        return {"environment_id": None, "agents": {}}

    def _save_registry(self):
        REGISTRY_PATH.parent.mkdir(exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(self._registry, indent=2))

    # ---------------------------- provisioning -------------------------- #
    def _ensure_environment(self) -> str:
        env_id = self._registry.get("environment_id")
        if env_id:
            return env_id
        try:
            env = self.client.beta.environments.create(
                name=ENV_NAME,
                config={"type": "cloud",
                        # deny-by-default egress: clinical tools are custom
                        # (host-side) and need no network from the sandbox
                        "networking": {"type": "limited"}})
            env_id = env.id
        except self._anthropic.APIStatusError as e:
            if e.status_code != 409:
                raise
            env_id = None
            envs = self.client.beta.environments.list()
            for it in envs:  # SDK auto-paginates the page object
                if getattr(it, "name", "") == ENV_NAME:
                    env_id = it.id
                    break
            if env_id is None:
                raise
        self._registry["environment_id"] = env_id
        self._save_registry()
        return env_id

    def _ensure_agent(self, agent_name: str, prompt: str) -> dict:
        """Create the agent once; update it (new immutable version) when the
        compiled prompt changes — e.g. after a skill promotion."""
        spec = AGENTS[agent_name]
        tools = tool_defs_for(sorted(spec["tools"]))
        config_hash = sha256_text(prompt + json.dumps(tools, sort_keys=True)
                                  + self.model)
        entry = self._registry["agents"].get(agent_name)
        if entry and entry.get("config_hash") == config_hash:
            return entry
        if entry:
            agent = self.client.beta.agents.update(
                entry["id"], name=f"opencsr-{agent_name}",
                model=self.model, system=prompt, tools=tools)
            if self.verbose:
                print(f"  [managed] updated agent {agent_name} -> "
                      f"version {agent.version}")
        else:
            agent = self.client.beta.agents.create(
                name=f"opencsr-{agent_name}", model=self.model,
                system=prompt, tools=tools,
                description=f"OpenCSR {spec['display']} (governed clinical "
                            f"tools only; synthetic data)")
            if self.verbose:
                print(f"  [managed] created agent {agent_name} "
                      f"({agent.id} v{agent.version})")
        entry = {"id": agent.id, "version": agent.version,
                 "config_hash": config_hash}
        self._registry["agents"][agent_name] = entry
        self._save_registry()
        return entry

    # ---------------------------- session loop -------------------------- #
    def _session_loop(self, agent_entry: dict, brief: str, handler,
                      title: str, max_tool_calls: int = 60) -> dict:
        env_id = self._ensure_environment()
        budget_cents = str(int(round(self.budget_usd * 100)))
        session = self.client.beta.sessions.create(
            agent={"type": "agent", "id": agent_entry["id"],
                   "version": agent_entry["version"]},
            environment_id=env_id,
            title=title,
            budget={"type": "limit",
                    "max_list_cost": {"amount": budget_cents,
                                      "currency": "USD"}},
            initial_events=[{"type": "user.message",
                             "content": [{"type": "text", "text": brief}]}])
        workspace = os.environ.get("OPENCSR_WORKSPACE", "default")
        if self.verbose:
            print(f"  [managed] session {session.id} — trace: "
                  f"https://platform.claude.com/workspaces/{workspace}/sessions/"
                  f"{session.id}")

        answered: set[str] = set()
        tool_results: dict[str, tuple] = {}  # ev_id -> (result, finished)
        state = {"finished": False, "calls": 0, "status": "completed",
                 "errors": []}

        def ev_field(ev, field, default=None):
            v = getattr(ev, field, default)
            if v is None and isinstance(ev, dict):
                v = ev.get(field, default)
            return v

        def process(ev) -> str:
            etype = ev_field(ev, "type", "")
            if etype == "agent.custom_tool_use":
                ev_id = ev_field(ev, "id")
                if ev_id in answered:
                    return "continue"
                # exactly-once tool execution, at-least-once delivery: the
                # host-side effect is cached by event id, and the id is only
                # marked answered AFTER the result send succeeds — a dropped
                # send is replayed on reconnect without re-running the tool.
                if ev_id not in tool_results:
                    name = ev_field(ev, "name", "")
                    raw = ev_field(ev, "input", {}) or {}
                    tool_input = dict(raw) if not isinstance(raw, dict) else raw
                    tool_results[ev_id] = handler(name, tool_input)
                    state["calls"] += 1
                result, finished = tool_results[ev_id]
                self.client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[{"type": "user.custom_tool_result",
                             "custom_tool_use_id": ev_id,
                             "content": [{"type": "text",
                                          "text": json.dumps(result,
                                                             default=str)}]}])
                answered.add(ev_id)
                if finished:
                    state["finished"] = True
                    self.client.beta.sessions.events.send(
                        session_id=session.id,
                        events=[{"type": "user.interrupt"}])
                elif state["calls"] >= max_tool_calls:
                    state["status"] = "budget_exceeded"
                    self.client.beta.sessions.events.send(
                        session_id=session.id,
                        events=[{"type": "user.interrupt"}])
            elif etype == "session.error":
                state["errors"].append(str(ev_field(ev, "error",
                                                    ev_field(ev, "message", ""))))
            elif etype == "session.status_terminated":
                return "stop"
            elif etype == "session.status_idle":
                sr = ev_field(ev, "stop_reason")
                sr_type = ev_field(sr, "type") if sr is not None else None
                if sr_type == "budget_reached":
                    state["status"] = "budget_exceeded"
                    return "stop"
                if sr_type != "requires_action":
                    return "stop"
            return "continue"

        done = False
        for _attempt in range(self.max_reconnects + 1):
            try:
                with self.client.beta.sessions.events.stream(
                        session_id=session.id) as stream:
                    # consolidation: history first, then live (dedupe by id;
                    # terminal checks run for already-seen events too)
                    seen: set[str] = set()
                    # iterate the page object itself: the SDK auto-paginates,
                    # while .data would silently stop at the first page
                    history = self.client.beta.sessions.events.list(
                        session_id=session.id)
                    for ev in history:
                        ev_id = ev_field(ev, "id")
                        if ev_id:
                            seen.add(ev_id)
                        if process(ev) == "stop":
                            done = True
                    if done:
                        break
                    for ev in stream:
                        ev_id = ev_field(ev, "id")
                        verdict = "continue"
                        if not ev_id or ev_id not in seen:
                            if ev_id:
                                seen.add(ev_id)
                            verdict = process(ev)
                        else:
                            etype = ev_field(ev, "type", "")
                            if etype in ("session.status_terminated",):
                                verdict = "stop"
                        if verdict == "stop":
                            done = True
                            break
                if done:
                    break
            except (self._anthropic.APIConnectionError,
                    self._anthropic.APIStatusError) as e:
                if self.verbose:
                    print(f"  [managed] stream dropped ({e}); reconnecting")
                time.sleep(1.5)
                continue

        usage = {}
        try:
            s = self.client.beta.sessions.retrieve(session_id=session.id)
            u = getattr(s, "usage", None)
            if u is not None:
                usage = json.loads(u.model_dump_json()) if hasattr(u, "model_dump_json") else dict(u)
        except Exception:  # noqa: BLE001 — usage is best-effort telemetry
            pass
        # persistent spend meter: one JSONL line per session, survives eval
        # throwaway ledgers, summed by the workbench header
        try:
            lc = (usage.get("list_cost") or {})
            REGISTRY_PATH.parent.mkdir(exist_ok=True)
            with open(REGISTRY_PATH.parent / "spend.jsonl", "a") as f:
                f.write(json.dumps({
                    "session_id": session.id, "title": title,
                    "model": self.model,
                    "list_cost_cents": int(lc.get("amount") or 0),
                    "tool_calls": state["calls"]}) + "\n")
        except Exception:  # noqa: BLE001
            pass

        if not self.keep_sessions:
            for _ in range(10):
                try:
                    s = self.client.beta.sessions.retrieve(session_id=session.id)
                    if getattr(s, "status", "") != "running":
                        self.client.beta.sessions.archive(session_id=session.id)
                        break
                except Exception:  # noqa: BLE001
                    break
                time.sleep(0.3)

        return {"session_id": session.id, "tool_calls": state["calls"],
                "finished": state["finished"], "status": state["status"],
                "errors": state["errors"], "usage": usage}

    # ---------------------------- Backend API --------------------------- #
    def run_agent(self, agent_name, prompt, brief, gateway, config):
        entry = self._ensure_agent(agent_name, prompt)

        def handler(name, tool_input):
            result = gateway.call(name, tool_input)
            return result, gateway.finished

        from .registry import TOOL_BUDGETS
        budgets = (config or {}).get("budgets", {})
        cap = min(budgets.get("max_tool_calls", 60),
                  TOOL_BUDGETS.get(agent_name, 40))
        loop = self._session_loop(
            entry, brief, handler,
            title=f"opencsr {agent_name}: {gateway.task_id}",
            max_tool_calls=cap)
        status = "completed" if gateway.finished else loop["status"]
        if status == "completed" and not gateway.finished:
            status = "failed"
        return {"status": status,
                "notes": f"managed session {loop['session_id']}; "
                         f"errors={loop['errors'][:2]}",
                "usage": loop["usage"] | {"tool_calls": loop["tool_calls"]}}

    def run_skillsmith(self, prompt, signals):
        full_prompt, _ = build_agent_prompt("skillsmith")
        entry = self._ensure_agent("skillsmith", full_prompt)
        out: dict = {}

        def handler(name, tool_input):
            if name == "submit_report":
                out["findings"] = tool_input.get("findings") or []
                return {"status": "report_recorded"}, True
            return {"error": "only submit_report is granted"}, False

        brief = (
            "Analyze these improvement signals and propose candidate skill "
            "rule additions.\n\nSIGNALS:\n" + json.dumps(signals, indent=1,
                                                         default=str)
            + "\n\nKnown skills: csr-efficacy-reporting, numeric-conventions, "
              "patient-narrative. Each finding must be an object with keys: "
              "skill, rule_marker (like RULE:SOME_NAME), rule_text (a markdown "
              "bullet ending with the marker), hypothesis {problem, change, "
              "predicted_effect}. Propose at most 4, only for problems the "
              "signals actually show.")
        self._session_loop(entry, brief, handler, title="opencsr skillsmith")
        candidates = []
        for f in out.get("findings", []):
            if not isinstance(f, dict):
                continue
            if {"skill", "rule_marker", "rule_text"} <= set(f):
                f.setdefault("hypothesis", {"problem": "", "change": "",
                                            "predicted_effect": ""})
                candidates.append(f)
        return candidates
