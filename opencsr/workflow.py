"""The governed task pipeline — deterministic outside, agentic inside.

Code decides the stage order, budgets, revision limits, capability grants,
and blocking rules. Agents get discretion only inside bounded runs against
a frozen snapshot. Humans (or the simulated governance reviewer) own the
final decision; an accepted change becomes a new document version with full
lineage.
"""

from __future__ import annotations

import json

from .agents.registry import AGENTS, build_agent_prompt
from .domain import AgentRun, Issue, new_id, sha256_text
from .sources import Snapshot
from .store import Store
from .tools import ToolGateway
from .validators import (validate_claims, validate_draft, validate_narrative)

DEFAULT_BUDGETS = {"max_revisions": 2, "max_tool_calls": 60}

DRAFT_ISSUE_CATEGORIES = {"numeric_mismatch", "unsupported_claim",
                          "interpretive_overreach", "missing_required_content",
                          "terminology", "style"}


def _store_issues(store: Store, task_id: str, dicts: list[dict],
                  raised_by: str, proposal_id: str | None = None) -> list[str]:
    ids = []
    for d in dicts:
        refs = dict(d.get("refs", {}))
        if proposal_id:
            refs["proposal_id"] = proposal_id
        issue = Issue(id=new_id("iss"), task_id=task_id, category=d["category"],
                      severity=d["severity"], description=d["description"],
                      refs=refs, raised_by=raised_by)
        store.add("issues", issue)
        ids.append(issue.id)
    return ids


def _open_issues(store: Store, task_id: str) -> list[dict]:
    return [i for i in store.by_task("issues", task_id) if i["status"] == "open"]


def _resolve_stale_draft_issues(store: Store, task_id: str):
    for i in _open_issues(store, task_id):
        if i["raised_by"] == "validator" and i["refs"].get("proposal_id"):
            store.update_fields("issues", i["id"], status="resolved")


def _latest_proposal(store: Store, task_id: str) -> dict | None:
    props = store.by_task("proposals", task_id)
    return props[-1] if props else None


def run_stage(store: Store, backend, task_id: str, agent_name: str,
              brief: str, snapshot: Snapshot, skills_override: dict | None,
              config: dict) -> tuple[ToolGateway, dict]:
    prompt, pinned = build_agent_prompt(agent_name, skills_override)
    run = AgentRun(id=new_id("run"), task_id=task_id, agent=agent_name,
                   agent_version=sha256_text(prompt)[:19], backend=backend.name)
    store.add("runs", run)
    gw = ToolGateway(store, snapshot, task_id, run.id, agent_name,
                     AGENTS[agent_name]["tools"])
    store.audit("agent_run.started", task_id,
                {"agent": agent_name, "run": run.id, "backend": backend.name},
                {"skills": pinned, "prompt_hash": run.agent_version})
    result = backend.run_agent(agent_name, prompt, brief, gw, config)
    store.update_fields(
        "runs", run.id, status=result.get("status", "completed"),
        detail={"notes": result.get("notes", ""),
                "usage": result.get("usage", {}),
                "violations": gw.violations,
                "final_output": gw.final_output, "skills": pinned})
    store.audit("agent_run.completed", task_id,
                {"agent": agent_name, "run": run.id},
                {"status": result.get("status"), "tool_calls": gw.calls,
                 "violations": len(gw.violations)})
    return gw, result


# ---------------------------------------------------------------------------
# CSR §11.4.1 primary efficacy task
# ---------------------------------------------------------------------------

