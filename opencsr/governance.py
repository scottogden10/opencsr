"""Simulated governance reviewer.

Stands in for the accountable human (medical writer / statistician) in
autonomous demo runs: rejects proposals with unresolved high-severity
issues, fixes surviving convention gaps (teaching the system via structured
feedback events), and accepts clean work. In the workbench UI a real human
makes these calls instead — same apply_decision path, same audit trail.
"""

from __future__ import annotations

import re

from .store import Store
from .workflow import apply_decision, _latest_proposal, _open_issues

REASON_BY_CATEGORY = {
    "numeric_mismatch": "wrong_number",
    "wrong_population": "wrong_population",
    "stale_cutoff": "wrong_number",
    "unsupported_claim": "evidence_insufficient",
    "suspicious_content": "other",
    "interpretive_overreach": "interpretation_too_strong",
    "missing_required_content": "missing_content",
    "terminology": "terminology",
    "style": "style_convention",
}

def _interpretive_sentence_pattern() -> re.Pattern:
    """Sentence-strip pattern built from the SAME terminology list the
    validator enforces — a hardcoded subset here would silently accept
    drafts using the uncovered phrases."""
    from .sources import Snapshot
    phrases = Snapshot().terminology["interpretive_phrases_requiring_approval"]
    alt = "|".join(re.escape(p) for p in phrases)
    return re.compile(rf"\s*[A-Z][^.!?]*\b({alt})\b[^.!?]*[.!?]", re.I)


def simulate_review(store: Store, task_id: str,
                    persona: str = "sim:medical_writer") -> dict:
    task = store.get_task(task_id)
    proposal = _latest_proposal(store, task_id)
    if not task or not proposal:
        return {"error": "nothing to review"}
    open_issues = _open_issues(store, task_id)
    high = [i for i in open_issues if i["severity"] == "high"]

    # Interpretive overreach is the one high-severity category a reviewer
    # fixes by editing (strip the phrase) rather than rejecting — the classic
    # "clinically meaningful" correction. Everything else high blocks.
    if high and all(i["category"] == "interpretive_overreach" for i in high):
        high = []

    # 1) Unresolved high-severity issues block acceptance outright.
    if high:
        cats = sorted({i["category"] for i in high})
        reason = REASON_BY_CATEGORY.get(high[0]["category"], "other")
        comment = (f"Rejected: {len(high)} unresolved high-severity issue(s) "
                   f"({', '.join(cats)}). Example: {high[0]['description'][:160]}")
        res = apply_decision(store, task_id, "rejected", reason,
                             "this_instance", comment, None, persona)
        return {"decision": "rejected", "comment": comment, **res}

    # 2) Repair surviving convention gaps; every fix becomes teachable feedback.
    text = proposal["text"]
    fixes: list[str] = []
    reasons: list[str] = []

    pat = _interpretive_sentence_pattern()
    while (m := pat.search(text)):
        text = (text[:m.start()] + text[m.end():]).strip()
        fixes.append("removed unapproved interpretive characterization "
                     f"('{m.group(0).strip()[:60]}...')")
        if "interpretation_too_strong" not in reasons:
            reasons.append("interpretation_too_strong")

    missing_cutoff = any(i["category"] == "missing_required_content"
                         and "cutoff" in i["description"].lower()
                         for i in open_issues)
    if missing_cutoff:
        cut = next((c for c in store.by_task("claims", task_id)
                    if c["claim_type"] == "data_cutoff"), None)
        if cut and cut["value"].get("date"):
            from datetime import datetime
            d = datetime.fromisoformat(cut["value"]["date"]).strftime("%d %B %Y")
            text = text.rstrip() + (f" Results are based on the data cutoff "
                                    f"of {d}.")
            fixes.append("added the required data cutoff statement")
            reasons.append("missing_content")

    style_issues = [i for i in open_issues if i["category"] == "style"]
    if style_issues:
        for c in store.by_task("claims", task_id):
            v = c.get("value", {})
            if (c["claim_type"] == "efficacy_result"
                    and v.get("numerator") is not None):
                pat = rf"{re.escape(str(v['estimate_pct']))}% \(95% CI"
                repl = (f"{v['estimate_pct']}% ({v['numerator']}/"
                        f"{v['denominator']}; 95% CI")
                text2 = re.sub(pat, repl, text)
                if text2 != text:
                    text = text2
                    if "added n/N denominators to percentages" not in fixes:
                        fixes.append("added n/N denominators to percentages")
                        reasons.append("style_convention")

    # Safety net: if any restricted phrase survived the sentence-strip
    # (unusual sentence shapes), do not accept — send it back instead.
    from .sources import Snapshot
    leftover = [p for p in
                Snapshot().terminology["interpretive_phrases_requiring_approval"]
                if p.lower() in text.lower()]
    if leftover:
        comment = (f"Rejected: unapproved interpretive language remains "
                   f"({', '.join(leftover)}) and could not be safely edited "
                   f"out.")
        res = apply_decision(store, task_id, "rejected",
                             "interpretation_too_strong", "this_instance",
                             comment, None, persona)
        return {"decision": "rejected", "comment": comment, **res}

    if fixes:
        comment = ("Accepted with modifications: " + "; ".join(fixes) +
                   ". Please make these house conventions so future drafts "
                   "arrive correct (n/N with every percentage; always state "
                   "the data cutoff; no unapproved interpretive language).")
        res = apply_decision(store, task_id, "modified", reasons[0],
                             "organization_wide", comment, text, persona)
        return {"decision": "modified", "comment": comment, "fixes": fixes, **res}

    comment = "Accepted as proposed."
    res = apply_decision(store, task_id, "accepted", "accepted_as_proposed",
                         "this_instance", comment, None, persona)
    return {"decision": "accepted", "comment": comment, **res}
