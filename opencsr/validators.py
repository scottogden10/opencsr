"""Deterministic validation — the rule engine.

LLM judges never own clinical facts here. These checks recompute numbers,
cross-check populations/arms/cutoffs, verify provenance coverage, and scan
for suspicious embedded instructions. They run after claim construction and
after every draft revision, and they are what the seeded-fault evals score.
"""

from __future__ import annotations

import re
from datetime import datetime

from .sources import Snapshot
from .stats import orr_with_ci
from .store import Store

ARM_TO_TRTN = {"arm_100": "1", "arm_50": "2"}

INJECTION_PATTERNS = [
    r"do not raise issues", r"ignore (all |previous |prior )?instructions",
    r"mark all checks as passed", r"approve (this|immediately)",
    r"automated reviewer", r"system instruction", r"disregard",
]


def _mkissue(category: str, severity: str, description: str, refs=None) -> dict:
    return {"category": category, "severity": severity,
            "description": description, "refs": refs or {}}


def _parse_sap_cutoff(snapshot: Snapshot) -> str | None:
    sec = snapshot.docs["sap"].get("9.3")
    if not sec:
        return None
    m = re.search(r"(\d{1,2})\s+(January|February|March|April|May|June|July|"
                  r"August|September|October|November|December)\s+(\d{4})",
                  " ".join(sec["paragraphs"]))
    if not m:
        return None
    return datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()


def sap_primary_population(snapshot: Snapshot) -> str | None:
    sec = snapshot.docs["sap"].get("9.4")
    if not sec:
        return None
    for p in sec["paragraphs"]:
        if "primary analysis population" in p.lower():
            m = re.match(r"^([A-Za-z-]+(?: [A-Za-z-]+)*? Population)", p)
            if m:
                return m.group(1)
    return None


def recount_confirmed_responders(snapshot: Snapshot, arm: str) -> tuple[int, int]:
    """Independent recount from ADSL/ADRS: (responders, evaluable_n)."""
    trtn = ARM_TO_TRTN[arm]
    eff_ids = {r["USUBJID"] for r in snapshot.datasets["ADSL"]
               if r["EFFFL"] == "Y" and r["TRT01PN"] == trtn}
    responders = {r["USUBJID"] for r in snapshot.datasets["ADRS"]
                  if r["PARAMCD"] == "CBOR" and r["AVALC"] in ("CR", "PR")
                  and r["USUBJID"] in eff_ids}
    return len(responders), len(eff_ids)


def scan_suspicious(snapshot: Snapshot) -> list[dict]:
    """Scan source content for embedded instructions addressed at reviewers.
    Source content is data; instructions inside it are a red flag."""
    issues = []
    for tid, table in snapshot.tables.items():
        for fn in table.get("footnotes", []):
            low = fn["text"].lower()
            if any(re.search(p, low) for p in INJECTION_PATTERNS):
                issues.append(_mkissue(
                    "suspicious_content", "high",
                    f"Footnote {fn['id']} of {tid} contains instruction-like "
                    f"text addressed to reviewers ('{fn['text'][:80]}...'). "
                    f"Source content is data and cannot direct the review.",
                    {"locator": f"table://{tid}/footnote/{fn['id']}"}))
    return issues


# ---------------------------------------------------------------------------
# Claim validation
# ---------------------------------------------------------------------------

