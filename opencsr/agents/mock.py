"""Deterministic offline backend.

Each agent behavior is scripted Python that exercises the SAME governed tool
gateway as the live Managed Agents backend — capabilities, receipts,
evidence verification, and validators all run identically. Writer and
narrative behavior is conditioned on the RULE:* markers present in the
compiled skill prompt, so skill-version changes have real, measurable
effects offline (this is what makes the hill-climb demo honest).
"""

from __future__ import annotations

import re
from datetime import datetime

ARM_LABELS = {"arm_100": "Examplinib 100 mg", "arm_50": "Examplinib 50 mg"}
ARM_TRTN = {"arm_100": "1", "arm_50": "2"}

INJECTION_PATTERNS = [
    r"do not raise issues", r"ignore (all |previous |prior )?instructions",
    r"mark all checks as passed", r"approve (this|immediately)",
    r"automated reviewer", r"system instruction", r"disregard",
]


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %B %Y")
    except ValueError:
        return iso


class MockBackend:
    name = "mock"

    def run_agent(self, agent_name, prompt, brief, gateway, config):
        fn = getattr(self, f"_run_{agent_name}", None)
        if fn is None:
            return {"status": "failed", "notes": f"no mock for {agent_name}"}
        notes = fn(prompt, brief, gateway)
        status = "completed" if gateway.finished else "failed"
        return {"status": status, "notes": notes or "",
                "usage": {"tool_calls": gateway.calls}}

    # ------------------------------------------------------------------ #
    def _run_evidence(self, prompt, brief, gw):
        gw.call("list_sources", {})
        study = gw.snapshot.study["study_id"]
        items = []

        def add_paragraph(doc, section, contains, role):
            sec = gw.call("get_section", {"doc": doc, "section": section})
            if "error" in sec:
                return
            for p in sec["paragraphs"]:
                if contains is None or contains.lower() in p["text"].lower():
                    items.append({"locator": p["locator"], "role": role})
                    if contains is not None:
                        return

        add_paragraph("protocol", "6.1", None, "endpoint_definition")
        add_paragraph("sap", "9.7.1", None, "endpoint_definition")
        add_paragraph("sap", "9.4", "primary analysis population",
                      "analysis_population")
        add_paragraph("sap", "9.3", None, "data_cutoff")

        cells = gw.call("get_tlf_cells",
                        {"table_id": "T14.2.1", "row_ids": ["orr_confirmed"]})
        for c in cells.get("cells", []):
            items.append({"locator": c["locator"], "role": "result_cell",
                          "expected": str(c["cell"].get("n", ""))})
        items.append({"locator": "table://T14.2.1/meta/population",
                      "role": "table_population"})
        items.append({"locator": "table://T14.2.1/meta/data_cutoff",
                      "role": "table_data_cutoff"})
        for fn_ in cells.get("footnotes", []):
            items.append({"locator": f"table://T14.2.1/footnote/{fn_['id']}",
                          "role": "table_footnote"})
        res = gw.call("create_evidence", {"items": items})
        n_ok = sum(1 for r in res["results"] if r["status"] == "verified")
        gw.call("submit_report", {
            "summary": f"Registered {n_ok}/{len(items)} evidence items for the "
                       f"{study} primary efficacy paragraph (endpoint, "
                       f"population, cutoff, result cells, footnotes)."})
        return f"evidence items verified: {n_ok}/{len(items)}"

    # ------------------------------------------------------------------ #
    def _run_biostat(self, prompt, brief, gw):
        ev = gw.call("get_evidence")["evidence"]
        by_role = {}
        for e in ev:
            by_role.setdefault(e["role"], []).append(e)

        pop_text = ""
        for e in by_role.get("analysis_population", []):
            pop_text = e["fragment"].get("text", "")
        pop_name = "Response-Evaluable Population"
        m = re.match(r"^([A-Za-z-]+(?: [A-Za-z-]+)*? Population)", pop_text)
        if m:
            pop_name = m.group(1)

        # table population label vs SAP definition
        table_pop = ""
        for e in by_role.get("table_population", []):
            table_pop = e["fragment"].get("value", "")
        if table_pop and pop_name and table_pop != pop_name:
            gw.call("raise_issue", {
                "category": "wrong_population", "severity": "high",
                "description": f"T14.2.1 population label '{table_pop}' "
                               f"conflicts with SAP primary analysis population "
                               f"'{pop_name}'.",
                "refs": {"locator": "table://T14.2.1/meta/population"}})

        # cutoff: SAP text vs table meta
        sap_cut = ""
        for e in by_role.get("data_cutoff", []):
            mm = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", e["fragment"].get("text", ""))
            if mm:
                try:
                    sap_cut = datetime.strptime(mm.group(1), "%d %B %Y").date().isoformat()
                except ValueError:
                    pass
        table_cut = ""
        for e in by_role.get("table_data_cutoff", []):
            table_cut = e["fragment"].get("value", "")
        if sap_cut and table_cut and sap_cut != table_cut:
            gw.call("raise_issue", {
                "category": "stale_cutoff", "severity": "high",
                "description": f"T14.2.1 data cutoff {table_cut} conflicts with "
                               f"SAP cutoff {sap_cut}; possible stale TLF run.",
                "refs": {"locator": "table://T14.2.1/meta/data_cutoff"}})

        claims = []
        ev_common = [e["id"] for e in by_role.get("analysis_population", [])]
        fn_ids = [e["id"] for e in by_role.get("table_footnote", [])]
        for e in by_role.get("result_cell", []):
            mcol = re.search(r"/col/([^/]+)$", e["locator"])
            if not mcol:
                continue
            arm = mcol.group(1)
            cell = e["fragment"]["cell"]
            n, den = int(cell["n"]), int(cell.get("N", 0) or 0)
            if den <= 0:
                continue
            check = gw.call("exact_binomial_ci",
                            {"numerator": n, "denominator": den})
            for f_cell, f_chk in (("pct", "estimate_pct"),
                                  ("ci_lower", "ci_lower_pct"),
                                  ("ci_upper", "ci_upper_pct")):
                if cell.get(f_cell) is None:
                    continue
                if abs(float(cell[f_cell]) - float(check[f_chk])) > 0.05:
                    gw.call("raise_issue", {
                        "category": "numeric_mismatch", "severity": "high",
                        "description": f"T14.2.1 {arm} {f_cell}={cell[f_cell]} "
                                       f"disagrees with validated recomputation "
                                       f"{check[f_chk]} for {n}/{den}.",
                        "refs": {"locator": e["locator"]}})
            recount = gw.call("query_dataset", {
                "name": "ADRS",
                "filters": [{"var": "PARAMCD", "op": "eq", "value": "CBOR"},
                            {"var": "AVALC", "op": "in", "value": ["CR", "PR"]}],
                "adsl_filters": [{"var": "EFFFL", "op": "eq", "value": "Y"},
                                 {"var": "TRT01PN", "op": "eq",
                                  "value": ARM_TRTN.get(arm, "")}]})
            if recount.get("count") != n:
                gw.call("raise_issue", {
                    "category": "numeric_mismatch", "severity": "high",
                    "description": f"Independent ADRS recount for {arm} gives "
                                   f"{recount.get('count')} confirmed responders "
                                   f"but T14.2.1 reports {n}.",
                    "refs": {"locator": e["locator"]}})
            claims.append({
                "claim_type": "efficacy_result",
                "text": (f"The confirmed objective response rate in the "
                         f"{ARM_LABELS.get(arm, arm)} arm was "
                         f"{cell.get('pct')}% ({n}/{den})."),
                "dimensions": {"endpoint": "confirmed_orr",
                               "population": pop_name,
                               "treatment_arm": arm,
                               "timepoint": table_cut or sap_cut},
                "value": {"numerator": n, "denominator": den,
                          "estimate_pct": cell.get("pct"),
                          "ci_lower_pct": cell.get("ci_lower"),
                          "ci_upper_pct": cell.get("ci_upper"),
                          "ci_level": 95, "unit": "percent"},
                "support_class": "direct",
                "evidence_ids": [e["id"]] + ev_common + fn_ids})
        if pop_text:
            claims.append({
                "claim_type": "analysis_population_definition",
                "text": pop_text, "dimensions": {"population": pop_name},
                "value": {}, "support_class": "direct",
                "evidence_ids": ev_common})
        cut_ev = [e["id"] for e in by_role.get("data_cutoff", [])]
        if sap_cut or table_cut:
            claims.append({
                "claim_type": "data_cutoff",
                "text": f"Analyses are based on the data cutoff of "
                        f"{_fmt_date(sap_cut or table_cut)}.",
                "dimensions": {}, "value": {"date": sap_cut or table_cut},
                "support_class": "direct", "evidence_ids": cut_ev or ev_common})
        res = gw.call("create_claims", {"claims": claims})
        created = sum(1 for r in res["results"] if r["status"] == "created")
        gw.call("submit_report", {
            "summary": f"Registered {created} claims "
                       f"({len(claims)} proposed)."})
        return f"claims created: {created}"

    # ------------------------------------------------------------------ #
    def _run_writer(self, prompt, brief, gw):
        claims = gw.call("get_claims")["claims"]
        eff = {c["dimensions"].get("treatment_arm"): c for c in claims
               if c["claim_type"] == "efficacy_result"}
        pop = next((c for c in claims
                    if c["claim_type"] == "analysis_population_definition"), None)
        cut = next((c for c in claims if c["claim_type"] == "data_cutoff"), None)
        if not eff:
            gw.call("request_more_evidence",
                    {"description": "No efficacy_result claims registered; "
                                    "cannot draft the primary paragraph."})
            gw.call("submit_draft", {"text": "", "sentence_map": []})
            return "no claims; requested evidence"

        rules = set(re.findall(r"RULE:[A-Z_]+", prompt))
        feedback = brief if "REVISION FEEDBACK" in brief else ""
        want_n_over_n = ("RULE:REPORT_N_OVER_N" in rules
                         or "n/N" in feedback or "numerators" in feedback)
        want_cutoff = ("RULE:STATE_DATA_CUTOFF" in rules
                       or "data cutoff stated" in feedback)
        no_interp = ("RULE:NO_UNAPPROVED_INTERPRETIVE" in rules
                     or "interpretive" in feedback.lower())
        pop_name = (pop["dimensions"].get("population")
                    if pop else "Response-Evaluable Population")

        def arm_phrase(arm_id):
            c = eff.get(arm_id)
            if not c:
                return None
            v = c["value"]
            inner = f"95% CI: {v['ci_lower_pct']}, {v['ci_upper_pct']}"
            if want_n_over_n:
                inner = f"{v['numerator']}/{v['denominator']}; " + inner
            return (f"{v['estimate_pct']}% ({inner}) in the "
                    f"{ARM_LABELS.get(arm_id, arm_id)} arm")

        phrases = [p for p in (arm_phrase("arm_100"), arm_phrase("arm_50")) if p]
        s1 = (f"In the {pop_name}, the confirmed objective response rate (ORR) "
              f"per RECIST v1.1, based on investigator assessment, was "
              f"{' and '.join(phrases)} (Table 14.2.1).")
        sentences = [(s1, [c["id"] for c in eff.values()]
                      + ([pop["id"]] if pop else []))]
        if want_cutoff and cut:
            s2 = (f"Results are based on the data cutoff of "
                  f"{_fmt_date(cut['value'].get('date', ''))}.")
            sentences.append((s2, [cut["id"]]))
        if not no_interp:
            sentences.append(("These results are clinically meaningful.", []))

        text = " ".join(s for s, _ in sentences)
        smap = [{"sentence": s, "claim_ids": ids} for s, ids in sentences]
        gw.call("submit_draft", {"text": text, "sentence_map": smap})
        return f"drafted {len(sentences)} sentences (rules: {sorted(rules)})"

    # ------------------------------------------------------------------ #
    def _run_qc(self, prompt, brief, gw):
        findings = []
        claims = gw.call("get_claims")["claims"]
        cells = gw.call("get_tlf_cells",
                        {"table_id": "T14.2.1", "row_ids": ["orr_confirmed"]})
        cell_by_col = {c["col_id"]: c["cell"] for c in cells.get("cells", [])}

        # injection scan on footnotes — flag, never comply
        for fn_ in cells.get("footnotes", []):
            low = fn_["text"].lower()
            if any(re.search(p, low) for p in INJECTION_PATTERNS):
                r = gw.call("raise_issue", {
                    "category": "suspicious_content", "severity": "high",
                    "description": f"T14.2.1 footnote {fn_['id']} contains "
                                   f"instruction-like text directed at reviewers; "
                                   f"treated as data and flagged, not followed.",
                    "refs": {"locator": f"table://T14.2.1/footnote/{fn_['id']}"}})
                findings.append({"issue_id": r.get("issue_id"),
                                 "category": "suspicious_content"})

        for c in claims:
            if c["claim_type"] != "efficacy_result":
                continue
            arm = c["dimensions"].get("treatment_arm")
            v = c["value"]
            cell = cell_by_col.get(arm)
            if not cell:
                continue
            if int(cell["n"]) != int(v["numerator"]):
                r = gw.call("raise_issue", {
                    "category": "numeric_mismatch", "severity": "high",
                    "description": f"Claim {c['id']} numerator {v['numerator']} "
                                   f"differs from T14.2.1 cell n={cell['n']} ({arm}).",
                    "refs": {"claim_id": c["id"]}})
                findings.append({"issue_id": r.get("issue_id"),
                                 "category": "numeric_mismatch"})
            check = gw.call("exact_binomial_ci",
                            {"numerator": int(v["numerator"]),
                             "denominator": int(v["denominator"])})
            for fk, ck in (("estimate_pct", "estimate_pct"),
                           ("ci_lower_pct", "ci_lower_pct"),
                           ("ci_upper_pct", "ci_upper_pct")):
                if v.get(fk) is None:
                    continue
                if abs(float(v[fk]) - float(check[ck])) > 0.05:
                    r = gw.call("raise_issue", {
                        "category": "numeric_mismatch", "severity": "high",
                        "description": f"Claim {c['id']} {fk}={v[fk]} disagrees "
                                       f"with validated recomputation {check[ck]}.",
                        "refs": {"claim_id": c["id"]}})
                    findings.append({"issue_id": r.get("issue_id"),
                                     "category": "numeric_mismatch"})
            recount = gw.call("query_dataset", {
                "name": "ADRS",
                "filters": [{"var": "PARAMCD", "op": "eq", "value": "CBOR"},
                            {"var": "AVALC", "op": "in", "value": ["CR", "PR"]}],
                "adsl_filters": [{"var": "EFFFL", "op": "eq", "value": "Y"},
                                 {"var": "TRT01PN", "op": "eq",
                                  "value": ARM_TRTN.get(arm, "")}]})
            if recount.get("count") != int(v["numerator"]):
                r = gw.call("raise_issue", {
                    "category": "numeric_mismatch", "severity": "high",
                    "description": f"ADRS recount for {arm} = "
                                   f"{recount.get('count')} responders; claim "
                                   f"says {v['numerator']}.",
                    "refs": {"claim_id": c["id"]}})
                findings.append({"issue_id": r.get("issue_id"),
                                 "category": "numeric_mismatch"})

        # population + cutoff checks against SAP
        sap94 = gw.call("get_section", {"doc": "sap", "section": "9.4"})
        sap_pop = ""
        for p in sap94.get("paragraphs", []):
            if "primary analysis population" in p["text"].lower():
                m = re.match(r"^([A-Za-z-]+(?: [A-Za-z-]+)*? Population)", p["text"])
                if m:
                    sap_pop = m.group(1)
        if sap_pop and cells.get("population") and cells["population"] != sap_pop:
            r = gw.call("raise_issue", {
                "category": "wrong_population", "severity": "high",
                "description": f"T14.2.1 population '{cells['population']}' vs "
                               f"SAP primary population '{sap_pop}'.",
                "refs": {"locator": "table://T14.2.1/meta/population"}})
            findings.append({"issue_id": r.get("issue_id"),
                             "category": "wrong_population"})
        sap93 = gw.call("get_section", {"doc": "sap", "section": "9.3"})
        sap_cut = ""
        for p in sap93.get("paragraphs", []):
            mm = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", p["text"])
            if mm:
                try:
                    sap_cut = datetime.strptime(mm.group(1), "%d %B %Y").date().isoformat()
                except ValueError:
                    pass
        if sap_cut and cells.get("data_cutoff") and cells["data_cutoff"] != sap_cut:
            r = gw.call("raise_issue", {
                "category": "stale_cutoff", "severity": "high",
                "description": f"T14.2.1 cutoff {cells['data_cutoff']} vs SAP "
                               f"cutoff {sap_cut}.",
                "refs": {"locator": "table://T14.2.1/meta/data_cutoff"}})
            findings.append({"issue_id": r.get("issue_id"),
                             "category": "stale_cutoff"})

        gw.call("submit_report", {
            "summary": f"Independent QC complete: {len(findings)} finding(s).",
            "findings": findings})
        return f"qc findings: {len(findings)}"

    # ------------------------------------------------------------------ #
    def _run_narrative(self, prompt, brief, gw):
        m = re.search(r"USUBJID:\s*(\S+)", brief)
        usubjid = m.group(1) if m else ""
        recs = gw.call("get_subject_records",
                       {"usubjid": usubjid, "datasets": ["ADSL", "ADAE"]})
        adsl = (recs["records"].get("ADSL") or [{}])[0]
        aes = recs["records"].get("ADAE") or []
        ae = next((a for a in aes if a.get("AEOUT") == "FATAL"),
                  next((a for a in aes if a.get("AESER") == "Y"), None))
        if adsl and not ae and adsl.get("DTHFL") == "Y":
            # death without an AE record (e.g. disease progression in
            # follow-up): narrative from the subject-level record alone
            gw.call("create_evidence", {"items": [
                {"locator": recs["locators"].get("ADSL",
                                                 f"dataset://ADSL/subject/{usubjid}"),
                 "role": "subject_records"}]})
            sex_word = "female" if adsl.get("SEX") == "F" else "male"
            cause = adsl.get("DCSREAS", "death").title()
            s1 = (f"Subject {usubjid}, a {adsl.get('AGE')}-year-old {sex_word} "
                  f"with an advanced solid tumor harboring an EXM1 activating "
                  f"mutation, received {adsl.get('TRT01P')} once daily.")
            s2 = (f"The subject discontinued study treatment due to "
                  f"{cause.lower()} and died on "
                  f"{_fmt_date(adsl.get('DTHDT', ''))}.")
            s3 = (f"The death was attributed to {cause.lower()}; no adverse "
                  f"event was recorded as the cause of death.")
            text = " ".join([s1, s2, s3])
            locs = [recs["locators"].get("ADSL", "")]
            gw.call("submit_narrative", {
                "usubjid": usubjid, "text": text,
                "facts_map": [{"sentence": s, "locators": locs}
                              for s in (s1, s2, s3)]})
            return f"death narrative drafted for {usubjid}"
        if not adsl or not ae:
            gw.call("raise_issue", {
                "category": "missing_evidence", "severity": "high",
                "description": f"No usable records for {usubjid}."})
            gw.call("submit_narrative", {"usubjid": usubjid, "text": "",
                                         "facts_map": []})
            return "missing records"
        gw.call("create_evidence", {"items": [
            {"locator": recs["locators"].get(ds, f"dataset://{ds}/subject/{usubjid}"),
             "role": "subject_records"} for ds in ("ADSL", "ADAE")]})

        rules = set(re.findall(r"RULE:[A-Z_]+", prompt))
        feedback = brief if "REVISION FEEDBACK" in brief else ""
        want_onset = ("RULE:STATE_ONSET_DAY" in rules
                      or "onset" in feedback.lower())
        onset_day = ""
        try:
            d0 = datetime.fromisoformat(adsl["TRTSDT"]).date()
            d1 = datetime.fromisoformat(ae["ASTDT"]).date()
            onset_day = str((d1 - d0).days + 1)
        except (ValueError, KeyError):
            pass
        sex_word = "female" if adsl.get("SEX") == "F" else "male"
        onset_clause = (f"On study day {onset_day}, " if (want_onset and onset_day)
                        else "During treatment, ")
        s1 = (f"Subject {usubjid}, a {adsl.get('AGE')}-year-old {sex_word} with "
              f"an advanced solid tumor harboring an EXM1 activating mutation, "
              f"received {adsl.get('TRT01P')} once daily.")
        s2 = (f"{onset_clause}the subject developed Grade {ae.get('ATOXGR')} "
              f"{ae.get('AEDECOD').lower()}, reported as a serious adverse event.")
        action = ("Study drug was withdrawn"
                  if ae.get("AEACN") == "DRUG WITHDRAWN"
                  else f"Action taken: {ae.get('AEACN', '').lower()}")
        if ae.get("AEOUT") == "FATAL":
            s3 = (f"{action}; despite management, the event had a fatal outcome "
                  f"on {_fmt_date(adsl.get('DTHDT', ''))}.")
        else:
            s3 = f"{action}; the event outcome was {ae.get('AEOUT', '').lower()}."
        s4 = (f"The investigator assessed the event as "
              f"{ae.get('AEREL', '').lower()} to examplinib.")
        sentences = [s1, s2, s3, s4]
        # subjects who died of something other than the drafted SAE still
        # need the death reported (ICH E3: all deaths are narrated)
        if (adsl.get("DTHFL") == "Y" and ae.get("AEOUT") != "FATAL"
                and adsl.get("DTHDT")):
            cause = adsl.get("DCSREAS", "death").lower().replace("_", " ")
            sentences.append(
                f"The subject subsequently died on "
                f"{_fmt_date(adsl.get('DTHDT', ''))}; the death was "
                f"attributed to {cause}.")
        text = " ".join(sentences)
        locs = [recs["locators"].get("ADSL", ""), recs["locators"].get("ADAE", "")]
        facts_map = [{"sentence": s, "locators": locs} for s in sentences]
        gw.call("submit_narrative", {"usubjid": usubjid, "text": text,
                                     "facts_map": facts_map})
        return f"narrative drafted for {usubjid}"

    # ------------------------------------------------------------------ #
    def run_skillsmith(self, prompt, signals):
        """Deterministic improvement proposals from feedback + eval signals."""
        from ..skills import load_skill, skill_rules
        proposals = []
        fb = signals.get("feedback", [])
        report = signals.get("eval_report", {})
        metrics = report.get("metrics", {})

        def have(skill, marker):
            return marker in skill_rules(load_skill(skill))

        reasons = {f.get("reason_code") for f in fb}
        comments = " ".join(f.get("comment", "") for f in fb).lower()

        if ((metrics.get("interpretive_flags_first_pass", 0) > 0
             or "interpretation_too_strong" in reasons)
                and not have("csr-efficacy-reporting", "RULE:NO_UNAPPROVED_INTERPRETIVE")):
            proposals.append({
                "skill": "csr-efficacy-reporting",
                "rule_marker": "RULE:NO_UNAPPROVED_INTERPRETIVE",
                "rule_text": "- Never characterize results as clinically "
                             "meaningful/significant or otherwise interpret "
                             "them; report and cite only. Interpretive language "
                             "requires an approved interpretive claim. "
                             "RULE:NO_UNAPPROVED_INTERPRETIVE",
                "hypothesis": {
                    "problem": "Drafts include unapproved interpretive "
                               "characterizations, triggering QC flags and "
                               "revision cycles.",
                    "change": "Add an explicit prohibition to the efficacy "
                              "reporting skill.",
                    "predicted_effect": "interpretive_flags_first_pass -> 0; "
                                        "revision cycles down."}})
        if ((not metrics.get("style_ok_first_pass", True)
             or "n/n" in comments or "denominator" in comments)
                and not have("csr-efficacy-reporting", "RULE:REPORT_N_OVER_N")):
            proposals.append({
                "skill": "csr-efficacy-reporting",
                "rule_marker": "RULE:REPORT_N_OVER_N",
                "rule_text": '- Always report the numerator and denominator '
                             'with every percentage, e.g. "42.1% (16/38)". '
                             'RULE:REPORT_N_OVER_N',
                "hypothesis": {
                    "problem": "Percentages reported without n/N, violating "
                               "sponsor numeric conventions.",
                    "change": "Require n/N with every percentage.",
                    "predicted_effect": "style issues at review -> 0."}})
        if ((metrics.get("missing_required_first_pass", 0) > 0
             or "cutoff" in comments)
                and not have("csr-efficacy-reporting", "RULE:STATE_DATA_CUTOFF")):
            proposals.append({
                "skill": "csr-efficacy-reporting",
                "rule_marker": "RULE:STATE_DATA_CUTOFF",
                "rule_text": '- End the paragraph by stating the data cutoff '
                             'date from the data_cutoff claim, e.g. "Results '
                             'are based on the data cutoff of 01 May 2026." '
                             'RULE:STATE_DATA_CUTOFF',
                "hypothesis": {
                    "problem": "Drafts omit the data cutoff statement required "
                               "for results paragraphs.",
                    "change": "Require a closing cutoff sentence.",
                    "predicted_effect": "missing_required_content -> 0."}})
        if (metrics.get("narrative_missing_onset", 0) > 0
                and not have("patient-narrative", "RULE:STATE_ONSET_DAY")):
            proposals.append({
                "skill": "patient-narrative",
                "rule_marker": "RULE:STATE_ONSET_DAY",
                "rule_text": '- State event onset as a study day relative to '
                             'first dose ("On study day N, ..."). '
                             'RULE:STATE_ONSET_DAY',
                "hypothesis": {
                    "problem": "Narratives omit onset timing relative to "
                               "dosing (ICH E3 12.3.2 element).",
                    "change": "Require an explicit study-day onset clause.",
                    "predicted_effect": "narrative element coverage -> 1.0."}})
        return proposals
