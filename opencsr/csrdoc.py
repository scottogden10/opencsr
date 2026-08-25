"""The assembled CSR: a living-document view of the report.

Builds the section-by-section state of the CSR from the ledger and the
frozen snapshot: which sections are populated (and by whom), which are
awaiting a human reviewer (and which role), which are being drafted, which
are frozen TLFs, and which remain shell/pending. Also derives the ICH E3
12.3.2 narrative workload (every death and every SAE subject) so the
document visibly fills in as narratives are drafted and approved.
"""

from __future__ import annotations

from .sources import Snapshot
from .store import Store

# Which human role owns the accept/modify/reject decision for each task type.
REVIEWER_ROLES = {
    "csr.section.draft": "Medical Writer",
    "csr.narrative.draft": "Safety / Pharmacovigilance",
}

# kind: shell_text (static from csr_shell.md) | authorable:<task_type key> |
#       narratives (per-subject group) | tlf:<table id> | future
OUTLINE = [
    ("2",      "Synopsis", "future"),
    ("9.1",    "Overall Study Design", "shell_text"),
    ("10.1",   "Disposition of Patients", "future"),
    ("11.1",   "Data Sets Analysed", "future"),
    ("11.4.1", "Analysis of Efficacy — Primary Endpoint", "authorable:efficacy"),
    ("11.4.7", "Efficacy Conclusions", "future"),
    ("12.1",   "Extent of Exposure", "future"),
    ("12.2.1", "Brief Summary of Adverse Events", "future"),
    ("12.3.2", "Narratives of Deaths and Serious Adverse Events", "narratives"),
    ("13",     "Discussion and Overall Conclusions", "future"),
    ("14.1.1", "Summary of Analysis Populations", "tlf:T14.1.1"),
    ("14.2.1", "Confirmed Objective Response Rate (RECIST v1.1)", "tlf:T14.2.1"),
    ("14.3.1", "Overview of Treatment-Emergent Adverse Events", "tlf:T14.3.1"),
]


def narrative_subjects(snapshot: Snapshot) -> list[dict]:
    """Every subject requiring an ICH E3 12.3.2 narrative: all deaths and
    all subjects with at least one serious adverse event."""
    adsl = {r["USUBJID"]: r for r in snapshot.datasets["ADSL"]}
    subjects: dict[str, dict] = {}
    for r in snapshot.datasets["ADAE"]:
        if r["AESER"] != "Y" and r["AEOUT"] != "FATAL":
            continue
        s = subjects.setdefault(r["USUBJID"], {
            "usubjid": r["USUBJID"],
            "arm": adsl.get(r["USUBJID"], {}).get("TRT01P", ""),
            "events": [], "fatal": False})
        s["events"].append(f"{r['AEDECOD']} (Grade {r['ATOXGR']})")
        if r["AEOUT"] == "FATAL":
            s["fatal"] = True
    for usubjid, r in adsl.items():
        if r.get("DTHFL") != "Y":
            continue
        if usubjid in subjects:
            # a death always outranks the subject's non-fatal SAEs
            subjects[usubjid]["fatal"] = True
            subjects[usubjid]["events"].append(
                r.get("DCSREAS", "death").title())
        else:
            subjects[usubjid] = {
                "usubjid": usubjid, "arm": r.get("TRT01P", ""),
                "events": [r.get("DCSREAS", "cause not recorded").title()],
                "fatal": True}
    out = sorted(subjects.values(),
                 key=lambda s: (not s["fatal"], s["usubjid"]))
    for s in out:
        s["reason"] = ("Death: " if s["fatal"] else "SAE: ") + "; ".join(
            sorted(set(s["events"]))[:3])
        del s["events"]
    return out


def _node_status(store: Store, tasks: list[dict], node: str) -> dict:
    """Resolve one document node's lifecycle state."""
    doc = store.latest_document(node)
    node_tasks = [t for t in tasks if t.get("target_node") == node]
    # list_tasks orders created_at DESC — index 0 is the NEWEST task
    pending = [t for t in node_tasks if t["status"] == "pending_review"]
    running = [t for t in node_tasks if t["status"] == "running"]
    status: dict = {"node": node}
    if pending:
        t = pending[0]
        status.update({
            "state": "in_review", "task_id": t["id"],
            "reviewer_role": REVIEWER_ROLES.get(t["task_type"], "Reviewer")})
        props = store.by_task("proposals", t["id"])
        if props:
            status["excerpt"] = props[-1]["text"][:220]
    elif running:
        status.update({"state": "drafting", "task_id": running[0]["id"]})
    elif doc:
        status.update({
            "state": "populated", "version": doc["version"],
            "text": doc["text"], "updated_by": doc["updated_by"],
            "updated_at": doc["updated_at"], "task_id": doc["task_id"],
            "history": [{"version": h["version"], "updated_by": h["updated_by"],
                         "updated_at": h["updated_at"], "task_id": h["task_id"]}
                        for h in store.document_history(node)]})
    else:
        status["state"] = "pending"
    return status


def build_csr_view(store: Store, snapshot: Snapshot) -> dict:
    tasks = store.list_tasks(limit=500)
    sections = []
    populated = 0
    total_units = 0

    for sec, title, kind in OUTLINE:
        entry: dict = {"section": sec, "title": title, "kind": kind}
        if kind == "shell_text":
            shell = snapshot.docs["csr_shell"].get(sec)
            entry["state"] = "shell"
            entry["text"] = " ".join(shell["paragraphs"]) if shell else ""
        elif kind.startswith("authorable:"):
            total_units += 1
            st = _node_status(store, tasks, f"csr/{sec}")
            entry.update(st)
            if st["state"] == "populated":
                populated += 1
        elif kind == "narratives":
            subs = []
            for s in narrative_subjects(snapshot):
                total_units += 1
                st = _node_status(store, tasks, f"csr/12.3.2/{s['usubjid']}")
                if st["state"] == "populated":
                    populated += 1
                subs.append({**s, **st})
            entry["state"] = "group"
            entry["subjects"] = subs
            done = sum(1 for x in subs if x["state"] == "populated")
            entry["progress"] = {"done": done, "total": len(subs)}
        elif kind.startswith("tlf:"):
            tid = kind.split(":", 1)[1]
            t = snapshot.tables.get(tid)
            entry.update({"state": "final_tlf", "table_id": tid,
                          "population": t["population"] if t else "",
                          "data_cutoff": t["data_cutoff"] if t else ""})
        else:
            entry["state"] = "future"
        sections.append(entry)

    review_queue: dict[str, list] = {}
    for t in tasks:
        if t["status"] == "pending_review":
            role = REVIEWER_ROLES.get(t["task_type"], "Reviewer")
            review_queue.setdefault(role, []).append({
                "task_id": t["id"], "title": t["title"],
                "target_node": t["target_node"],
                "blocking": (t.get("result") or {}).get("blocking")})

    return {
        "study": snapshot.study["study_id"],
        "study_name": snapshot.study.get("study_name", ""),
        "sections": sections,
        "progress": {"populated": populated, "total": total_units},
        "review_queue": review_queue,
        "roles": sorted(set(REVIEWER_ROLES.values())),
    }