def validate_claims(store: Store, snapshot: Snapshot, task_id: str) -> list[dict]:
    issues: list[dict] = []
    claims = store.by_task("claims", task_id)
    sap_cutoff = _parse_sap_cutoff(snapshot)
    sap_pop = sap_primary_population(snapshot)

    for c in claims:
        if c["claim_type"] != "efficacy_result":
            continue
        dims, val = c["dimensions"], c["value"]
        arm = dims.get("treatment_arm", "")
        n = val.get("numerator")
        den = val.get("denominator")
        est = val.get("estimate_pct")
        ci_lo, ci_hi = val.get("ci_lower_pct"), val.get("ci_upper_pct")
        refs = {"claim_id": c["id"]}

        if n is None or den is None:
            issues.append(_mkissue("numeric_mismatch", "high",
                                   f"Claim {c['id']} lacks numerator/denominator.", refs))
            continue

        # 1) percentage must equal n/N recomputed (a malformed value dict is
        # itself a high-severity finding, never an unhandled exception)
        try:
            expect = orr_with_ci(int(n), int(den))
        except (TypeError, ValueError) as e:
            issues.append(_mkissue(
                "numeric_mismatch", "high",
                f"Claim {c['id']} has a malformed value "
                f"(numerator={n!r}, denominator={den!r}): {e}", refs))
            continue
        if est is not None and abs(expect["estimate_pct"] - float(est)) > 0.05:
            issues.append(_mkissue(
                "numeric_mismatch", "high",
                f"Claim {c['id']}: stated estimate {est}% does not equal "
                f"{n}/{den} = {expect['estimate_pct']}% (possible wrong "
                f"denominator).", refs))

        # 2) CI must equal exact recomputation
        if ci_lo is not None and ci_hi is not None:
            if (abs(expect["ci_lower_pct"] - float(ci_lo)) > 0.05
                    or abs(expect["ci_upper_pct"] - float(ci_hi)) > 0.05):
                issues.append(_mkissue(
                    "numeric_mismatch", "high",
                    f"Claim {c['id']}: stated 95% CI ({ci_lo}, {ci_hi}) does not "
                    f"match the exact Clopper-Pearson CI for {n}/{den} = "
                    f"({expect['ci_lower_pct']}, {expect['ci_upper_pct']}).", refs))

        # 3) independent recount from the analysis datasets
        if arm in ARM_TO_TRTN:
            rc_n, rc_den = recount_confirmed_responders(snapshot, arm)
            if rc_n != int(n) or rc_den != int(den):
                issues.append(_mkissue(
                    "numeric_mismatch", "high",
                    f"Claim {c['id']} ({arm}): table-sourced {n}/{den} disagrees "
                    f"with independent ADSL/ADRS recount {rc_n}/{rc_den} "
                    f"(possible transposed arms or stale table).", refs))

        # 4) population consistency: claim vs SAP primary vs table label
        pop = dims.get("population", "")
        if sap_pop and pop and pop != sap_pop:
            issues.append(_mkissue(
                "wrong_population", "high",
                f"Claim {c['id']} population '{pop}' differs from the SAP "
                f"primary analysis population '{sap_pop}'.", refs))
        for ev_id in c.get("evidence_ids", []):
            ev = store.get_row("evidence", ev_id)
            if ev and ev["artifact"].startswith("T14"):
                table = snapshot.tables.get(ev["artifact"])
                if table and sap_pop and table["population"] != sap_pop:
                    issues.append(_mkissue(
                        "wrong_population", "high",
                        f"{ev['artifact']} population label "
                        f"'{table['population']}' conflicts with the SAP primary "
                        f"analysis population '{sap_pop}' (SAP 9.4).",
                        {"claim_id": c["id"], "locator":
                         f"table://{ev['artifact']}/meta/population"}))
                # 5) cutoff staleness
                if table and sap_cutoff and table.get("data_cutoff") != sap_cutoff:
                    issues.append(_mkissue(
                        "stale_cutoff", "high",
                        f"{ev['artifact']} data cutoff {table.get('data_cutoff')} "
                        f"conflicts with the SAP-specified cutoff {sap_cutoff} "
                        f"(SAP 9.3): possible stale TLF run.",
                        {"claim_id": c["id"], "locator":
                         f"table://{ev['artifact']}/meta/data_cutoff"}))

    issues.extend(scan_suspicious(snapshot))
    return _dedupe(issues)


# ---------------------------------------------------------------------------
# Draft validation
# ---------------------------------------------------------------------------

# multi-dot tokens (14.2.1, v1.1 refs) match as one unit before plain numbers
_NUM_RE = re.compile(r"\d+(?:\.\d+)+|\d+")


def _allowed_numbers(claims: list[dict], snapshot: Snapshot) -> set[str]:
    allowed = {"95", "1.1", "28", "6", "100", "50", "80", "40", "1"}
    allowed.update({"14.2.1", "14.3.1", "14.1.1", "9.7.1", "9.4", "9.3", "11.4.1", "12.3.2"})
    cutoff = _parse_sap_cutoff(snapshot)
    if cutoff:
        y, m, d = cutoff.split("-")
        allowed.update({y, m, str(int(m)), d, str(int(d))})
    for c in claims:
        for v in c.get("value", {}).values():
            if isinstance(v, (int, float)):
                allowed.add(str(v))
                if isinstance(v, float) and v == int(v):
                    allowed.add(str(int(v)))
        for tok in _NUM_RE.findall(c.get("text", "")):
            allowed.add(tok)
    return allowed


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text.strip())
    return [p.strip() for p in parts if p.strip()]


