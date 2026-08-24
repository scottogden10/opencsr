#!/usr/bin/env python3
"""OpenCSR autonomous end-to-end demo.

    python3 run_demo.py                    # offline, deterministic (mock backend)
    python3 run_demo.py --backend managed  # Claude Managed Agents (needs API key)

Acts:
  1. Governed drafting at skill v1  — pipeline drafts CSR §11.4.1, burns a
     revision cycle, the (simulated) reviewer accepts WITH modifications and
     leaves teaching feedback. Document v1 is written with full lineage.
  2. Seeded fault                   — a transposed-arms TLF; the pipeline
     blocks the proposal and the reviewer rejects it.
  3. Patient narrative              — ICH E3 12.3.2 narrative for the fatal
     SAE subject, validated against the datasets.
  4. Hill climb                     — baseline eval → skill-smith proposes
     rules from feedback + failures → gate-protected promotion → re-eval.
  5. Re-run at improved skills      — first-pass clean, zero revisions,
     accepted as proposed. Document v2.

Everything lands in the ledger (opencsr.db). Inspect it with:  python3 serve.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from opencsr.agents.backend import get_backend
from opencsr.evals import run_eval_corpus
from opencsr.governance import simulate_review
from opencsr.hillclimb import run_hillclimb
from opencsr.skills import list_skills, reset_skills
from opencsr.store import Store
from opencsr.workflow import run_efficacy_task, run_narrative_task

RULE = "=" * 74


def banner(text: str):
    print(f"\n{RULE}\n{text}\n{RULE}")


def show_task(store: Store, task_id: str):
    task = store.get_task(task_id)
    r = task["result"] or {}
    props = store.by_task("proposals", task_id)
    if props:
        print(f"\n  DRAFT (revision {props[-1]['revision']}):")
        print("  " + props[-1]["text"].replace("\n", "\n  "))
    open_issues = [i for i in store.by_task("issues", task_id)
                   if i["status"] == "open"]
    if open_issues:
        print("\n  OPEN ISSUES:")
        for i in open_issues:
            print(f"    [{i['severity']:6s}] {i['category']}: "
                  f"{i['description'][:96]}")
    print(f"\n  revisions={r.get('revisions')}  blocking={r.get('blocking')}  "
          f"capability_violations={r.get('capability_violations', 0)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=["mock", "managed"])
    ap.add_argument("--db", default="opencsr.db")
    ap.add_argument("--no-reset", action="store_true",
                    help="keep existing ledger and skill versions")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    if not args.no_reset:
        reset_skills()
        Path(args.db).unlink(missing_ok=True)
    store = Store(args.db)
    backend = get_backend(args.backend)

    banner("OpenCSR demo — study OCS-101 (EMBER-1), synthetic, non-GxP\n"
           "Timeline: data cutoff 01 May 2026 -> hard lock 12 Jun -> final "
           "TLFs 26 Jun -> CSR authoring (this system) -> eCTD M5.3.5")
    print(f"backend: {backend.name}   skills: "
          f"{ {s['name']: 'v%d' % s['version'] for s in list_skills()} }")

    # ---- Act 1: governed drafting at skill v1 ---------------------------
    banner("ACT 1 — Draft CSR §11.4.1 at skill v1 (expect churn + teaching)")
    t1 = run_efficacy_task(store, backend)
    show_task(store, t1)
    rev = simulate_review(store, t1)
    print(f"\n  GOVERNANCE REVIEW: {rev['decision'].upper()}")
    print(f"  {rev.get('comment', '')}")
    doc = store.latest_document("csr/11.4.1")
    if doc:
        print(f"\n  DOCUMENT csr/11.4.1 v{doc['version']} written:")
        print("  " + doc["text"].replace("\n", "\n  "))

    # ---- Act 2: seeded fault -------------------------------------------
    banner("ACT 2 — Same task against a corrupted TLF (transposed arms)")
    t2 = run_efficacy_task(store, backend, fault_id="swapped_arms",
                           title="Draft §11.4.1 (seeded fault: swapped_arms)")
    show_task(store, t2)
    rev2 = simulate_review(store, t2)
    print(f"\n  GOVERNANCE REVIEW: {rev2['decision'].upper()} — "
          f"{rev2.get('comment', '')[:140]}")

    # ---- Act 3: patient narrative --------------------------------------
    banner("ACT 3 — ICH E3 12.3.2 narrative for the fatal SAE subject")
    t3 = run_narrative_task(store, backend)
    show_task(store, t3)
    rev3 = simulate_review(store, t3)
    print(f"\n  GOVERNANCE REVIEW: {rev3['decision'].upper()}")

    # ---- Act 4: hill climb ---------------------------------------------
    banner("ACT 4 — Hill climb: feedback + eval failures -> skill candidates "
           "-> replay -> promote")
    climb = run_hillclimb(store, backend, max_rounds=args.rounds)
    for h in climb["history"]:
        if "candidate" in h:
            c = h["candidate"]
            mark = "PROMOTED" if h["promoted"] else "rejected"
            print(f"  {h['stage']}: {c['skill']} + {c['rule_marker']}  "
                  f"score {h['baseline_score']} -> {h['candidate_score']}  "
                  f"[{mark}]")
            hyp = c.get("hypothesis", {})
            if hyp.get("problem"):
                print(f"      hypothesis: {hyp['problem'][:90]}")
        elif "eval" in h:
            print(f"  {h['stage']}: score={h['eval']['score']} "
                  f"gates={'PASS' if h['eval']['gates_passed'] else 'FAIL'}")
        else:
            print(f"  {h['stage']}: {h.get('note', '')}")
    print(f"\n  score: {climb['baseline_score']} -> {climb['final_score']}  "
          f"improved={climb['improved']}")
    print(f"  skills now: "
          f"{ {s['name']: 'v%d' % s['version'] for s in list_skills()} }")

    # ---- Act 5: re-run at improved skills ------------------------------
    banner("ACT 5 — Draft §11.4.1 again at the promoted skills")
    t5 = run_efficacy_task(store, backend,
                           title="Draft §11.4.1 (post-hill-climb)")
    show_task(store, t5)
    rev5 = simulate_review(store, t5)
    print(f"\n  GOVERNANCE REVIEW: {rev5['decision'].upper()} — "
          f"{rev5.get('comment', '')[:120]}")
    doc = store.latest_document("csr/11.4.1")
    print(f"  document csr/11.4.1 is now v{doc['version']}")

    # ---- summary --------------------------------------------------------
    evals = store.list_evals()
    audits = store.audit_events(limit=10000)
    summary = {
        "backend": backend.name,
        "score_before": climb["baseline_score"],
        "score_after": climb["final_score"],
        "skill_versions": {s["name"]: s["version"] for s in list_skills()},
        "tasks_run": len(store.list_tasks()),
        "eval_runs": len(evals),
        "audit_events": len(audits),
        "act5_revisions": store.get_task(t5)["result"]["revisions"],
        "act5_decision": rev5["decision"],
    }
    Path("demo_summary.json").write_text(json.dumps(summary, indent=2))
    banner("DONE — full lineage is in the ledger")
    print(json.dumps(summary, indent=2))
    print("\nOpen the workbench to explore every claim, evidence locator, "
          "issue, decision, skill version, and audit event:\n"
          "    python3 serve.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
