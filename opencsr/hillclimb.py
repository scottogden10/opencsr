"""Bounded self-improvement (hill climbing) over versioned skills.

Production skills never rewrite themselves online. The loop is:

    reviewer feedback + eval failures
      -> skill-smith proposes a MINIMAL rule addition with a hypothesis
      -> candidate is replayed against the full regression corpus
      -> promoted only if every hard gate passes AND the score improves
      -> promotion = new immutable skill version (+ audit); else rejected.

Because agent system prompts are compiled from skills at run time, a
promotion automatically becomes a new agent configuration version on the
next run (and, on the Managed Agents backend, a new versioned agent object).
Rollback = the previous version file is retained under skills/<name>/versions/.
"""

from __future__ import annotations

from .evals import run_eval_corpus
from .skills import load_skill, render_skill_content
from .store import Store

LEARNED_HEADER = "## House rules (learned)"


def _candidate_content(skill_name: str, rule_text: str) -> tuple[str, int, str]:
    cur = load_skill(skill_name)
    body = cur["body"]
    if LEARNED_HEADER in body:
        body = body.rstrip() + f"\n{rule_text}\n"
    else:
        body = body.rstrip() + f"\n\n{LEARNED_HEADER}\n\n{rule_text}\n"
    new_version = cur["version"] + 1
    return (render_skill_content(skill_name, body, new_version,
                                 cur["description"]),
            new_version, cur["description"])


# Which eval cases can possibly observe a change to each skill. Candidates
# whose skill has no exercised case in a scoped run are skipped — an eval
# that cannot see the change can only reject it (pure wasted spend).
SKILL_OBSERVED_BY = {
    "csr-efficacy-reporting": {"gold_efficacy"},
    "numeric-conventions": {"gold_efficacy", "narrative"},
    "patient-narrative": {"narrative"},
}


def run_hillclimb(store: Store, backend, max_rounds: int = 3,
                  cases: list[str] | None = None,
                  min_improvement: float = 0.0) -> dict:
    """Greedy, gate-protected hill climb. Returns a full history.

    `min_improvement` is the promotion margin: candidates must beat the
    baseline score by more than this. Keep 0 for the deterministic mock
    backend; use a positive margin on live backends where run-to-run
    variance could otherwise promote noise."""
    history = []
    baseline = run_eval_corpus(store, backend, label="baseline", cases=cases)
    history.append({"stage": "baseline", "eval": _slim(baseline)})

    for rnd in range(1, max_rounds + 1):
        feedback = _all_feedback(store)
        signals = {"feedback": feedback,
                   "eval_report": {"metrics": baseline["metrics"]}}
        candidates = backend.run_skillsmith(
            "Propose minimal skill rule additions from the signals.", signals)
        # Candidates are agent-proposed: validate before touching the
        # filesystem (skill must exist — no path traversal — and the rule
        # marker must be well-formed and present in the rule text).
        import re as _re
        from .skills import list_skills as _ls
        known = {s["name"] for s in _ls()}
        candidates = [c for c in (candidates or [])
                      if isinstance(c, dict)
                      and c.get("skill") in known
                      and isinstance(c.get("rule_text"), str)
                      and _re.fullmatch(r"RULE:[A-Z_]{3,40}",
                                        str(c.get("rule_marker", "")))
                      and c["rule_marker"] in c["rule_text"]]
        if not candidates:
            history.append({"stage": f"round{rnd}",
                            "note": "no candidates proposed; converged"})
            break

        for cand in candidates:
            skill = cand["skill"]
            if cases is not None:
                observers = SKILL_OBSERVED_BY.get(skill)
                if observers and not (set(cases) & observers):
                    history.append({
                        "stage": f"round{rnd}", "candidate_skipped": cand,
                        "note": f"skipped: no case in scope can observe "
                                f"changes to '{skill}'"})
                    store.audit("skill.candidate_skipped", None,
                                {"actor": "hillclimb"},
                                {"skill": skill,
                                 "rule": cand.get("rule_marker"),
                                 "reason": "not observable in scoped corpus"})
                    continue
            content, new_version, description = _candidate_content(
                skill, cand["rule_text"])
            label = f"candidate:{skill}@v{new_version}:{cand['rule_marker']}"
            cand_eval = run_eval_corpus(store, backend,
                                        skills_override={skill: content},
                                        label=label, cases=cases)
            improved = cand_eval["score"] > baseline["score"] + min_improvement
            gates_ok = cand_eval["gates_passed"]
            # A candidate may not regress a passing gate even if score rises.
            regressed = [g for g, ok in baseline["hard_gates"].items()
                         if ok and not cand_eval["hard_gates"].get(g, False)]
            promote = improved and gates_ok and not regressed

            entry = {"stage": f"round{rnd}", "candidate": cand,
                     "baseline_score": baseline["score"],
                     "candidate_score": cand_eval["score"],
                     "gates_passed": gates_ok, "gate_regressions": regressed,
                     "promoted": promote, "eval": _slim(cand_eval)}
            if promote:
                from .skills import parse_skill, write_skill_version
                parsed = parse_skill(content)
                write_skill_version(skill, parsed["body"], new_version,
                                    description)
                store.skill_event(skill, new_version, "promoted",
                                  parsed["hash"], cand["hypothesis"],
                                  cand_eval["eval_id"])
                store.audit("skill.promoted", None,
                            {"actor": "hillclimb", "backend": backend.name},
                            {"skill": skill, "version": new_version,
                             "rule": cand["rule_marker"],
                             "hypothesis": cand["hypothesis"],
                             "baseline_score": baseline["score"],
                             "candidate_score": cand_eval["score"],
                             "eval_id": cand_eval["eval_id"]})
                baseline = cand_eval
            else:
                store.skill_event(skill, new_version, "rejected", "",
                                  cand["hypothesis"], cand_eval["eval_id"])
                store.audit("skill.rejected", None,
                            {"actor": "hillclimb"},
                            {"skill": skill, "rule": cand["rule_marker"],
                             "reason": ("gate regression" if regressed else
                                        "no score improvement" if not improved
                                        else "hard gates failed")})
            history.append(entry)

    final = run_eval_corpus(store, backend, label="post-hillclimb", cases=cases)
    history.append({"stage": "final", "eval": _slim(final)})
    return {"history": history,
            "baseline_score": history[0]["eval"]["score"],
            "final_score": final["score"],
            "improved": final["score"] > history[0]["eval"]["score"]}


def _all_feedback(store: Store) -> list[dict]:
    rows = store._rows("SELECT * FROM feedback ORDER BY created_at")  # noqa: SLF001
    return rows


def _slim(ev: dict) -> dict:
    return {"eval_id": ev["eval_id"], "label": ev["label"],
            "score": ev["score"], "gates_passed": ev["gates_passed"],
            "metrics": ev["metrics"]}
