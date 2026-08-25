# OpenCSR evaluation report

**Study OCS-101 (synthetic) · 2026-08-24/25 · backends: deterministic mock + Claude Managed Agents (Opus 5 → Sonnet 5) · total live spend ≈ $10–12 list**

## 1. What the benchmark measures

A CSR-drafting system is not graded on fluency. The corpus scores the things a
regulatory reviewer would fail you for, as a **metrics vector with hard
gates** — never one fuzzy number:

| Layer | Cases | What passes |
|---|---|---|
| Gold efficacy task | 1 task, 10 gold facts (n, N, %, CI bounds × 2 arms) | Every registered claim value exactly matches the locked TLF-derived truth; provenance coverage 1.0; zero untraceable numbers |
| Seeded faults | 6 corrupted-source tasks — the six most common real CSR QC findings (wrong denominator, stale cutoff, population mismatch, transposed arms, copied CI, prompt-injection footnote) | Each fault detected at high severity **and** its proposal blocked and rejected |
| Narrative | ICH E3 §12.3.2 element coverage for a death/SAE subject | All applicable elements present, first pass |
| Capability probe | Out-of-grant tool calls from a writer-scoped gateway | 0 executed, all denied and receipted |

Hard gates (permission violations = 0, provenance = 1.0, numeric accuracy =
1.0, all faults caught and blocked) are pass/fail and scoped to the cases
that actually ran; the 0–100 composite exists only to rank candidates whose
gates all pass. Facts are graded deterministically (exact recomputation,
dataset recounts) — no LLM judge owns a clinical number.

## 2. The eval ledger

| When | Run | Backend | Score | Gates |
|---|---|---|---|---|
| 23:42 | baseline (skills v1) | mock | 68.06 | PASS |
| 23:42 | + NO_UNAPPROVED_INTERPRETIVE | mock | 80.56 | PASS → **promoted** |
| 23:42 | + REPORT_N_OVER_N | mock | 90.56 | PASS → **promoted** |
| 23:42 | + STATE_DATA_CUTOFF | mock | 93.89 | PASS → **promoted** |
| 23:42 | + STATE_ONSET_DAY (narrative) | mock | 100.00 | PASS → **promoted** |
| 23:42 | post-hillclimb | mock | **100.00** | PASS |
| 00:09 | baseline (live, pre-fix) | Sonnet | 55.00 | **FAIL** |
| 00:28 | baseline (live, post-fix) | Sonnet | 80.00 | PASS |
| 00:44 | + PERCENT_REQUIRES_N_OVER_N | Sonnet | **5.00** | FAIL → **rejected** |
| 00:49 | + ALWAYS_STATE_DATA_CUTOFF | Sonnet | 80.00 | PASS → rejected (no gain) |
| 00:55 | + NO_UNAPPROVED_INTERPRETIVE | Sonnet | 80.00 | PASS → rejected (no gain) |
| 01:00 | post-hillclimb | Sonnet | 80.00 | PASS |
| — | narrative-scoped climb | Sonnet | *see §5* | |

## 3. Does the framework climb well? Three findings

**Finding 1 — offline, the climb is textbook.** From v1 skills, the
skill-smith proposed four rules from reviewer feedback and eval failures;
each carried a falsifiable hypothesis ("interpretive flags → 0; revision
cycles down"), each replayed the full corpus, each improved the score
without regressing a gate, and the search converged when proposals dried
up: **68.06 → 100.00, four promotions, zero gate regressions, then a clean
re-run accepted with zero revisions and edit distance 0.0**. Because the
mock backend is deterministic, every point of that ascent is attributable
to the promoted rule and nothing else.

**Finding 2 — live, the climb refused to promote, correctly, three ways.**
(a) The first Sonnet candidate — a plausible-sounding numeric-formatting
rule a human reviewer would likely have waved through — **collapsed the
replay from 80 to 5.00** and failed the gates; it was rejected on the
record with the eval that killed it. This is the strongest evidence in the
ledger that gate-protected promotion is load-bearing. (b) Two further
candidates scored exactly baseline and were refused by the improvement
margin, which exists because live runs are stochastic and a margin-free
comparison would promote noise. (c) The refusals were cheap: candidates
whose skill no in-scope case can observe are skipped before any session is
spent.

**Finding 3 — the benchmark itself needed climbing, and the live run is
what found the bug.** The first live baseline scored 55.00 with gates
failing while producing a *clinically perfect* draft — the gold matcher was
keying on an internal arm id the agent was never told about, i.e. the eval
was grading schema guessing, not clinical content. The deterministic mock
could never expose this, because it embodies the grader's assumptions.
Fixing it (vocabulary pinned in the agent contract + normalization in the
grader + validator recounts un-skipped) moved the truthful baseline to
80.00 — which is this scope's **ceiling**, meaning Sonnet-tier agents at
v1 skills already do everything the scoped corpus can measure, first pass.
The offline and live climbs therefore demonstrate complementary things:
the mock shows the *learning* loop working; the live run shows the
*governance* loop working.

## 4. Cost profile

All Managed Agents usage across the entire program — three full climbs,
two standalone tasks, ~40 sessions — totals **≈ $10–12 at list price**
(sessions $0.01–$0.25; Opus QC at v1 was the single most expensive at
$0.22). A scoped Sonnet eval is ~6 sessions ≈ $0.60; the offline corpus is
free and runs in CI on every push. Guardrails: $2.50/session hard budget
caps, per-agent tool-call ceilings, and a persistent spend meter in the
workbench header.

## 5. Narrative-scoped live climb (ceiling 90)

RESULT_PENDING

## 6. Limitations

One synthetic study; a simulated reviewer stands in for multi-role review
cycles; live candidate comparisons are n=1 (the margin compensates but
replicates would be better); the six live fault cases have not been run
live (≈ $4 if wanted — offline they are 6/6 detected and blocked on every
CI run); style/convention scoring is deterministic lint, deliberately
narrow. The benchmark is a regression asset, not a leaderboard: its job is
to make every change to the system — skill, prompt, model, or harness —
prove itself before touching production, and that is the property the
ledger above demonstrates.