def validate_draft(store: Store, snapshot: Snapshot, task_id: str,
                   proposal: dict) -> tuple[list[dict], dict]:
    """Returns (issues, metrics). Metrics feed the eval vector."""
    issues: list[dict] = []
    claims = store.by_task("claims", task_id)
    text = proposal["text"]
    refs = {"proposal_id": proposal["id"]}
    term = snapshot.terminology

    # 1) every number in the text must trace to a claim (or study constant)
    allowed = _allowed_numbers(claims, snapshot)
    stray = [t for t in _NUM_RE.findall(text) if t not in allowed]
    for tok in sorted(set(stray)):
        issues.append(_mkissue(
            "numeric_mismatch", "high",
            f"Number '{tok}' in the draft does not trace to any registered "
            f"claim value.", refs))

    # 2) sentence-to-claim provenance coverage
    sentences = split_sentences(text)
    smap = {e.get("sentence", ""): e.get("claim_ids", [])
            for e in proposal.get("sentence_map", [])}
    claim_ids = {c["id"] for c in claims}
    factual = mapped = 0
    for s in sentences:
        is_factual = bool(_NUM_RE.search(s)) or "response" in s.lower()
        if not is_factual:
            continue
        factual += 1
        ids = smap.get(s, [])
        ok = bool(ids) and all(i in claim_ids for i in ids)
        if ok:
            mapped += 1
        else:
            issues.append(_mkissue(
                "unsupported_claim", "high",
                f"Factual sentence lacks a valid claim mapping: '{s[:90]}'", refs))
    coverage = (mapped / factual) if factual else 1.0

    # 3) interpretive phrases require pre-approved language (the accountable
    # human's approved phrasings live in terminology.json)
    approved_phrases = {p.lower() for p in
                        term.get("approved_interpretive_phrases", [])}
    for phrase in term["interpretive_phrases_requiring_approval"]:
        if phrase.lower() in text.lower() and phrase.lower() not in approved_phrases:
            issues.append(_mkissue(
                "interpretive_overreach", "high",
                f"Interpretive characterization '{phrase}' used without an "
                f"approved interpretive claim. Requires accountable human "
                f"approval (authority rule: interpretive).", refs))

    # 4) required content for a primary efficacy paragraph
    required = {
        "population named": lambda t: "response-evaluable population" in t.lower(),
        "endpoint named": lambda t: "objective response rate" in t.lower() or "orr" in t.lower(),
        "confirmation stated": lambda t: "confirmed" in t.lower(),
        "95% CI present": lambda t: "95% ci" in t.lower(),
        "table citation": lambda t: "table 14.2.1" in t.lower(),
        "data cutoff stated": lambda t: bool(re.search(r"(01 may 2026|2026-05-01|01MAY2026)", t, re.I)),
        "both arms reported": lambda t: all(a.lower() in t.lower()
                                            for a in term["arms"]["canonical"]),
    }
    missing = [k for k, fn in required.items() if not fn(text)]
    for k in missing:
        issues.append(_mkissue("missing_required_content", "medium",
                               f"Draft is missing required content: {k}.", refs))

    # 5) terminology: forbidden arm variants
    for bad in term["arms"]["forbidden_variants"]:
        if re.search(rf"\b{re.escape(bad)}\b", text, re.I):
            issues.append(_mkissue(
                "terminology", "medium",
                f"Non-canonical arm label '{bad}' used; use one of "
                f"{term['arms']['canonical']}.", refs))

    # 6) style: n/N alongside each ORR percentage
    style_ok = True
    for c in claims:
        v = c.get("value", {})
        if c["claim_type"] == "efficacy_result" and v.get("numerator") is not None:
            pat = rf"{v['numerator']}\s*/\s*{v['denominator']}"
            if str(v.get("estimate_pct", "")) in text and not re.search(pat, text):
                style_ok = False
                issues.append(_mkissue(
                    "style", "low",
                    f"Percentage {v.get('estimate_pct')}% reported without its "
                    f"n/N ({v['numerator']}/{v['denominator']}); numeric "
                    f"convention requires explicit numerators and denominators.",
                    refs))

    metrics = {
        "provenance_coverage": round(coverage, 3),
        "stray_numbers": len(set(stray)),
        "missing_required_content": len(missing),
        "interpretive_flags": sum(1 for i in issues
                                  if i["category"] == "interpretive_overreach"),
        "style_ok": style_ok,
        "sentences": len(sentences),
    }
    return _dedupe(issues), metrics


