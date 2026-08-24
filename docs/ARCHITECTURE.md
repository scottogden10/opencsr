# OpenCSR architecture notes

## The five layers

1. **Clinical control plane** (`store.py`, `domain.py`) — SQLite ledger:
   tasks, runs, evidence, claims, proposals, issues, feedback, receipts,
   documents (versioned), evals, skill events, append-only audit. This is
   the system of record; Managed Agents session state is disposable.
2. **Deterministic workflow** (`workflow.py`) — code decides stage order
   (evidence → claims → validate → draft ⇄ revise ≤2 → QC → review), briefs,
   budgets, blocking rules, and the controlled patch on acceptance
   (stale-base check included). Agents never decide any of this.
3. **Agent execution** (`agents/`) — a `Backend` protocol with two
   implementations. `mock.py` is deterministic Python driving the same tool
   gateway (so the entire governance surface is exercised offline, and skill
   rules have real behavioral effect). `live.py` is Claude Managed Agents:
   agent-object-per-role (create once, update = new immutable version),
   session-per-stage with `initial_events`, dollar budget caps,
   consolidation reconnects, and the Pattern-5 idle gate; every
   `agent.custom_tool_use` is answered host-side via
   `user.custom_tool_result`.
4. **Governed tool gateway** (`tools.py`) — narrow clinical semantic tools
   (`get_tlf_cells`, `exact_binomial_ci`, `create_claims`, …) with JSON
   schemas shared verbatim with the live agent configs. Enforces capability
   grants, locator re-resolution + `expected`-value verification for
   evidence, authority rules for claims, and writes a receipt for every
   call.
5. **Improvement system** (`evals.py`, `hillclimb.py`, `skills.py`) —
   metrics vector + hard gates over a replayed corpus; skill-smith proposals
   with hypotheses; greedy gate-protected promotion; versioned skill files
   with full history and reset.

## Claim model

```json
{
  "claim_type": "efficacy_result",
  "text": "The confirmed objective response rate ... was 42.1% (16/38).",
  "dimensions": {"endpoint": "confirmed_orr",
                 "population": "Response-Evaluable Population",
                 "treatment_arm": "arm_100", "timepoint": "2026-05-01"},
  "value": {"numerator": 16, "denominator": 38, "estimate_pct": 42.1,
            "ci_lower_pct": 26.3, "ci_upper_pct": 59.2, "ci_level": 95},
  "support_class": "direct",
  "evidence_ids": ["ev_...cell", "ev_...population", "ev_...footnotes"]
}
```

Support classes: `direct` (value appears in a permitted source), `derived`
(validated calculator), `interpretive` (requires accountable human
approval — unapproved use is a high-severity issue), `conflicted`,
`unsupported` (cannot proceed).

## Authority rules

`data/synthetic_trial/authority.json`: which source types may support which
claim types (e.g. `efficacy_result` → TLF only; `analysis_population_definition`
→ SAP), with conflict behavior naming the accountable human role. A claim
combining a permitted primary source with supplementary context passes with
a note; a claim with *no* permitted source is rejected at registration.

## Why the mock backend is honest

The mock isn't a stub that returns canned answers — it is a scripted client
of the same gateway, subject to the same capability grants, evidence
verification, authority rules, validators, and QC recounts. Its writer and
narrative behaviors key off the `RULE:*` markers actually present in the
compiled skill prompt, so a skill promotion changes behavior for real, and
the hill-climb's measured improvement is earned, not staged. Seeded faults
flow through the same path a live model would see.

## Live-mode governance highlights

- Agents get **only** custom tools — no `agent_toolset` — so the sandbox has
  no bash/filesystem/web; the environment is `limited` networking besides.
- The trial data and ledger stay in your process (client-patterns "Pattern
  9"); prompt-injected source content can at worst emit tool calls, which
  the gateway grants/denies and receipts.
- Skill promotion → prompt recompile → `agents.update()` → new immutable
  agent version; sessions pin `{id, version}`, so replays and rollbacks are
  exact.

## Extending

- New section workflow: add gold facts + a task function in `workflow.py`
  (the stages are reusable), plus validators for the section's required
  content.
- New fault: drop a JSON patch in `data/synthetic_trial/seeded_faults/` and
  an expectation in `evals/gold/expected_issues.json` — the eval corpus
  picks it up by name.
- New skill: a directory under `skills/` with `SKILL.md` (frontmatter +
  body); bind it to agents in `agents/registry.py`.
- Real human review: `serve.py` — decisions land through the same
  `apply_decision` path as the simulated reviewer, with the same audit trail.
