"""Governed clinical tool gateway.

Every agent capability is a narrow, purpose-built tool executed HOST-SIDE by
this gateway — never inside the model's sandbox. The gateway enforces the
task's capability grant, verifies evidence locators against the frozen
snapshot, applies authority rules to claims, and writes a receipt for every
call. A denied call is recorded as a capability violation (a hard eval gate).
"""

from __future__ import annotations

import hashlib
import json
import re

from .domain import (Claim, EvidenceItem, Issue, Proposal, ToolReceipt,
                     new_id, now_iso, sha256_text, SUPPORT_CLASSES,
                     ISSUE_CATEGORIES, SEVERITIES)
from .sources import Snapshot
from .stats import orr_with_ci
from .store import Store


def _digest(obj) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _fragment_leaves(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _fragment_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _fragment_leaves(v)
    else:
        yield obj


def _expected_in_fragment(expected: str, fragment: dict) -> bool:
    """True when the agent's transcription matches the source fragment:
    exact equality with a leaf value, or a token-boundary occurrence inside
    a text leaf. A raw substring test would falsely verify '3' against 38
    and falsely reject non-ASCII text against escaped JSON."""
    expected = str(expected).strip()
    if not expected:
        return False
    pat = re.compile(rf"(?<![\w.]){re.escape(expected)}(?![\w.])")
    for leaf in _fragment_leaves(fragment):
        s = str(leaf)
        if expected == s:
            return True
        if isinstance(leaf, str) and pat.search(s):
            return True
    return False


def _source_type(artifact: str) -> str:
    if artifact in ("protocol", "sap", "csr_shell"):
        return artifact
    if artifact.startswith("T14"):
        return "tlf"
    if artifact in ("ADSL", "ADRS", "ADAE"):
        return "dataset"
    return "unknown"


class ToolDenied(Exception):
    pass


class ToolGateway:
    """One gateway instance per agent run, bound to a capability grant."""

    def __init__(self, store: Store, snapshot: Snapshot, task_id: str,
                 run_id: str, agent: str, allowed_tools: set[str]):
        self.store = store
        self.snapshot = snapshot
        self.task_id = task_id
        self.run_id = run_id
        self.agent = agent
        self.allowed = set(allowed_tools)
        self.violations: list[dict] = []
        self.finished = False
        self.final_output: dict | None = None
        self.calls = 0

    # ------------------------------------------------------------------ #
    def call(self, tool: str, tool_input: dict | None = None) -> dict:
        tool_input = tool_input or {}
        """Dispatch a tool call under the capability grant. Always returns a
        JSON-serializable dict; errors come back as {"error": ...}."""
        self.calls += 1
        receipt = ToolReceipt(id=new_id("rcpt"), task_id=self.task_id,
                              run_id=self.run_id, tool=tool,
                              tool_input=tool_input, output_digest="",
                              allowed=True)
        if tool not in self.allowed:
            receipt.allowed = False
            receipt.error = "capability_denied"
            self.store.add("receipts", receipt)
            self.violations.append({"tool": tool, "input": tool_input})
            self.store.audit("capability.denied", self.task_id,
                             {"agent": self.agent, "run": self.run_id},
                             {"tool": tool})
            return {"error": f"Tool '{tool}' is not in this task's capability "
                             f"grant. This attempt has been logged."}
        fn = getattr(self, f"t_{tool}", None)
        if fn is None:
            receipt.allowed = False
            receipt.error = "unknown_tool"
            self.store.add("receipts", receipt)
            return {"error": f"Unknown tool '{tool}'."}
        try:
            out = fn(**tool_input)
        except TypeError as e:
            out = {"error": f"Bad arguments for {tool}: {e}"}
        except (KeyError, ValueError) as e:
            out = {"error": str(e)}
        receipt.output_digest = _digest(out)
        self.store.add("receipts", receipt)
        return out

    # ----------------------------- read tools ------------------------- #
    def t_list_sources(self) -> dict:
        man = self.snapshot.manifest()
        return {"study_id": man["study_id"],
                "documents": ["protocol", "sap", "csr_shell"],
                "tables": sorted(self.snapshot.tables.keys()),
                "datasets": sorted(self.snapshot.datasets.keys()),
                "artifact_hashes": man["artifacts"]}

    def t_get_section(self, doc: str, section: str) -> dict:
        sec = self.snapshot.docs.get(doc, {}).get(section)
        if not sec:
            raise KeyError(f"section {section} not found in {doc}")
        study = self.snapshot.study["study_id"]
        return {"doc": doc, "section": section, "title": sec["title"],
                "paragraphs": [
                    {"locator": f"artifact://{study}/{doc}/section/{section}/paragraph/{i+1}",
                     "text": p}
                    for i, p in enumerate(sec["paragraphs"])]}

    def t_find_sections(self, doc: str, query: str) -> dict:
        terms = [t.lower() for t in query.split() if len(t) > 2]
        hits = []
        for sid, sec in self.snapshot.docs.get(doc, {}).items():
            blob = (sec["title"] + " " + " ".join(sec["paragraphs"])).lower()
            score = sum(blob.count(t) for t in terms)
            if score > 0:
                hits.append({"section": sid, "title": sec["title"], "score": score})
        hits.sort(key=lambda h: -h["score"])
        return {"doc": doc, "matches": hits[:8],
                "note": "Search results are pointers, not provenance. Read the "
                        "section and register evidence with exact locators."}

    def t_get_tlf_overview(self, table_id: str) -> dict:
        return self.snapshot.table_overview(table_id)

    def t_get_tlf_cells(self, table_id: str, row_ids: list | None = None,
                        col_ids: list | None = None) -> dict:
        t = self.snapshot.tables.get(table_id)
        if not t:
            raise KeyError(f"unknown table {table_id}")
        out = []
        for row in t["rows"]:
            if row_ids and row["id"] not in row_ids:
                continue
            for col_id, cell in row["cells"].items():
                if col_ids and col_id not in col_ids:
                    continue
                out.append({
                    "locator": f"table://{table_id}/row/{row['id']}/col/{col_id}",
                    "row_id": row["id"], "row_label": row["label"],
                    "col_id": col_id, "cell": cell})
        return {"table_id": table_id, "population": t["population"],
                "data_cutoff": t["data_cutoff"],
                "footnotes": t["footnotes"], "cells": out}

    def t_get_dataset_meta(self, name: str) -> dict:
        r = self.snapshot.resolve(f"dataset://{name}/meta")
        return {"dataset": name, **r["fragment"]["value"]}

    def t_query_dataset(self, name: str, filters: list | None = None,
                        distinct_subjects: bool = True,
                        adsl_filters: list | None = None) -> dict:
        return self.snapshot.query_dataset(name, filters, distinct_subjects,
                                           adsl_filters)

    def t_get_subject_records(self, usubjid: str,
                              datasets: list | None = None) -> dict:
        datasets = datasets or ["ADSL", "ADAE"]
        out = {"usubjid": usubjid, "records": {}, "locators": {}}
        for ds in datasets:
            loc = f"dataset://{ds}/subject/{usubjid}"
            try:
                r = self.snapshot.resolve(loc)
                out["records"][ds] = r["fragment"]["records"]
                out["locators"][ds] = loc
            except KeyError:
                out["records"][ds] = []
        return out

    def t_exact_binomial_ci(self, numerator: int, denominator: int,
                            conf: float = 0.95) -> dict:
        res = orr_with_ci(int(numerator), int(denominator), conf)
        res["calculator"] = "opencsr.stats.orr_with_ci"
        return res

    # --------------------------- write tools --------------------------- #
    def t_create_evidence(self, items: list) -> dict:
        """Register evidence. Each item: {locator, role, expected?}. The
        gateway re-resolves the locator against the frozen snapshot; if the
        agent supplied an `expected` transcription it must match the source
        fragment or the item is rejected (transcription-error guard)."""
        results = []
        for item in items:
            loc = item.get("locator", "")
            try:
                resolved = self.snapshot.resolve(loc)
            except KeyError as e:
                results.append({"locator": loc, "status": "rejected",
                                "reason": str(e)})
                continue
            verified = True
            reason = ""
            expected = item.get("expected")
            if expected is not None:
                if not _expected_in_fragment(str(expected),
                                             resolved["fragment"]):
                    verified = False
                    reason = ("expected value not found in source fragment; "
                              "possible transcription error")
            ev = EvidenceItem(
                id=new_id("ev"), task_id=self.task_id, locator=loc,
                artifact=resolved["artifact"],
                artifact_hash=resolved["artifact_hash"],
                fragment=resolved["fragment"], role=item.get("role", ""),
                created_by=self.agent, verified=verified)
            self.store.add("evidence", ev)
            results.append({"locator": loc, "evidence_id": ev.id,
                            "status": "verified" if verified else "mismatch",
                            **({"reason": reason} if reason else {})})
        return {"results": results}

    def t_get_evidence(self) -> dict:
        rows = self.store.by_task("evidence", self.task_id)
        return {"evidence": [
            {"id": r["id"], "locator": r["locator"], "artifact": r["artifact"],
             "role": r["role"], "verified": bool(r["verified"]),
             "fragment": r["fragment"]} for r in rows]}

    def t_create_claims(self, claims: list) -> dict:
        rules = {r["claim_type"]: r for r in self.snapshot.authority["rules"]}
        known_ev = {r["id"]: r for r in self.store.by_task("evidence", self.task_id)}
        results = []
        for c in claims:
            support = c.get("support_class", "unsupported")
            if support not in SUPPORT_CLASSES:
                results.append({"status": "rejected",
                                "reason": f"bad support_class {support}"})
                continue
            ev_ids = c.get("evidence_ids", [])
            missing = [e for e in ev_ids if e not in known_ev]
            if missing:
                results.append({"status": "rejected",
                                "reason": f"unknown evidence ids {missing}"})
                continue
            unverified = [e for e in ev_ids if not known_ev[e]["verified"]]
            if unverified and support in ("direct", "derived"):
                results.append({"status": "rejected",
                                "reason": f"evidence not verified: {unverified}"})
                continue
            rule = rules.get(c.get("claim_type", ""))
            authority_note = ""
            if rule is not None and rule["permitted_sources"] and ev_ids:
                types = [_source_type(known_ev[e]["artifact"]) for e in ev_ids]
                permitted_present = any(t in rule["permitted_sources"]
                                        for t in types)
                extra = sorted({t for t in types
                                if t not in rule["permitted_sources"]})
                if not permitted_present and support == "direct":
                    results.append({
                        "status": "rejected",
                        "reason": (f"authority rule for {c['claim_type']} requires "
                                   f"support from {rule['permitted_sources']}; "
                                   f"got only {extra}")})
                    continue
                if extra:
                    authority_note = f"supplementary non-primary sources: {extra}"
            if support in ("direct", "derived") and not ev_ids:
                results.append({"status": "rejected",
                                "reason": "direct/derived claims require evidence"})
                continue
            claim = Claim(
                id=new_id("clm"), task_id=self.task_id,
                claim_type=c.get("claim_type", "other"), text=c.get("text", ""),
                dimensions=c.get("dimensions", {}), value=c.get("value", {}),
                support_class=support, evidence_ids=ev_ids,
                created_by=self.agent)
            self.store.add("claims", claim)
            res = {"status": "created", "claim_id": claim.id}
            if authority_note:
                res["note"] = authority_note
            results.append(res)
        return {"results": results}

    def t_get_claims(self) -> dict:
        rows = self.store.by_task("claims", self.task_id)
        return {"claims": [
            {"id": r["id"], "claim_type": r["claim_type"], "text": r["text"],
             "dimensions": r["dimensions"], "value": r["value"],
             "support_class": r["support_class"],
             "evidence_ids": r["evidence_ids"], "status": r["status"]}
            for r in rows]}

    def t_raise_issue(self, category: str, severity: str, description: str,
                      refs: dict | None = None) -> dict:
        if category not in ISSUE_CATEGORIES:
            category = "other"
        if severity not in SEVERITIES:
            severity = "medium"
        issue = Issue(id=new_id("iss"), task_id=self.task_id, category=category,
                      severity=severity, description=description,
                      refs=refs or {}, raised_by=self.agent)
        self.store.add("issues", issue)
        return {"issue_id": issue.id, "status": "open"}

    def t_request_more_evidence(self, description: str) -> dict:
        issue = Issue(id=new_id("iss"), task_id=self.task_id,
                      category="missing_evidence", severity="medium",
                      description=description, refs={}, raised_by=self.agent)
        self.store.add("issues", issue)
        return {"issue_id": issue.id,
                "note": "Evidence request logged; the runtime will decide "
                        "whether to re-run evidence collection."}

    # ------------------------- finalization tools ----------------------- #
    def t_submit_draft(self, text: str, sentence_map: list) -> dict:
        node = "csr/11.4.1"
        latest = self.store.latest_document(node)
        base_version = latest["version"] if latest else 0
        base_hash = latest["text_hash"] if latest else sha256_text("")
        prior = [p for p in self.store.by_task("proposals", self.task_id)]
        proposal = Proposal(
            id=new_id("prop"), task_id=self.task_id, target_node=node,
            base_version=base_version, base_hash=base_hash, text=text,
            sentence_map=sentence_map, revision=len(prior))
        self.store.add("proposals", proposal)
        self.finished = True
        self.final_output = {"proposal_id": proposal.id}
        return {"proposal_id": proposal.id, "status": "draft_recorded"}

    def t_submit_narrative(self, usubjid: str, text: str,
                           facts_map: list) -> dict:
        node = f"csr/12.3.2/{usubjid}"
        latest = self.store.latest_document(node)
        proposal = Proposal(
            id=new_id("prop"), task_id=self.task_id, target_node=node,
            base_version=latest["version"] if latest else 0,
            base_hash=latest["text_hash"] if latest else sha256_text(""),
            text=text, sentence_map=facts_map)
        self.store.add("proposals", proposal)
        self.finished = True
        self.final_output = {"proposal_id": proposal.id}
        return {"proposal_id": proposal.id, "status": "narrative_recorded"}

    def t_submit_report(self, summary: str, findings: list | None = None) -> dict:
        self.finished = True
        self.final_output = {"summary": summary, "findings": findings or []}
        return {"status": "report_recorded"}


# ---------------------------------------------------------------------------
# Tool definitions (JSON schema) — used verbatim for Managed Agents configs.
# ---------------------------------------------------------------------------

def _obj(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}

TOOL_DEFS: dict[str, dict] = {
    "list_sources": {
        "description": "List the frozen source snapshot for this task: documents, TLF tables, datasets, and their content hashes.",
        "input_schema": _obj({}, [])},
    "get_section": {
        "description": "Read one numbered section of a source document. Returns each paragraph with its exact locator.",
        "input_schema": _obj({
            "doc": {"type": "string", "enum": ["protocol", "sap", "csr_shell"]},
            "section": {"type": "string", "description": "Section number, e.g. '9.7.1'"}},
            ["doc", "section"])},
    "find_sections": {
        "description": "Keyword-search a document's sections. Results are pointers only — never provenance. Follow up with get_section.",
        "input_schema": _obj({
            "doc": {"type": "string", "enum": ["protocol", "sap", "csr_shell"]},
            "query": {"type": "string"}}, ["doc", "query"])},
    "get_tlf_overview": {
        "description": "Get a TLF table's title, population, columns, row ids, footnotes, and data cutoff (no cell values).",
        "input_schema": _obj({"table_id": {"type": "string"}}, ["table_id"])},
    "get_tlf_cells": {
        "description": "Read exact TLF cells with locators. Optionally filter by row_ids/col_ids.",
        "input_schema": _obj({
            "table_id": {"type": "string"},
            "row_ids": {"type": "array", "items": {"type": "string"}},
            "col_ids": {"type": "array", "items": {"type": "string"}}},
            ["table_id"])},
    "get_dataset_meta": {
        "description": "Get row count and variable list for an analysis dataset (ADSL, ADRS, ADAE).",
        "input_schema": _obj({"name": {"type": "string"}}, ["name"])},
    "query_dataset": {
        "description": "Constrained count-only dataset query for cross-checks. Filters: [{var, op(eq|ne|in|gte), value}]. `adsl_filters` restricts to subjects matching ADSL filters (population/arm joins). Counts distinct subjects by default.",
        "input_schema": _obj({
            "name": {"type": "string"},
            "filters": {"type": "array", "items": {"type": "object"}},
            "adsl_filters": {"type": "array", "items": {"type": "object"}},
            "distinct_subjects": {"type": "boolean"}}, ["name"])},
    "get_subject_records": {
        "description": "Get all dataset records for one subject (for narrative drafting). Returns records plus locators.",
        "input_schema": _obj({
            "usubjid": {"type": "string"},
            "datasets": {"type": "array", "items": {"type": "string"}}},
            ["usubjid"])},
    "exact_binomial_ci": {
        "description": "Validated calculator: exact (Clopper-Pearson) binomial CI for a response rate. The ONLY permitted way to derive a CI.",
        "input_schema": _obj({
            "numerator": {"type": "integer"}, "denominator": {"type": "integer"},
            "conf": {"type": "number"}}, ["numerator", "denominator"])},
    "create_evidence": {
        "description": "Register evidence items by exact locator. Include `expected` (a value or phrase you transcribed) so the gateway can verify it against the source; mismatches are rejected.",
        "input_schema": _obj({
            "items": {"type": "array", "items": _obj({
                "locator": {"type": "string"},
                "role": {"type": "string"},
                "expected": {"type": "string"}}, ["locator", "role"])}},
            ["items"])},
    "get_evidence": {
        "description": "List the evidence registered for this task.",
        "input_schema": _obj({}, [])},
    "create_claims": {
        "description": "Register typed claims. Each: {claim_type, text, dimensions, value, support_class(direct|derived|interpretive), evidence_ids}. Authority rules and evidence verification are enforced.",
        "input_schema": _obj({
            "claims": {"type": "array", "items": {"type": "object"}}}, ["claims"])},
    "get_claims": {
        "description": "List the claims registered for this task.",
        "input_schema": _obj({}, [])},
    "raise_issue": {
        "description": "Raise a QC/consistency issue. category in numeric_mismatch|wrong_population|wrong_arm|stale_cutoff|unsupported_claim|missing_required_content|terminology|interpretive_overreach|source_conflict|suspicious_content|missing_evidence|style|other; severity low|medium|high.",
        "input_schema": _obj({
            "category": {"type": "string"}, "severity": {"type": "string"},
            "description": {"type": "string"}, "refs": {"type": "object"}},
            ["category", "severity", "description"])},
    "request_more_evidence": {
        "description": "Ask the runtime for additional evidence instead of browsing sources yourself.",
        "input_schema": _obj({"description": {"type": "string"}}, ["description"])},
    "submit_draft": {
        "description": "Submit the final draft paragraph with a sentence-to-claim map: [{sentence, claim_ids}]. Every factual sentence must map to claims. Call exactly once, when done.",
        "input_schema": _obj({
            "text": {"type": "string"},
            "sentence_map": {"type": "array", "items": {"type": "object"}}},
            ["text", "sentence_map"])},
    "submit_narrative": {
        "description": "Submit a patient safety narrative with a facts map: [{sentence, locators}]. Call exactly once, when done.",
        "input_schema": _obj({
            "usubjid": {"type": "string"}, "text": {"type": "string"},
            "facts_map": {"type": "array", "items": {"type": "object"}}},
            ["usubjid", "text"])},
    "submit_report": {
        "description": "Submit your final structured report: {summary, findings:[...]}. Call exactly once, when your review/collection is complete.",
        "input_schema": _obj({
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "object"}}},
            ["summary"])},
}


def tool_defs_for(names: set[str] | list[str]) -> list[dict]:
    """Managed-Agents-shaped custom tool definitions for an agent config."""
    return [{"type": "custom", "name": n,
             "description": TOOL_DEFS[n]["description"],
             "input_schema": TOOL_DEFS[n]["input_schema"]}
            for n in names]