def run_efficacy_task(store: Store, backend, fault_id: str | None = None,
                      skills_override: dict | None = None,
                      title: str | None = None,
                      budgets: dict | None = None) -> str:
    budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
    snapshot = Snapshot(fault_id=fault_id)
    config = {"backend": backend.name, "fault_id": fault_id, "budgets": budgets}
    task_id = store.create_task(
        "csr.section.draft",
        title or "Draft CSR §11.4.1 primary efficacy paragraph",
        "csr/11.4.1", snapshot.manifest(), config)
    store.audit("task.created", task_id, {"initiated_by": "runtime"}, config)
    store.audit("snapshot.frozen", task_id, {"initiated_by": "runtime"},
                snapshot.manifest())
    store.update_task(task_id, status="running")

    # 1) evidence
    run_stage(store, backend, task_id, "evidence",
              "Task: collect and register the evidence needed to draft the "
              "primary efficacy paragraph for CSR section 11.4.1 of study "
              "OCS-101: endpoint definition, primary analysis population, "
              "data cutoff, the confirmed ORR result cells for both arms in "
              "T14.2.1, and every governing footnote.",
              snapshot, skills_override, config)

    # 2) claims
    run_stage(store, backend, task_id, "biostat",
              "Task: construct typed claims for the CSR 11.4.1 primary "
              "efficacy paragraph from the registered evidence. Cross-check "
              "every value with the validated calculator and dataset "
              "recounts; raise issues for any disagreement.",
              snapshot, skills_override, config)

    claim_issue_dicts = validate_claims(store, snapshot, task_id)
    _store_issues(store, task_id, claim_issue_dicts, "validator")
    store.audit("claims.validated", task_id, {"actor": "validator"},
                {"issues": len(claim_issue_dicts)})

    # 3) bounded draft/revise loop
    revisions = 0
    first_pass: dict | None = None
    metrics: dict = {}
    while True:
        brief = ("Task: draft the primary efficacy paragraph for CSR section "
                 "11.4.1 from the registered claims only.")
        if revisions > 0:
            # cumulative: every high-severity draft issue raised in any
            # revision, so fixes are never lost between rounds. Convention
            # gaps (medium/low) are left for the human reviewer.
            seen, fb = set(), []
            for i in store.by_task("issues", task_id):
                if (i["category"] in DRAFT_ISSUE_CATEGORIES
                        and i["severity"] == "high"
                        and i["description"] not in seen):
                    seen.add(i["description"])
                    fb.append(f"- [{i['severity']}] {i['category']}: "
                              f"{i['description']}")
            brief += ("\n\nREVISION FEEDBACK — address every issue and "
                      "resubmit:\n" + "\n".join(fb))
        run_stage(store, backend, task_id, "writer", brief, snapshot,
                  skills_override, config)
        proposal = _latest_proposal(store, task_id)
        if proposal is None or not proposal["text"].strip():
            store.update_task(task_id, status="failed",
                              result={"error": "writer produced no usable draft "
                                               "(see missing_evidence issues)"})
            store.audit("task.failed", task_id, {"actor": "runtime"},
                        {"reason": "empty draft"})
            return task_id
        draft_issue_dicts, metrics = validate_draft(store, snapshot, task_id,
                                                    proposal)
        if first_pass is None:
            first_pass = dict(metrics)
            first_pass["draft_issues"] = len(draft_issue_dicts)
        _resolve_stale_draft_issues(store, task_id)
        _store_issues(store, task_id, draft_issue_dicts, "validator",
                      proposal["id"])
        store.audit("draft.validated", task_id, {"actor": "validator"},
                    {"proposal": proposal["id"], "revision": revisions,
                     "issues": len(draft_issue_dicts), "metrics": metrics})
        high = [d for d in draft_issue_dicts if d["severity"] == "high"]
        if not high or revisions >= budgets["max_revisions"]:
            break
        revisions += 1

    # 4) independent QC
    qc_brief = ("Independent QC of the following draft for CSR 11.4.1. "
                "Verify every number, population, arm, and cutoff against "
                "the frozen sources with your own tools.\n\n--- DRAFT ---\n"
                f"{proposal['text']}\n\n--- SENTENCE MAP ---\n"
                f"{json.dumps(proposal['sentence_map'])}")
    run_stage(store, backend, task_id, "qc", qc_brief, snapshot,
              skills_override, config)

    # 5) gate + present for review
    open_now = _open_issues(store, task_id)
    high_open = [i for i in open_now if i["severity"] == "high"]
    store.update_fields("proposals", proposal["id"], status="pending_review")
    runs = store.by_task("runs", task_id)
    violations = sum(len(r["detail"].get("violations", [])) for r in runs
                     if isinstance(r.get("detail"), dict))
    result = {
        "proposal_id": proposal["id"],
        "revisions": revisions,
        "first_pass": first_pass,
        "final_metrics": metrics,
        "blocking": bool(high_open),
        "open_issue_counts": {
            "high": len(high_open),
            "medium": len([i for i in open_now if i["severity"] == "medium"]),
            "low": len([i for i in open_now if i["severity"] == "low"])},
        "capability_violations": violations,
    }
    store.update_task(task_id, status="pending_review", result=result)
    store.audit("task.ready_for_review", task_id, {"actor": "runtime"}, result)
    return task_id


# ---------------------------------------------------------------------------
# ICH E3 12.3.2 narrative task
# ---------------------------------------------------------------------------

def find_narrative_subject(snapshot: Snapshot) -> str:
    for r in snapshot.datasets["ADAE"]:
        if r["AEOUT"] == "FATAL":
            return r["USUBJID"]
    for r in snapshot.datasets["ADAE"]:
        if r["AESER"] == "Y":
            return r["USUBJID"]
    raise RuntimeError("no narrative subject found")


