"""Typed domain objects for the clinical control plane.

Agents exchange these artifacts (as JSON) — never free-form chat. The SQLite
ledger in `store.py` is the system of record; provider session state is
disposable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


SUPPORT_CLASSES = ("direct", "derived", "interpretive", "conflicted", "unsupported")
SEVERITIES = ("low", "medium", "high")
ISSUE_CATEGORIES = (
    "numeric_mismatch", "wrong_population", "wrong_arm", "stale_cutoff",
    "unsupported_claim", "missing_required_content", "terminology",
    "interpretive_overreach", "source_conflict", "suspicious_content",
    "missing_evidence", "style", "capability_violation", "other",
)
REASON_CODES = (
    "accepted_as_proposed", "interpretation_too_strong", "wrong_number",
    "wrong_population", "style_convention", "missing_content",
    "terminology", "evidence_insufficient", "other",
)
FEEDBACK_SCOPES = ("this_instance", "this_study", "organization_wide")


@dataclass
class EvidenceItem:
    id: str
    task_id: str
    locator: str
    artifact: str
    artifact_hash: str
    fragment: dict          # {"text": ...} or {"cell": {...}} or {"value": ...}
    role: str               # what this evidence is for (endpoint_definition, ...)
    created_by: str
    verified: bool = False  # harness re-resolved the locator and matched fragment
    created_at: str = field(default_factory=now_iso)


@dataclass
class Claim:
    id: str
    task_id: str
    claim_type: str
    text: str
    dimensions: dict        # endpoint / population / arm / timepoint ...
    value: dict             # numerator / denominator / estimate / ci ...
    support_class: str      # direct | derived | interpretive | conflicted | unsupported
    evidence_ids: list
    status: str = "proposed"   # proposed | validated | rejected
    created_by: str = ""
    created_at: str = field(default_factory=now_iso)


@dataclass
class Proposal:
    id: str
    task_id: str
    target_node: str        # e.g. csr/11.4.1
    base_version: int
    base_hash: str
    text: str
    sentence_map: list      # [{"sentence": ..., "claim_ids": [...]}]
    status: str = "draft"   # draft | validated | qc_reviewed | pending_review | accepted | modified | rejected | stale
    revision: int = 0
    created_at: str = field(default_factory=now_iso)


@dataclass
class Issue:
    id: str
    task_id: str
    category: str
    severity: str
    description: str
    refs: dict = field(default_factory=dict)   # claim_id / locator / proposal_id
    raised_by: str = ""
    status: str = "open"    # open | resolved | overridden
    created_at: str = field(default_factory=now_iso)


@dataclass
class FeedbackEvent:
    id: str
    task_id: str
    proposal_id: str
    decision: str           # accepted | modified | rejected
    reason_code: str
    scope: str
    comment: str
    final_text: str
    actor: str
    created_at: str = field(default_factory=now_iso)


@dataclass
class ToolReceipt:
    id: str
    task_id: str
    run_id: str
    tool: str
    tool_input: dict
    output_digest: str
    allowed: bool
    error: str = ""
    created_at: str = field(default_factory=now_iso)


@dataclass
class AgentRun:
    id: str
    task_id: str
    agent: str
    agent_version: str
    backend: str            # mock | managed
    status: str = "running"  # running | completed | failed | budget_exceeded
    detail: dict = field(default_factory=dict)  # session_id, usage, tool_calls...
    created_at: str = field(default_factory=now_iso)


def to_json(obj) -> str:
    return json.dumps(asdict(obj), ensure_ascii=False)