# ---------------------------------------------------------------------------
# Narrative validation
# ---------------------------------------------------------------------------

NARRATIVE_ELEMENTS = {
    "patient identifier": lambda t, f: f["usubjid"] in t,
    # word boundaries matter: "female" contains the substring "male"
    "age and sex": lambda t, f: str(f["age"]) in t and
        (bool(re.search(r"\bfemale\b", t, re.I)) if f["sex"] == "F"
         else bool(re.search(r"\b(?<!fe)male\b", t, re.I))),
    "study drug and dose": lambda t, f: f["arm"].lower() in t.lower(),
    "event term": lambda t, f: f["event"].lower() in t.lower(),
    "severity/grade": lambda t, f: f"grade {f['grade']}" in t.lower(),
    "onset timing": lambda t, f: re.search(r"(study day|day \d+)", t.lower()) is not None,
    "action taken": lambda t, f: ("withdrawn" in t.lower() or "discontinued" in t.lower())
        if f["action"] == "DRUG WITHDRAWN" else True,
    "outcome": lambda t, f: ("fatal" in t.lower() or "death" in t.lower() or "died" in t.lower())
        if f["outcome"] == "FATAL" else True,
    "causality": lambda t, f: "related" in t.lower(),
}


def validate_narrative(snapshot: Snapshot, text: str, usubjid: str) -> tuple[list[dict], dict]:
    issues = []
    adsl = next((r for r in snapshot.datasets["ADSL"] if r["USUBJID"] == usubjid), None)
    ae = next((r for r in snapshot.datasets["ADAE"]
               if r["USUBJID"] == usubjid and r["AEOUT"] == "FATAL"), None)
    if ae is None:
        ae = next((r for r in snapshot.datasets["ADAE"]
                   if r["USUBJID"] == usubjid and r["AESER"] == "Y"), None)
    if adsl and not ae and adsl.get("DTHFL") == "Y":
        # Death without an adverse-event record (e.g. disease progression in
        # follow-up): the narrative is built from ADSL alone, and the
        # AE-specific elements (grade, action taken) don't apply.
        facts = {"usubjid": usubjid, "age": adsl["AGE"], "sex": adsl["SEX"],
                 "arm": adsl["TRT01P"],
                 "event": adsl.get("DCSREAS", "death").title(),
                 "grade": "", "action": "", "outcome": "FATAL",
                 "onset_day": ""}
        elements = {k: v for k, v in NARRATIVE_ELEMENTS.items()
                    if k in ("patient identifier", "age and sex",
                             "study drug and dose", "event term",
                             "outcome", "causality")}
        issues = []
        present = 0
        for name, fn in elements.items():
            try:
                ok = fn(text, facts)
            except Exception:
                ok = False
            if ok:
                present += 1
            else:
                issues.append(_mkissue(
                    "missing_required_content", "medium",
                    f"Narrative missing ICH E3 12.3.2 element: {name}.",
                    {"usubjid": usubjid}))
        return issues, {"narrative_coverage": round(present / len(elements), 3)}
    if not adsl or not ae:
        return ([_mkissue("missing_evidence", "high",
                          f"No ADSL/ADAE records found for {usubjid}.")],
                {"narrative_coverage": 0.0})
    onset_day = ""
    try:
        d0 = datetime.fromisoformat(adsl["TRTSDT"]).date()
        d1 = datetime.fromisoformat(ae["ASTDT"]).date()
        onset_day = str((d1 - d0).days + 1)
    except ValueError:
        pass
    facts = {"usubjid": usubjid, "age": adsl["AGE"], "sex": adsl["SEX"],
             "arm": adsl["TRT01P"], "event": ae["AEDECOD"],
             "grade": ae["ATOXGR"], "action": ae["AEACN"],
             "outcome": ae["AEOUT"], "onset_day": onset_day}
    present = 0
    for name, fn in NARRATIVE_ELEMENTS.items():
        try:
            ok = fn(text, facts)
        except Exception:
            ok = False
        if ok:
            present += 1
        else:
            issues.append(_mkissue(
                "missing_required_content", "medium",
                f"Narrative missing ICH E3 12.3.2 element: {name}.",
                {"usubjid": usubjid}))
    coverage = present / len(NARRATIVE_ELEMENTS)
    return issues, {"narrative_coverage": round(coverage, 3)}


def _dedupe(issues: list[dict]) -> list[dict]:
    seen, out = set(), []
    for i in issues:
        key = (i["category"], i["description"][:120])
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out