def run_narrative_task(store: Store, backend, usubjid: str | None = None,
                       skills_override: dict | None = None) -> str:
    snapshot = Snapshot()
    usubjid = usubjid or find_narrative_subject(snapshot)
    config = {"backend": backend.name, "usubjid": usubjid,
              "budgets": {"max_revisions": 1}}
    task_id = store.create_task(
        "csr.narrative.draft",
        f"Draft ICH E3 12.3.2 narrative for {usubjid}",
        f"csr/12.3.2/{usubjid}", snapshot.manifest(), config)
    store.audit("task.created", task_id, {"initiated_by": "runtime"}, config)
    store.update_task(task_id, status="running")

    revisions = 0
    first_pass = None
    while True:
        brief = (f"Task: draft the ICH E3 12.3.2 safety narrative.\n"
                 f"USUBJID: {usubjid}")
        if revisions > 0:
            fb = [f"- [{i['severity']}] {i['category']}: {i['description']}"
                  for i in _open_issues(store, task_id)]
            brief += ("\n\nREVISION FEEDBACK — address every issue and "
                      "resubmit:\n" + "\n".join(fb))
        run_stage(store, backend, task_id, "narrative", brief, snapshot,
                  skills_override, config)
        proposal = _latest_proposal(store, task_id)
        if proposal is None or not proposal["text"].strip():
            store.update_task(task_id, status="failed",
                              result={"error": "no usable narrative produced "
                                               "(see issues)"})
            store.audit("task.failed", task_id, {"actor": "runtime"},
                        {"reason": "empty narrative"})
            return task_id
        issue_dicts, metrics = validate_narrative(snapshot, proposal["text"],
                                                  usubjid)
        if first_pass is None:
            first_pass = dict(metrics)
            first_pass["missing_onset"] = sum(
                1 for i in issue_dicts if "onset" in i["description"].lower())
            first_pass["issues"] = len(issue_dicts)
        _resolve_stale_draft_issues(store, task_id)
        _store_issues(store, task_id, issue_dicts, "validator", proposal["id"])
        if not issue_dicts or revisions >= config["budgets"]["max_revisions"]:
            break
        revisions += 1

    open_now = _open_issues(store, task_id)
    store.update_fields("proposals", proposal["id"], status="pending_review")
    result = {"proposal_id": proposal["id"], "revisions": revisions,
              "first_pass": first_pass, "final_metrics": metrics,
              "blocking": any(i["severity"] == "high" for i in open_now),
              "open_issue_counts": {"total": len(open_now)}}
    store.update_task(task_id, status="pending_review", result=result)
    store.audit("task.ready_for_review", task_id, {"actor": "runtime"}, result)
    return task_id


# ---------------------------------------------------------------------------
# Human (or simulated) decision -> controlled patch
# ---------------------------------------------------------------------------

def apply_decision(store: Store, task_id: str, decision: str,
                   reason_code: str, scope: str, comment: str,
                   final_text: str | None, actor: str) -> dict:
    from .domain import FeedbackEvent, REASON_CODES, FEEDBACK_SCOPES
    if reason_code not in REASON_CODES:
        reason_code = "other"
    if scope not in FEEDBACK_SCOPES:
        scope = "this_instance"
    task = store.get_task(task_id)
    if not task:
        return {"error": "unknown task"}
    proposal = _latest_proposal(store, task_id)
    if not proposal:
        return {"error": "no proposal to decide on"}
    if proposal["status"] != "pending_review":
        return {"error": f"proposal is '{proposal['status']}' — only a "
                         f"pending_review proposal can be decided; the task "
                         f"was already closed"}
    node = proposal["target_node"]

    if decision in ("accepted", "modified"):
        latest = store.latest_document(node)
        current_version = latest["version"] if latest else 0
        if current_version != proposal["base_version"]:
            store.update_fields("proposals", proposal["id"], status="stale")
            # close the task too — a stale proposal can never be decided, so
            # leaving it pending would wedge the review queue and the CSR view
            store.update_task(task_id, status="closed_stale")
            store.audit("proposed_change.stale", task_id, {"actor": actor},
                        {"proposal": proposal["id"],
                         "base_version": proposal["base_version"],
                         "current_version": current_version})
            return {"error": "base document changed since proposal; task must "
                             "be re-run against a fresh snapshot",
                    "status": "stale"}
        text = final_text if (final_text and decision == "modified") else proposal["text"]
        version = store.write_document(node, text, sha256_text(text), actor,
                                       task_id)
        store.update_fields("proposals", proposal["id"], status=decision)
        for i in _open_issues(store, task_id):
            store.update_fields("issues", i["id"], status="overridden")
        new_status = f"closed_{decision}"
    elif decision == "rejected":
        store.update_fields("proposals", proposal["id"], status="rejected")
        version = None
        new_status = "closed_rejected"
    else:
        return {"error": f"unknown decision {decision}"}

    fb = FeedbackEvent(id=new_id("fb"), task_id=task_id,
                       proposal_id=proposal["id"], decision=decision,
                       reason_code=reason_code, scope=scope, comment=comment,
                       final_text=final_text or "", actor=actor)
    store.add("feedback", fb)
    store.update_task(task_id, status=new_status)
    store.audit(f"proposed_change.{decision}", task_id,
                {"decided_by": actor,
                 "generated_by_task": task_id},
                {"proposal": proposal["id"], "reason_code": reason_code,
                 "scope": scope, "comment": comment,
                 "document_version": version,
                 "config": task.get("config", {}),
                 "snapshot": task.get("snapshot", {})})
    return {"status": decision, "document_version": version,
            "feedback_id": fb.id}
