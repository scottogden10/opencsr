# Changelog

## 2026-08-24 — initial build (one day, seven commits)

- **`7048565`** Full platform: synthetic trial OCS-101 (ADaM CSVs with
  RECIST-derived responses; TLFs computed from the datasets), governed
  pipeline (evidence → claims → draft → validators → independent QC →
  human review), SQLite ledger with append-only audit, six seeded faults,
  eval corpus with hard gates, gate-protected hill climb over versioned
  skills, zero-dependency mock backend, Claude Managed Agents live
  backend, localhost review workbench, 14 tests, CI.
- **`676277c`** Security hardening after first review pass (UI escaping,
  CSRF guard, vocabulary normalization, skill-name validation).
- **`e7f381c`** Live-mode key file (`.opencsr/api_key`) + venv docs.
- **`75567b5`** All 21 confirmed findings from a 26-agent adversarial
  review fixed, each with a regression test (query-filter correctness,
  transcription-guard soundness, live event-loop delivery safety,
  eval failure shaping, DNS-rebinding defense, more).
- **`60bc087`** Scoped live hill climb from the workbench.
- **`b107844`** CSR assembly view (living document, per-section states,
  per-subject 12.3.2 narrative workload, inline frozen TLFs) + explicit
  human roles (review queues, acting-as decisions, actor chips).

Milestones proven along the way: offline demo 68 → 100 hill climb;
live Managed Agents efficacy task (4 sessions, provenance 1.0,
0 revisions); live hill climb baseline 55.0 at v1 skills.
