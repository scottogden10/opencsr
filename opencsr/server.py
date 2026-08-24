"""OpenCSR review workbench — stdlib-only localhost server.

    python3 serve.py                 # mock backend (offline)
    python3 serve.py --backend managed   # Claude Managed Agents (needs API key)

No dependencies: ThreadingHTTPServer + a single-page UI. The UI drives the
same workflow, governance, eval, and hill-climb code paths as run_demo.py.
"""

from __future__ import annotations

import json
import socketserver
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), which can hang for
        # many seconds on hosts with slow reverse DNS. Skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

from .agents.backend import get_backend
from .evals import run_eval_corpus
from .governance import simulate_review
from .hillclimb import run_hillclimb
from .skills import list_skills, reset_skills, load_skill
from .store import Store
from .workflow import run_efficacy_task, run_narrative_task, apply_decision

UI_PATH = Path(__file__).resolve().parent / "ui" / "index.html"
FAULTS = ["wrong_denominator", "stale_cutoff", "population_mismatch",
          "swapped_arms", "ci_copied", "prompt_injection"]

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _start_job(kind: str, fn) -> str:
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "kind": kind, "status": "running",
                         "result": None, "error": None}

    def run():
        try:
            result = fn()
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = result
        except Exception as e:  # noqa: BLE001 — surface to UI
            traceback.print_exc()
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return job_id


class App:
    def __init__(self, db_path: str, backend_name: str):
        self.store = Store(db_path)
        self.backend_name = backend_name
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            self._backend = get_backend(self.backend_name)
        return self._backend

    # ------------------------------ views ------------------------------ #
    def state(self) -> dict:
        docs = {}
        for node_row in self.store._rows(  # noqa: SLF001
                "SELECT DISTINCT node FROM documents"):
            node = node_row["node"]
            docs[node] = self.store.document_history(node)
        with _jobs_lock:
            jobs = list(_jobs.values())
        return {
            "backend": self.backend_name,
            "faults": FAULTS,
            "tasks": self.store.list_tasks(),
            "evals": self.store.list_evals(),
            "skills": [{"name": s["name"], "version": s["version"],
                        "description": s["description"]}
                       for s in list_skills()],
            "skill_events": self.store.skill_history(),
            "documents": docs,
            "jobs": jobs,
            "audit": self.store.audit_events(limit=120),
        }

    def task_detail(self, task_id: str) -> dict:
        return {
            "task": self.store.get_task(task_id),
            "runs": self.store.by_task("runs", task_id),
            "evidence": self.store.by_task("evidence", task_id),
            "claims": self.store.by_task("claims", task_id),
            "proposals": self.store.by_task("proposals", task_id),
            "issues": self.store.by_task("issues", task_id),
            "feedback": self.store.by_task("feedback", task_id),
            "receipts": self.store.by_task("receipts", task_id),
            "audit": self.store.audit_events(task_id, limit=200),
        }

    # ------------------------------ actions ---------------------------- #
    def run_task(self, body: dict) -> dict:
        ttype = body.get("type", "efficacy")
        fault = body.get("fault_id") or None
        if fault is not None and fault not in FAULTS:
            return {"error": f"unknown fault {fault}"}
        if ttype == "efficacy":
            job = _start_job("task", lambda: run_efficacy_task(
                self.store, self.backend, fault_id=fault,
                title=body.get("title")))
        elif ttype == "narrative":
            job = _start_job("task", lambda: run_narrative_task(
                self.store, self.backend))
        else:
            return {"error": f"unknown task type {ttype}"}
        return {"job_id": job}

    def review(self, body: dict) -> dict:
        return apply_decision(
            self.store, body["task_id"], body["decision"],
            body.get("reason_code", "other"), body.get("scope", "this_instance"),
            body.get("comment", ""), body.get("final_text") or None,
            body.get("actor", "human:reviewer"))

    def sim_review(self, body: dict) -> dict:
        return simulate_review(self.store, body["task_id"])

    def run_evals(self, body: dict) -> dict:
        label = body.get("label", "manual")
        job = _start_job("eval", lambda: run_eval_corpus(
            self.store, self.backend, label=label))
        return {"job_id": job}

    def run_climb(self, body: dict) -> dict:
        rounds = int(body.get("rounds", 3))
        job = _start_job("hillclimb", lambda: run_hillclimb(
            self.store, self.backend, max_rounds=rounds))
        return {"job_id": job}

    def skill_detail(self, name: str) -> dict:
        s = load_skill(name)
        versions_dir = Path(__file__).resolve().parent.parent / "skills" / name / "versions"
        versions = []
        if versions_dir.exists():
            for p in sorted(versions_dir.glob("v*.md")):
                versions.append({"file": p.name, "content": p.read_text()})
        return {"current": s, "versions": versions}

    def reset(self, body: dict) -> dict:
        reset_skills()
        for table in ("tasks", "runs", "evidence", "claims", "proposals",
                      "issues", "feedback", "receipts", "audit", "documents",
                      "evals", "skill_events"):
            self.store._exec(f"DELETE FROM {table}")  # noqa: SLF001
        with _jobs_lock:
            _jobs.clear()
        self.store.audit("demo.reset", None, {"actor": "human"}, {})
        return {"status": "reset", "skills": [
            {"name": s["name"], "version": s["version"]} for s in list_skills()]}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload, content_type="application/json"):
            body = (payload if isinstance(payload, bytes)
                    else json.dumps(payload, default=str).encode())
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # quiet
            pass

        def do_GET(self):  # noqa: N802
            try:
                if self.path in ("/", "/index.html"):
                    self._send(200, UI_PATH.read_bytes(),
                               "text/html; charset=utf-8")
                elif self.path == "/api/state":
                    self._send(200, app.state())
                elif self.path.startswith("/api/tasks/"):
                    self._send(200, app.task_detail(self.path.split("/")[-1]))
                elif self.path.startswith("/api/skills/"):
                    self._send(200, app.skill_detail(self.path.split("/")[-1]))
                else:
                    self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": str(e)})

        def do_POST(self):  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                routes = {
                    "/api/tasks": app.run_task,
                    "/api/review": app.review,
                    "/api/simulate_review": app.sim_review,
                    "/api/evals/run": app.run_evals,
                    "/api/hillclimb/run": app.run_climb,
                    "/api/reset": app.reset,
                }
                fn = routes.get(self.path)
                if fn is None:
                    self._send(404, {"error": "not found"})
                    return
                self._send(200, fn(body))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": str(e)})

    return Handler


def serve(db_path: str = "opencsr.db", backend: str = "mock",
          host: str = "127.0.0.1", port: int = 8734):
    app = App(db_path, backend)
    httpd = _Server((host, port), make_handler(app))
    print(f"OpenCSR workbench: http://{host}:{port}  "
          f"(backend: {backend}, ledger: {db_path})")
    httpd.serve_forever()
