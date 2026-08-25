# OpenCSR — handoff notes

State of the project as of 2026-08-24, for anyone (human or agent) picking
it up.

## What exists and where

| Thing | Where |
|---|---|
| Repo (public) | https://github.com/scottogden10/opencsr — CI green on push |
| Ledger (system of record) | `opencsr.db` (SQLite, WAL) — tasks, evidence, claims, proposals, issues, feedback, receipts, documents, evals, skill events, append-only audit |
| Live agent registry | `.opencsr/live_registry.json` — Managed Agent ids + versions + config hashes (safe to delete; agents are re-provisioned) |
| API key (live mode) | `.opencsr/api_key` — gitignored, chmod 600, read only by `agents/live.py` when `ANTHROPIC_API_KEY` is unset |
| Skills (versioned) | `skills/<name>/SKILL.md` + `versions/` — reset to v1 with `reset_skills()` or `git checkout -- skills` |
| Shareable overview | https://claude.ai/code/artifact/6db810e9-7eb5-478f-968c-7400ca0b0439 |

## Operating it

- Workbench: `python3 serve.py` (mock) / `.venv/bin/python serve.py
  --backend managed` (live). Port 8734.
- Model for live agents: `OPENCSR_MODEL` env var (default `claude-opus-5`;
  use `claude-sonnet-5` for fast/cheap demos). Changing it (or any skill)
  bumps the Managed Agent versions on next run — that is the intended
  change-control mechanism.
- Console workspace for trace links: `OPENCSR_WORKSPACE` (default
  `default`).
- `run_demo.py` refuses to wipe the ledger while the workbench is running
  (`--force` overrides). Never delete `opencsr.db` under a live run.

## Verification posture

- 22 offline tests (`python3 -m unittest discover -s tests`), including
  regression tests for every fixed review finding.
- A 26-agent adversarial review (5 lenses + skeptic verification) confirmed
  21 findings; all fixed in commit `75567b5`.
- Live mode verified end-to-end: efficacy task (4 sessions, provenance 1.0,
  0 revisions) and a scoped live hill climb (baseline 55.0 at v1 skills).

## Known limitations / next steps

- Only §11.4.1 and §12.3.2 have drafting workflows; the CSR view marks the
  rest NOT YET AUTOMATED (adding a section = task function + validators +
  gold facts; see docs/ARCHITECTURE.md → Extending).
- The live skill-smith proposes free-form rules; candidates are validated
  (known skill, well-formed RULE marker) and gate-checked, but rule quality
  varies with model tier.
- Simulated governance stands in for multi-role review cycles; real
  multi-reviewer sign-off chains (writer → biostat → QA) are not modeled.
- No DOCX/eCTD export; documents live as versioned ledger text.
