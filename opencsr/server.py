"""OpenCSR review workbench — stdlib-only localhost server.

    python3 serve.py                 # mock backend (offline)
    python3 serve.py --backend managed   # Claude Managed Agents (needs API key)

No dependencies: ThreadingHTTPServer + a single-page UI. The UI drives the
same workflow, governance, eval, and hill-climb code paths as run_demo.py.
"""

from __future__ import annotations

import json
import os
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
from .csrdoc import build_csr_view, narrative_subjects
from .evals import run_eval_corpus
from .governance import simulate_review
from .hillclimb import run_hillclimb
from .skills import list_skills, reset_skills, load_skill
from .sources import Snapshot
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
        self.snapshot = Snapshot()  # clean snapshot for views (read-only)
        # Jobs live in this process's memory: any task still marked running
        # in the ledger at boot was orphaned by a restart (deploy, crash,
        # free-tier spin-down). Close it out so the UI never shows a zombie
        # "agents drafting" forever.
        for t in self.store.list_tasks(limit=500):
            if t["status"] in ("created", "running"):
                self.store.update_task(
                    t["id"], status="failed",
                    result={"error": "orphaned by a server restart — "
                                     "re-run the task"})
                self.store.audit("task.failed", t["id"], {"actor": "runtime"},
                                 {"reason": "orphaned by restart"})

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
        spend_cents = 0
        spend_file = Path(__file__).resolve().parent.parent / ".opencsr" / "spend.jsonl"
        if spend_file.exists():
            for line in spend_file.read_text().splitlines():
                try:
                    spend_cents += int(json.loads(line).get("list_cost_cents", 0))
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
        return {
            "backend": self.backend_name,
            "faults": FAULTS,
            "live_spend_usd": round(spend_cents / 100, 2),
            "csr": build_csr_view(self.store, self.snapshot),
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
    def _live_guard(self) -> dict | None:
        """For shared live deployments: refuse new agent work past the demo
        spend ceiling, and run one live job at a time so spend is serial
        and visible. Mock mode is never guarded."""
        if self.backend_name != "managed":
            return None
        limit = float(os.environ.get("OPENCSR_SPEND_LIMIT_USD", "25"))
        spent = self.state()["live_spend_usd"]
        if spent >= limit:
            return {"error": f"demo spend ceiling reached (${spent:.2f} of "
                             f"${limit:.2f} list). The host can raise "
                             f"OPENCSR_SPEND_LIMIT_USD."}
        with _jobs_lock:
            running = [j for j in _jobs.values() if j["status"] == "running"]
        if running:
            return {"error": "another agent job is already running — live "
                             "work is serialized so spend stays visible; "
                             "try again when it finishes"}
        return None

    def _node_busy(self, node: str) -> str | None:
        for t in self.store.list_tasks(limit=200):
            if (t.get("target_node") == node
                    and t["status"] in ("created", "running", "pending_review")):
                return t["id"]
        return None

    def run_task(self, body: dict) -> dict:
        guard = self._live_guard()
        if guard:
            return guard
        ttype = body.get("type", "efficacy")
        fault = body.get("fault_id") or None
        if fault is not None and fault not in FAULTS:
            return {"error": f"unknown fault {fault}"}
        # one active task per document node — duplicates create stale
        # proposals that can never be decided
        if ttype == "efficacy" and not fault:
            busy = self._node_busy("csr/11.4.1")
            if busy:
                return {"error": f"§11.4.1 already has an active task ({busy}); "
                                 f"decide it first"}
        if ttype == "narrative":
            from .workflow import find_narrative_subject
            usubjid_chk = body.get("usubjid") or find_narrative_subject(self.snapshot)
            busy = self._node_busy(f"csr/12.3.2/{usubjid_chk}")
            if busy:
                return {"error": f"a narrative task for {usubjid_chk} is "
                                 f"already active ({busy}); decide it first"}
        if ttype == "efficacy":
            job = _start_job("task", lambda: run_efficacy_task(
                self.store, self.backend, fault_id=fault,
                title=body.get("title")))
        elif ttype == "narrative":
            usubjid = body.get("usubjid") or None
            if usubjid is not None:
                known = {s["usubjid"] for s in narrative_subjects(self.snapshot)}
                if usubjid not in known:
                    return {"error": f"no narrative is required for {usubjid}"}
            job = _start_job("task", lambda: run_narrative_task(
                self.store, self.backend, usubjid=usubjid))
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
        task = self.store.get_task(body["task_id"]) or {}
        persona = ("sim:pv_reviewer"
                   if task.get("task_type") == "csr.narrative.draft"
                   else "sim:medical_writer")
        return simulate_review(self.store, body["task_id"], persona=persona)

    def run_evals(self, body: dict) -> dict:
        guard = self._live_guard()
        if guard:
            return guard
        label = body.get("label", "manual")
        job = _start_job("eval", lambda: run_eval_corpus(
            self.store, self.backend, label=label))
        return {"job_id": job}

    def run_climb(self, body: dict) -> dict:
        guard = self._live_guard()
        if guard:
            return guard
        rounds = int(body.get("rounds", 3))
        cases = body.get("cases") or None
        if cases is not None:
            valid = {"gold_efficacy", "narrative", "capability_probe"} | {
                f"fault:{f}" for f in FAULTS}
            cases = [c for c in cases if c in valid]
            if not cases:
                return {"error": "no valid cases in scope"}
        margin = float(body.get(
            "min_improvement", 1.0 if self.backend_name == "managed" else 0.0))
        job = _start_job("hillclimb", lambda: run_hillclimb(
            self.store, self.backend, max_rounds=rounds, cases=cases,
            min_improvement=margin))
        return {"job_id": job}

    def skill_detail(self, name: str) -> dict:
        if name not in {s["name"] for s in list_skills()}:
            return {"error": f"unknown skill {name!r}"}
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


def make_handler(app: App, allowed_hosts: set[str]):
    access_code = os.environ.get("OPENCSR_ACCESS_CODE", "")

    class Handler(BaseHTTPRequestHandler):
        def _authed(self):
            """Shared-link gate: when OPENCSR_ACCESS_CODE is set, visitors
            need the code once (?code=...) and get a cookie. Returns True,
            False, or 'grant' (code just supplied — set the cookie)."""
            if not access_code:
                return True
            if f"opencsr_code={access_code}" in self.headers.get("Cookie", ""):
                return True
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            if q.get("code", [None])[0] == access_code:
                return "grant"
            return False

        def _host_ok(self) -> bool:
            # Host allowlist defeats DNS rebinding: a hostile domain that
            # resolves to 127.0.0.1 still sends its own Host header.
            if "*" in allowed_hosts:
                return True
            h = self.headers.get("Host", "").lower()
            return h in allowed_hosts or h.split(":")[0] in allowed_hosts

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
                from urllib.parse import urlparse
                path = urlparse(self.path).path
                if path == "/healthz":   # host health checks, pre-auth
                    self._send(200, {"ok": True})
                    return
                if not self._host_ok():
                    self._send(403, {"error": "unrecognized Host header"})
                    return
                auth = self._authed()
                if auth == "grant":
                    body = b"granted"
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie",
                                     f"opencsr_code={os.environ['OPENCSR_ACCESS_CODE']}"
                                     f"; Path=/; HttpOnly; SameSite=Lax")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not auth:
                    self._send(401, (
                        b"<meta charset='utf-8'><body style='font-family:sans-serif;"
                        b"background:#10171a;color:#e6ecec;display:grid;place-items:center;"
                        b"height:100vh'><form><h2>OpenCSR demo</h2>"
                        b"<p>Enter the access code you were given:</p>"
                        b"<input name='code' autofocus> <button>Enter</button>"
                        b"</form></body>"), "text/html; charset=utf-8")
                    return
                if path in ("/", "/index.html"):
                    self._send(200, UI_PATH.read_bytes(),
                               "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._send(200, app.state())
                elif path.startswith("/api/tasks/"):
                    self._send(200, app.task_detail(path.split("/")[-1]))
                elif path.startswith("/api/tlf/"):
                    tid = path.split("/")[-1]
                    table = app.snapshot.tables.get(tid)
                    self._send(200 if table else 404,
                               table or {"error": f"unknown table {tid}"})
                elif path.startswith("/api/skills/"):
                    self._send(200, app.skill_detail(path.split("/")[-1]))
                else:
                    self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": str(e)})

        def do_POST(self):  # noqa: N802
            try:
                # CSRF hardening: Host allowlist (anti-rebinding), JSON
                # content type (forces a CORS preflight, which we never
                # answer), and an Origin check against the allowlist.
                if not self._host_ok():
                    self._send(403, {"error": "unrecognized Host header"})
                    return
                if self._authed() is not True:
                    self._send(401, {"error": "access code required"})
                    return
                ctype = self.headers.get("Content-Type", "")
                if not ctype.startswith("application/json"):
                    self._send(415, {"error": "Content-Type must be "
                                              "application/json"})
                    return
                origin = self.headers.get("Origin")
                # both schemes: behind a TLS proxy (Render etc.) the browser
                # sends https origins while the app itself speaks http
                allowed_origins = {f"{s}://{h}" for h in allowed_hosts
                                   for s in ("http", "https")}
                if (origin and origin != "null"
                        and "*" not in allowed_hosts
                        and origin not in allowed_origins):
                    self._send(403, {"error": "cross-origin requests are "
                                              "not allowed"})
                    return
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
    allowed_hosts = {f"{host}:{port}", f"127.0.0.1:{port}",
                     f"localhost:{port}"}
    # Hosted deployments: OPENCSR_ALLOWED_HOSTS="demo.example.com,other.com"
    # adds public hostnames (matched with or without a port); "*" disables
    # the check entirely — acceptable only for a keyless public mock demo.
    extra = os.environ.get("OPENCSR_ALLOWED_HOSTS", "")
    for h in extra.split(","):
        h = h.strip().lower()
        if h:
            allowed_hosts.add(h)
            allowed_hosts.add(f"{h}:{port}")
    httpd = _Server((host, port), make_handler(app, allowed_hosts))
    print(f"OpenCSR workbench: http://{host}:{port}  "
          f"(backend: {backend}, ledger: {db_path})")
    httpd.serve_forever()
