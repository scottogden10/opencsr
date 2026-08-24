"""Clinical artifact plane: frozen source snapshots and exact locators.

A Snapshot is an immutable, hash-stamped view of the study sources a task
runs against. Locators address exact fragments:

    artifact://OCS-101/protocol/section/6.1/paragraph/1
    table://T14.2.1/row/orr_confirmed/col/arm_100
    table://T14.2.1/footnote/d
    table://T14.2.1/meta/population
    dataset://ADSL/meta

Vector-ish search never counts as provenance here — evidence is only valid
when its locator re-resolves to the exact fragment recorded.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
from pathlib import Path

TRIAL_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic_trial"


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _parse_markdown(text: str) -> dict:
    """Split a source doc into numbered sections with numbered paragraphs."""
    sections: dict[str, dict] = {}
    current_id, current_title, buf = "0", "Preamble", []

    def flush():
        paras = [p.strip() for p in re.split(r"\n\s*\n", "\n".join(buf)) if p.strip()]
        sections[current_id] = {"title": current_title, "paragraphs": paras}

    for line in text.splitlines():
        m = re.match(r"^##\s+([\d.]+)\s+(.*)$", line)
        if m:
            flush()
            current_id, current_title, buf = m.group(1), m.group(2).strip(), []
        else:
            buf.append(line)
    flush()
    return sections


def _load_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class Snapshot:
    """A frozen set of study sources, optionally with a seeded fault applied."""

    def __init__(self, trial_dir: Path = TRIAL_DIR, fault_id: str | None = None):
        self.trial_dir = Path(trial_dir)
        self.fault_id = fault_id
        self.study = json.loads((self.trial_dir / "study.json").read_text())
        self.authority = json.loads((self.trial_dir / "authority.json").read_text())
        self.terminology = json.loads((self.trial_dir / "terminology.json").read_text())

        self.docs: dict[str, dict] = {}
        self.doc_raw: dict[str, str] = {}
        for name in ("protocol", "sap", "csr_shell"):
            raw = (self.trial_dir / f"{name}.md").read_text()
            self.doc_raw[name] = raw
            self.docs[name] = _parse_markdown(raw)

        self.tables: dict[str, dict] = {}
        for p in sorted((self.trial_dir / "tlf").glob("*.json")):
            t = json.loads(p.read_text())
            self.tables[t["table_id"]] = t

        self.datasets: dict[str, list[dict]] = {}
        for p in sorted((self.trial_dir / "datasets").glob("*.csv")):
            self.datasets[p.stem] = _load_csv(p)

        if fault_id:
            self._apply_fault(fault_id)
        self.hashes = self._compute_hashes()

    # -- faults ------------------------------------------------------------
    def _apply_fault(self, fault_id: str):
        fp = self.trial_dir / "seeded_faults" / f"{fault_id}.json"
        fault = json.loads(fp.read_text())
        for op in fault["ops"]:
            table = self.tables[op["table"]]
            if op["op"] == "set_cell_field":
                row = next(r for r in table["rows"] if r["id"] == op["row_id"])
                row["cells"][op["col"]][op["field"]] = op["value"]
            elif op["op"] == "swap_cells":
                row = next(r for r in table["rows"] if r["id"] == op["row_id"])
                a, b = op["cols"]
                row["cells"][a], row["cells"][b] = row["cells"][b], row["cells"][a]
            elif op["op"] == "set_table_field":
                table[op["field"]] = op["value"]
            elif op["op"] == "set_footnote":
                fn = next(f for f in table["footnotes"] if f["id"] == op["footnote_id"])
                fn["text"] = op["text"]
            elif op["op"] == "add_footnote":
                table["footnotes"].append({"id": op["footnote_id"], "text": op["text"]})
            else:
                raise ValueError(f"unknown fault op {op['op']}")

    # -- hashing -----------------------------------------------------------
    def _compute_hashes(self) -> dict[str, str]:
        h = {}
        for name, raw in self.doc_raw.items():
            h[name] = _sha256_bytes(raw.encode())
        for tid, t in self.tables.items():
            h[tid] = _sha256_bytes(json.dumps(t, sort_keys=True).encode())
        for name, rows in self.datasets.items():
            h[name] = _sha256_bytes(json.dumps(rows, sort_keys=True).encode())
        return h

    def manifest(self) -> dict:
        return {"fault_id": self.fault_id, "artifacts": dict(self.hashes),
                "study_id": self.study["study_id"]}

    # -- locator resolution ------------------------------------------------
    def resolve(self, locator: str) -> dict:
        """Resolve a locator to {artifact, artifact_hash, fragment}."""
        m = re.match(r"^artifact://[^/]+/(protocol|sap|csr_shell)/section/([\d.]+)/paragraph/(\d+)$",
                     locator)
        if m:
            doc, sec, para = m.group(1), m.group(2), int(m.group(3))
            section = self.docs[doc].get(sec)
            if not section or para < 1 or para > len(section["paragraphs"]):
                raise KeyError(f"locator not found: {locator}")
            return {"artifact": doc, "artifact_hash": self.hashes[doc],
                    "fragment": {"text": section["paragraphs"][para - 1],
                                 "section_title": section["title"]}}
        m = re.match(r"^table://([^/]+)/row/([^/]+)/col/([^/]+)$", locator)
        if m:
            tid, row_id, col = m.groups()
            table = self.tables.get(tid)
            if not table:
                raise KeyError(f"locator not found: {locator}")
            row = next((r for r in table["rows"] if r["id"] == row_id), None)
            if not row or col not in row["cells"]:
                raise KeyError(f"locator not found: {locator}")
            return {"artifact": tid, "artifact_hash": self.hashes[tid],
                    "fragment": {"cell": row["cells"][col], "row_label": row["label"],
                                 "column": next(c["label"] for c in table["columns"]
                                                if c["id"] == col)}}
        m = re.match(r"^table://([^/]+)/footnote/([^/]+)$", locator)
        if m:
            tid, fid = m.groups()
            table = self.tables.get(tid)
            if not table:
                raise KeyError(f"locator not found: {locator}")
            fn = next((f for f in table["footnotes"] if f["id"] == fid), None)
            if not fn:
                raise KeyError(f"locator not found: {locator}")
            return {"artifact": tid, "artifact_hash": self.hashes[tid],
                    "fragment": {"text": fn["text"]}}
        m = re.match(r"^table://([^/]+)/meta/(title|population|data_cutoff|source)$", locator)
        if m:
            tid, fieldname = m.groups()
            table = self.tables.get(tid)
            if not table:
                raise KeyError(f"locator not found: {locator}")
            return {"artifact": tid, "artifact_hash": self.hashes[tid],
                    "fragment": {"value": table[fieldname]}}
        m = re.match(r"^dataset://([^/]+)/meta$", locator)
        if m:
            name = m.group(1)
            rows = self.datasets.get(name)
            if rows is None:
                raise KeyError(f"locator not found: {locator}")
            return {"artifact": name, "artifact_hash": self.hashes[name],
                    "fragment": {"value": {"rows": len(rows),
                                           "variables": list(rows[0].keys()) if rows else []}}}
        m = re.match(r"^dataset://([^/]+)/subject/([^/]+)$", locator)
        if m:
            name, usubjid = m.groups()
            rows = self.datasets.get(name)
            if rows is None:
                raise KeyError(f"locator not found: {locator}")
            matches = [r for r in rows if r.get("USUBJID") == usubjid]
            if not matches:
                raise KeyError(f"locator not found: {locator}")
            return {"artifact": name, "artifact_hash": self.hashes[name],
                    "fragment": {"records": matches}}
        raise KeyError(f"unrecognized locator: {locator}")

    # -- constrained dataset queries ----------------------------------------
    def query_dataset(self, name: str, filters: list[dict] | None = None,
                      distinct_subjects: bool = True,
                      adsl_filters: list[dict] | None = None) -> dict:
        """Count-only query. Filters: [{var, op(eq|in|gte|ne), value}].
        `adsl_filters` restricts to subjects matching filters on ADSL
        (population/arm joins for recounts)."""
        rows = self.datasets.get(name)
        if rows is None:
            raise KeyError(f"unknown dataset {name}")
        filters = filters or []

        def keep(r: dict) -> bool:
            for f in filters:
                v = r.get(f["var"], "")
                op = f.get("op", "eq")
                if op == "eq" and v != str(f["value"]):
                    return False
                if op == "ne" and v == str(f["value"]):
                    return False
                if op == "in" and v not in [str(x) for x in f["value"]]:
                    return False
                if op == "gte":
                    try:
                        if float(v or "nan") < float(f["value"]):
                            return False
                    except ValueError:
                        return False
            return True

        kept = [r for r in rows if keep(r)]
        if adsl_filters:
            def keep_adsl(r: dict) -> bool:
                for f in adsl_filters:
                    v = r.get(f["var"], "")
                    op = f.get("op", "eq")
                    if op == "eq" and v != str(f["value"]):
                        return False
                    if op == "in" and v not in [str(x) for x in f["value"]]:
                        return False
                return True
            allowed_ids = {r["USUBJID"] for r in self.datasets["ADSL"] if keep_adsl(r)}
            kept = [r for r in kept if r["USUBJID"] in allowed_ids]
        if distinct_subjects:
            count = len({r["USUBJID"] for r in kept})
        else:
            count = len(kept)
        return {"dataset": name, "filters": filters,
                "adsl_filters": adsl_filters or [], "count": count,
                "distinct_subjects": distinct_subjects,
                "artifact_hash": self.hashes[name]}

    def table_overview(self, table_id: str) -> dict:
        t = self.tables[table_id]
        return {
            "table_id": t["table_id"], "title": t["title"],
            "population": t["population"], "protocol": t["protocol"],
            "columns": t["columns"],
            "row_ids": [{"id": r["id"], "label": r["label"]} for r in t["rows"]],
            "footnotes": t["footnotes"], "data_cutoff": t["data_cutoff"],
            "source": t["source"],
        }

    def copy(self) -> "Snapshot":
        return copy.deepcopy(self)
