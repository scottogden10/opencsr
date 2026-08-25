# OpenCSR — 10-minute demo script

A presenter's walkthrough. Offline mode needs nothing but Python; live mode
needs an Anthropic API key. Every step below leaves durable traces: ledger
rows, audit events, document versions, and (live) Console session traces.

## Setup (once, 2 minutes)

```bash
git clone https://github.com/scottogden10/opencsr && cd opencsr
python3 run_demo.py        # seeds the ledger with the 5-act offline story
python3 serve.py           # workbench at http://127.0.0.1:8734
```

Live mode (optional): `python3 -m venv .venv && .venv/bin/pip install anthropic`,
put the key in `.opencsr/api_key`, then
`OPENCSR_MODEL=claude-sonnet-5 .venv/bin/python serve.py --backend managed`
(Sonnet is the fast/cheap demo choice; omit `OPENCSR_MODEL` for Opus quality).

## The demo flow

1. **Open the CSR tab (landing page).** Talking point: *"This is the report
   assembling itself. Every section shows its state — shell, pending, agents
   drafting, awaiting a named human role, populated with an approver, or a
   locked final TLF. Nothing gets in without a human decision."* Show the
   progress bar and the role pipeline strip.

2. **Expand §14.2.1.** The actual frozen TLF renders inline — the exact
   table cells the agents cite, with footnotes and data cutoff. *"Sources
   are frozen and hashed per task; evidence is an exact cell address, not a
   vector-search vibe."*

3. **Expand §12.3.2 and click "Draft with agents" on any SAE subject.**
   Watch the chip flip to AGENTS DRAFTING, then to AWAITING SAFETY/PV
   (~2 s offline; ~2–4 min live — narrate the Console trace link meanwhile).

4. **Click "Review now".** Walk the review screen top to bottom: the draft,
   the sentence→claim map, each claim's evidence locator and verified
   fragment, the issues panel (rule engine + independent QC), and the
   role-attributed decision panel. **Accept as Safety/PV.** Back on the CSR
   tab, the narrative is populated, stamped with your role, and the
   progress bar moved. Repeat for a second subject if time allows.

5. **Run a seeded fault.** Tasks tab → fault dropdown → `swapped_arms` →
   run. The proposal comes back **blocked** with high-severity
   `numeric_mismatch` issues — the independent recount from ADSL/ADRS
   disagrees with the corrupted table. Reject it. *"The six seeded faults
   are the six most common real CSR QC findings, including a
   prompt-injection footnote the QC flags instead of obeying."*

6. **Evals tab.** The metrics vector and hard gates. *"Never one fuzzy
   number: safety properties are gates, quality is a vector."*

7. **Hill climb tab.** Show the score trajectory and the promotion history
   — each promoted rule carries a hypothesis and the eval that justified
   it. Offline: 68 → 100 in one round. *"Skills are versioned files;
   promotions are gate-protected; on the live backend a promotion becomes a
   new immutable Managed Agent version with exact rollback."*

8. **Audit tab.** End on the append-only trail: task created → snapshot
   frozen → agent runs → validations → human decision → document version.

## Reset between demos

```bash
python3 run_demo.py --force      # fresh ledger + re-seeded story
```

## Cost notes (live mode)

Each agent session carries a $5 list-cost budget cap. A single task ≈ 4
sessions; a scoped hill climb ≈ 20–30 sessions. Sonnet 5 runs roughly 2–3×
faster and ~3× cheaper than Opus for this workload.
