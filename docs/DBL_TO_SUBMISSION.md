# Database lock → submission: how it really works, and where OpenCSR sits

This is the real-world process the demo models, compiled from industry
sources (CDM SOP guides, PHUSE papers, medical-writing surveys, ICH E3/E6,
eCTD guidance). Dates in *italics* are study OCS-101's synthetic timeline.

## 1. Before lock: cleaning and rehearsal

- Clinical data management runs continuous query management through the
  trial; the pre-lock checklist requires every CRF page entered, all queries
  resolved, medical coding (MedDRA for AEs, WHODrug for con-meds) approved,
  vendor data (labs, imaging, ECG) reconciled, SAE reconciliation against
  the pharmacovigilance safety database completed, and protocol deviations
  classified.
- Statistical programming rehearses with **dry runs**: the full
  SDTM → ADaM → TLF pipeline executed on blinded or dummy-randomized data
  against the mock shells in the SAP, so the final production run is mostly
  re-execution.
- The SAP is finalized and approved **before** lock (*OCS-101 SAP v2
  approved 28 Apr 2026; data cutoff 01 May 2026*).

## 2. Lock and unblinding

- **Soft lock (freeze)** — all data in, known queries resolved; edit access
  restricted to a few data managers (*29 May 2026*). A **blinded data review
  meeting** finalizes analysis populations and deviation handling while the
  team is still blinded (*05 Jun*).
- **Hard lock** — after QC/QA review, formal sign-off (typically PI,
  clinical operations, CDM lead, biostatistics) removes edit access
  entirely; any later change requires a documented, exception-level unlock
  with audit trail (*12 Jun 2026*).
- **Unblinding** — the randomization code is released to biostatistics only
  after hard lock, deliberately sequenced to prevent bias. (*OCS-101 is
  open-label, so this step collapses; the hard-lock gate still governs when
  final analysis begins.*)

## 3. Final analysis and topline

- Final SDTM tabulation datasets → ADaM analysis datasets → TLFs per SAP.
- QC classically uses **independent double programming** for primary
  efficacy and key safety outputs (a second programmer independently
  reproduces the result), increasingly risk-tiered.
- **Topline results** — a pre-specified subset of key TLFs — go to
  management ~5–7 business days after DBL (*19 Jun*). The full TLF package
  follows (*26 Jun*, e.g. `t_orr.sas` producing Table 14.2.1).

## 4. CSR authoring (where OpenCSR lives)

- The CSR follows **ICH E3**: synopsis, sections 1–15, Section 14 post-text
  TLFs (14.1.x demographics, 14.2.x efficacy, 14.3.x safety), Section 16
  appendices (16.1 protocol/SAP, 16.2 listings, 16.3 CRFs).
- Methods sections are pre-drafted from protocol/SAP before lock ("shell
  first"); results sections are populated from final TLFs after lock —
  exactly the moment OpenCSR's efficacy task fires.
- Survey timelines for a moderately complex Phase 3: final TLFs → first
  draft ≈ 17 days; review cycles to final ≈ 26 more; **~83 days DBL → final
  CSR** on average. Review rounds involve the medical monitor,
  biostatistics, PV/safety, regulatory, clin pharm, and data management.
- The dominant QC burden — and the most common findings — are exactly what
  OpenCSR's validators and seeded faults encode: **text-vs-TLF number
  mismatches, wrong analysis population, stale data cutoff, broken table
  cross-references, unapproved interpretive language**.
- Per ICH E3 §12.3.2, every death and SAE gets a **narrative** containing
  patient identifier, age/sex, disease and duration, con-meds, study drug
  and dose, event nature and intensity, onset timing relative to dosing,
  action taken, countermeasures, outcome (with post-mortem findings for
  deaths), and the investigator's causality opinion — the element checklist
  OpenCSR's narrative validator enforces.

## 5. Submission package

- The CSR files in **eCTD Module 5.3.5.x** (5.3.5.1 controlled studies,
  5.3.5.2 uncontrolled — *OCS-101's slot*, 5.3.5.3 pooled analyses),
  accompanied by SDTM/ADaM datasets (XPT), **define.xml** for each package,
  the SDRG/ADRG reviewer's guides, the annotated CRF, and key analysis
  programs.
- **Submission readiness** = conformance validation (Pinnacle 21 against
  CDISC IG rules and FDA Technical Rejection Criteria), dataset/define
  consistency, SDTM→ADaM→TLF traceability, eCTD structure and PDF
  hyperlink/bookmark QC — FDA validates incoming sequences automatically
  and can technically reject nonconforming study data.

## What the demo deliberately compresses

One CSR section + one narrative instead of a full report; JSON TLFs instead
of RTF; a simulated reviewer standing in for a multi-role review cycle; no
define.xml/eCTD assembly. The *governance shape* — frozen sources, typed
claims, exact locators, independent recomputation, blocking gates, human
sign-off, append-only audit — is the part that transfers.
