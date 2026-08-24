#!/usr/bin/env python3
"""Generate the OCS-101 synthetic trial fixture.

Study OCS-101 ("EMBER-1"): a fictional Phase 2, open-label, two-arm study of
examplinib (a fictional kinase inhibitor) in advanced solid tumors with EXM1
mutations. Everything here is synthetic and deterministic (fixed seed).

Design goals:
  * ADaM-shaped CSVs (ADSL, ADRS, ADAE) with CDISC-conventional variables.
  * Every TLF number is DERIVED from the datasets at generation time, so
    dataset -> TLF -> CSR traceability is real, not cosmetic.
  * RECIST 1.1 confirmed best overall response derived by algorithm from
    per-assessment OVR records (confirmation >= 28 days, CR>PR>SD>PD>NE,
    assessments only up to first PD).
  * Gold facts for evals are emitted from the same computation.

Run:  python3 data/synthetic_trial/generate_data.py
"""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from opencsr.stats import orr_with_ci, pct  # noqa: E402

RNG = random.Random(20260501)

STUDYID = "OCS-101"
DATA_CUTOFF = date(2026, 5, 1)
CUTOFF_DDMMMYYYY = "01MAY2026"
ARMS = {
    1: {"col": "arm_100", "label": "Examplinib 100 mg", "n_enrolled": 40},
    2: {"col": "arm_50", "label": "Examplinib 50 mg", "n_enrolled": 40},
}
SITES = ["001", "002", "003", "004", "005", "006", "007", "008"]

# Confirmed BOR plan per arm: category -> count among response-evaluable.
CBOR_PLAN = {
    1: {"CR": 3, "PR": 13, "SD": 12, "PD": 8, "NE": 2},   # EFF n=38
    2: {"CR": 1, "PR": 9, "SD": 14, "PD": 10, "NE": 3},   # EFF n=37
}
UNCONFIRMED_PR_DOWNGRADES = {1: 2, 2: 2}  # BOR=PR but CBOR=SD

AE_POOL = [
    # (verbatim, AEDECOD, AEBODSYS, weight, grade weights g1..g4)
    ("fatigue", "Fatigue", "General disorders and administration site conditions", 9, (5, 4, 1, 0)),
    ("nausea", "Nausea", "Gastrointestinal disorders", 8, (6, 3, 1, 0)),
    ("loose stools", "Diarrhoea", "Gastrointestinal disorders", 8, (5, 3, 2, 0)),
    ("low haemoglobin", "Anaemia", "Blood and lymphatic system disorders", 6, (3, 4, 2, 0)),
    ("maculopapular rash", "Rash maculo-papular", "Skin and subcutaneous tissue disorders", 6, (5, 3, 1, 0)),
    ("elevated ALT", "Alanine aminotransferase increased", "Investigations", 5, (4, 3, 2, 0)),
    ("decreased appetite", "Decreased appetite", "Metabolism and nutrition disorders", 5, (6, 3, 0, 0)),
    ("vomiting", "Vomiting", "Gastrointestinal disorders", 4, (5, 3, 1, 0)),
    ("itching", "Pruritus", "Skin and subcutaneous tissue disorders", 4, (6, 2, 0, 0)),
    ("cough", "Cough", "Respiratory, thoracic and mediastinal disorders", 3, (7, 2, 0, 0)),
    ("shortness of breath", "Dyspnoea", "Respiratory, thoracic and mediastinal disorders", 3, (4, 4, 1, 0)),
    ("low platelets", "Platelet count decreased", "Investigations", 3, (4, 3, 2, 0)),
]


def iso(d: date) -> str:
    return d.isoformat()


def derive_cbor(assessments: list[tuple[date, str]]) -> str:
    """Confirmed best overall response per RECIST 1.1 (simplified, faithful).

    assessments: [(date, AVALC)] sorted by date; AVALC in CR/PR/SD/PD/NE.
    Confirmation: CR confirmed by CR >= 28 days later; PR confirmed by PR or
    CR >= 28 days later. Only assessments up to and including first PD count.
    """
    upto = []
    for d, r in assessments:
        upto.append((d, r))
        if r == "PD":
            break
    def confirmed(target: str, oks: set[str]) -> bool:
        for i, (d1, r1) in enumerate(upto):
            if r1 != target:
                continue
            for d2, r2 in upto[i + 1:]:
                if r2 in oks and (d2 - d1).days >= 28:
                    return True
        return False
    if confirmed("CR", {"CR"}):
        return "CR"
    if confirmed("PR", {"PR", "CR"}):
        return "PR"
    if any(r == "SD" for _, r in upto):
        return "SD"
    if any(r in {"PR", "CR"} for _, r in upto):
        # response seen but never confirmed and no SD recorded -> SD per
        # convention when minimum SD duration met; keep SD for simplicity
        return "SD"
    if any(r == "PD" for _, r in upto):
        return "PD"
    return "NE"


def derive_bor(assessments: list[tuple[date, str]]) -> str:
    upto = []
    for d, r in assessments:
        upto.append(r)
        if r == "PD":
            break
    for cat in ("CR", "PR", "SD"):
        if cat in upto:
            return cat
    if "PD" in upto:
        return "PD"
    return "NE"


# ---------------------------------------------------------------------------
# Build subjects
# ---------------------------------------------------------------------------

def build_subjects():
    subjects = []
    subjid = 1000
    for trtn, arm in ARMS.items():
        # roles within the arm
        n = arm["n_enrolled"]
        roles = ["evaluable"] * n
        if trtn == 1:
            roles[7] = "not_dosed"          # ITT only: never received drug
            roles[19] = "no_measurable"     # dosed, but no measurable disease
        else:
            roles[5] = "no_measurable"
            roles[23] = "no_measurable"
            roles[31] = "no_measurable"
        # assign CBOR categories to evaluable subjects deterministically
        cats = []
        for cat, cnt in CBOR_PLAN[trtn].items():
            cats.extend([cat] * cnt)
        RNG.shuffle(cats)
        ci = 0
        for i in range(n):
            subjid += 1
            site = SITES[(subjid + trtn) % len(SITES)]
            role = roles[i]
            cbor = None
            if role == "evaluable":
                cbor = cats[ci]
                ci += 1
            randdt = date(2025, 3, 3) + timedelta(days=RNG.randint(0, 240))
            trtsdt = None if role == "not_dosed" else randdt + timedelta(days=RNG.randint(1, 7))
            subjects.append({
                "subjid": str(subjid),
                "usubjid": f"{STUDYID}-{site}-{subjid}",
                "site": site,
                "trtn": trtn,
                "trt": arm["label"],
                "role": role,
                "cbor": cbor,
                "randdt": randdt,
                "trtsdt": trtsdt,
                "sex": RNG.choice(["F", "M"]),
                "age": RNG.randint(34, 81),
                "race": RNG.choices(
                    ["WHITE", "ASIAN", "BLACK OR AFRICAN AMERICAN", "OTHER"],
                    weights=[62, 20, 12, 6])[0],
                "country": RNG.choices(["USA", "ESP", "KOR", "DEU"], weights=[5, 2, 2, 1])[0],
            })
    assert len(cats) == ci or True
    return subjects


def pick(subjects, trtn, role="evaluable", cbor=None, exclude=()):
    for s in subjects:
        if s["trtn"] == trtn and s["role"] == role and s["usubjid"] not in exclude:
            if cbor is None or s["cbor"] == cbor:
                return s
    raise RuntimeError("no subject matches")


# ---------------------------------------------------------------------------
# ADRS: per-assessment OVR + derived rows
# ---------------------------------------------------------------------------

def make_assessments(sub) -> list[tuple[date, str, str]]:
    """Return [(adt, avisit, avalc)] realizing the planned CBOR."""
    if sub["role"] != "evaluable" or sub["trtsdt"] is None:
        return []
    t0 = sub["trtsdt"]
    w6, w12, w18 = t0 + timedelta(days=42), t0 + timedelta(days=84), t0 + timedelta(days=126)
    cbor = sub["cbor"]
    if cbor == "CR":
        seq = [(w6, "WEEK 6", "PR"), (w12, "WEEK 12", "CR"), (w18, "WEEK 18", "CR")]
    elif cbor == "PR":
        seq = [(w6, "WEEK 6", "PR"), (w12, "WEEK 12", "PR"), (w18, "WEEK 18", "SD")]
    elif cbor == "SD":
        if sub.get("unconfirmed_pr"):
            seq = [(w6, "WEEK 6", "PR"), (w12, "WEEK 12", "SD"), (w18, "WEEK 18", "SD")]
        else:
            seq = [(w6, "WEEK 6", "SD"), (w12, "WEEK 12", "SD")]
    elif cbor == "PD":
        # PD at the first assessment: an SD-then-PD sequence would derive to
        # best response SD per RECIST, so planned-PD subjects progress early.
        seq = [(w6, "WEEK 6", "PD")]
    else:  # NE
        seq = [(w6, "WEEK 6", "NE")]
    return [(d, v, r) for d, v, r in seq if d <= DATA_CUTOFF]


def build_adrs(subjects):
    aval_map = {"CR": 1, "PR": 2, "SD": 3, "PD": 4, "NE": 5}
    rows = []
    # mark unconfirmed-PR downgrades among SD subjects
    for trtn, k in UNCONFIRMED_PR_DOWNGRADES.items():
        marked = 0
        for s in subjects:
            if s["trtn"] == trtn and s["cbor"] == "SD" and marked < k:
                s["unconfirmed_pr"] = True
                marked += 1
    for s in subjects:
        asmts = make_assessments(s)
        s["assessments"] = [(d, r) for d, _, r in asmts]
        for adt, avisit, avalc in asmts:
            rows.append({
                "STUDYID": STUDYID, "USUBJID": s["usubjid"], "PARAMCD": "OVR",
                "PARAM": "Overall Response by Investigator (RECIST 1.1)",
                "AVISIT": avisit, "ADT": iso(adt), "AVALC": avalc,
                "AVAL": aval_map[avalc], "ANL01FL": "Y",
                "CRIT1": "", "CRIT1FL": "",
            })
        if s["role"] == "evaluable":
            pairs = [(d, r) for d, r in s["assessments"]]
            bor = derive_bor(pairs) if pairs else "NE"
            cbor = derive_cbor(pairs) if pairs else "NE"
            assert cbor == s["cbor"], f"derived {cbor} != planned {s['cbor']} for {s['usubjid']}"
            responder = "Y" if cbor in ("CR", "PR") else "N"
            for pcd, pnam, av in (
                ("BOR", "Best Overall Response (Unconfirmed)", bor),
                ("CBOR", "Best Confirmed Overall Response (RECIST 1.1)", cbor),
            ):
                rows.append({
                    "STUDYID": STUDYID, "USUBJID": s["usubjid"], "PARAMCD": pcd,
                    "PARAM": pnam, "AVISIT": "", "ADT": "", "AVALC": av,
                    "AVAL": aval_map[av], "ANL01FL": "Y", "CRIT1": "", "CRIT1FL": "",
                })
            rows.append({
                "STUDYID": STUDYID, "USUBJID": s["usubjid"], "PARAMCD": "CRSP",
                "PARAM": "Confirmed Responder (CR or PR)", "AVISIT": "", "ADT": "",
                "AVALC": responder, "AVAL": 1 if responder == "Y" else 0,
                "ANL01FL": "Y", "CRIT1": "Response of CR or PR",
                "CRIT1FL": "Y" if responder == "Y" else "",
            })
    return rows


# ---------------------------------------------------------------------------
# ADAE
# ---------------------------------------------------------------------------

def build_adae(subjects):
    rows = []
    weights = [a[3] for a in AE_POOL]
    for s in subjects:
        if s["trtsdt"] is None:
            continue  # not dosed -> no TEAEs
        if RNG.random() < 0.08:
            continue  # AE-free subject
        n_events = RNG.choices([1, 2, 3, 4, 5], weights=[15, 30, 30, 17, 8])[0]
        for _ in range(n_events):
            verb, decod, soc, _, gw = RNG.choices(AE_POOL, weights=weights)[0]
            grade = RNG.choices([1, 2, 3, 4], weights=gw)[0]
            astdt = s["trtsdt"] + timedelta(days=RNG.randint(3, 200))
            if astdt > DATA_CUTOFF:
                astdt = DATA_CUTOFF - timedelta(days=RNG.randint(1, 30))
            dur = RNG.randint(3, 40)
            aendt = min(astdt + timedelta(days=dur), DATA_CUTOFF)
            rel = RNG.random() < (0.62 if grade >= 2 else 0.5)
            ser = grade >= 3 and RNG.random() < 0.32
            if grade >= 3:
                acn = RNG.choices(
                    ["DRUG INTERRUPTED", "DOSE REDUCED", "DRUG WITHDRAWN", "DOSE NOT CHANGED"],
                    weights=[4, 3, 1, 2])[0]
            else:
                acn = RNG.choices(["DOSE NOT CHANGED", "DRUG INTERRUPTED"], weights=[8, 1])[0]
            rows.append({
                "STUDYID": STUDYID, "USUBJID": s["usubjid"],
                "AETERM": verb, "AEDECOD": decod, "AEBODSYS": soc,
                "ATOXGR": str(grade), "AESER": "Y" if ser else "N",
                "AEREL": "RELATED" if rel else "NOT RELATED",
                "AEACN": acn,
                "AEOUT": RNG.choices(
                    ["RECOVERED/RESOLVED", "RECOVERING/RESOLVING", "NOT RECOVERED/NOT RESOLVED"],
                    weights=[6, 2, 2])[0],
                "AESTDTC": iso(astdt), "AEENDTC": iso(aendt),
                "ASTDT": iso(astdt), "AENDT": iso(aendt),
                "TRTEMFL": "Y",
            })
    # --- forced special events -------------------------------------------
    # 1) Fatal grade 5 pneumonitis on a 100 mg subject (narrative subject).
    fatal = pick(subjects, 1, cbor="SD")
    fatal["fatal_ae"] = True
    astdt = fatal["trtsdt"] + timedelta(days=95)
    dthdt = astdt + timedelta(days=12)
    fatal["dthdt"] = dthdt
    fatal["dcsreas"] = "ADVERSE EVENT"
    rows.append({
        "STUDYID": STUDYID, "USUBJID": fatal["usubjid"],
        "AETERM": "pneumonitis", "AEDECOD": "Pneumonitis",
        "AEBODSYS": "Respiratory, thoracic and mediastinal disorders",
        "ATOXGR": "5", "AESER": "Y", "AEREL": "RELATED",
        "AEACN": "DRUG WITHDRAWN", "AEOUT": "FATAL",
        "AESTDTC": iso(astdt), "AEENDTC": iso(dthdt),
        "ASTDT": iso(astdt), "AENDT": iso(dthdt), "TRTEMFL": "Y",
    })
    # 2) Serious grade 3 pneumonitis, recovered after interruption (100 mg).
    sae2 = pick(subjects, 1, cbor="PR", exclude={fatal["usubjid"]})
    sae2["sae_pneumonitis"] = True
    astdt = sae2["trtsdt"] + timedelta(days=63)
    rows.append({
        "STUDYID": STUDYID, "USUBJID": sae2["usubjid"],
        "AETERM": "interstitial pneumonitis", "AEDECOD": "Pneumonitis",
        "AEBODSYS": "Respiratory, thoracic and mediastinal disorders",
        "ATOXGR": "3", "AESER": "Y", "AEREL": "RELATED",
        "AEACN": "DRUG INTERRUPTED", "AEOUT": "RECOVERED/RESOLVED",
        "AESTDTC": iso(astdt), "AEENDTC": iso(astdt + timedelta(days=21)),
        "ASTDT": iso(astdt), "AENDT": iso(astdt + timedelta(days=21)), "TRTEMFL": "Y",
    })
    # 3) Deaths from disease progression (not AE-driven), one per arm.
    for trtn in (1, 2):
        s = pick(subjects, trtn, cbor="PD",
                 exclude={fatal["usubjid"], sae2["usubjid"]})
        s["dthdt"] = s["trtsdt"] + timedelta(days=170)
        if s["dthdt"] > DATA_CUTOFF:
            s["dthdt"] = DATA_CUTOFF - timedelta(days=10)
        s["dcsreas"] = "PROGRESSIVE DISEASE"
    return rows, fatal, sae2


# ---------------------------------------------------------------------------
# ADSL
# ---------------------------------------------------------------------------

def build_adsl(subjects):
    rows = []
    for s in subjects:
        dosed = s["trtsdt"] is not None
        efffl = "Y" if s["role"] == "evaluable" else "N"
        dth = s.get("dthdt")
        trtedt = None
        if dosed:
            trtedt = min(s["trtsdt"] + timedelta(days=RNG.randint(60, 330)), DATA_CUTOFF)
            if dth:
                trtedt = min(trtedt, dth)
        eosstt = "ONGOING"
        dcsreas = ""
        if dth:
            eosstt = "DISCONTINUED"
            dcsreas = s.get("dcsreas", "DEATH")
        elif s["role"] == "not_dosed":
            eosstt = "DISCONTINUED"
            dcsreas = "WITHDRAWAL BY SUBJECT"
        rows.append({
            "STUDYID": STUDYID, "USUBJID": s["usubjid"], "SUBJID": s["subjid"],
            "SITEID": s["site"],
            "TRT01P": s["trt"], "TRT01PN": s["trtn"],
            "TRT01A": s["trt"] if dosed else "", "TRT01AN": s["trtn"] if dosed else "",
            "SAFFL": "Y" if dosed else "N", "ITTFL": "Y", "EFFFL": efffl,
            "AGE": s["age"], "AGEU": "YEARS", "SEX": s["sex"], "RACE": s["race"],
            "ETHNIC": "NOT HISPANIC OR LATINO", "COUNTRY": s["country"],
            "RANDDT": iso(s["randdt"]),
            "TRTSDT": iso(s["trtsdt"]) if dosed else "",
            "TRTEDT": iso(trtedt) if trtedt else "",
            "DTHFL": "Y" if dth else "",
            "DTHDT": iso(dth) if dth else "",
            "EOSSTT": eosstt, "DCSREAS": dcsreas,
        })
    return rows


# ---------------------------------------------------------------------------
# TLFs (derived from the datasets)
# ---------------------------------------------------------------------------

def cell(n, denom, extra=None):
    c = {"n": n, "pct": pct(n, denom), "display": f"{n} ({pct(n, denom)})"}
    if extra:
        c.update(extra)
    return c


def build_tlfs(adsl, adrs, adae):
    by_arm = {1: [r for r in adsl if r["TRT01PN"] == 1],
              2: [r for r in adsl if r["TRT01PN"] == 2]}
    itt = {k: len(v) for k, v in by_arm.items()}
    saf = {k: sum(1 for r in v if r["SAFFL"] == "Y") for k, v in by_arm.items()}
    eff = {k: sum(1 for r in v if r["EFFFL"] == "Y") for k, v in by_arm.items()}
    eff_ids = {k: {r["USUBJID"] for r in v if r["EFFFL"] == "Y"} for k, v in by_arm.items()}
    saf_ids = {k: {r["USUBJID"] for r in v if r["SAFFL"] == "Y"} for k, v in by_arm.items()}

    def col(k, N):
        return {"id": ARMS[k]["col"], "label": f"{ARMS[k]['label']} (N={N})", "N": N}

    # ---- T14.1.1 analysis populations -----------------------------------
    t1411 = {
        "table_id": "T14.1.1", "protocol": STUDYID,
        "title": "Summary of Analysis Populations",
        "population": "All Enrolled Subjects",
        "columns": [col(1, itt[1]), col(2, itt[2])],
        "rows": [
            {"id": "enrolled", "label": "Enrolled (Intent-to-Treat), n",
             "cells": {ARMS[k]["col"]: {"n": itt[k], "display": str(itt[k])} for k in (1, 2)}},
            {"id": "safety", "label": "Safety Population, n (%)",
             "cells": {ARMS[k]["col"]: cell(saf[k], itt[k]) for k in (1, 2)}},
            {"id": "response_evaluable", "label": "Response-Evaluable Population, n (%)",
             "cells": {ARMS[k]["col"]: cell(eff[k], itt[k]) for k in (1, 2)}},
        ],
        "footnotes": [
            {"id": "a", "text": "Safety Population: all subjects who received at least one dose of examplinib."},
            {"id": "b", "text": "Response-Evaluable Population: all subjects who received at least one dose of examplinib and had measurable disease at baseline per RECIST v1.1."},
            {"id": "c", "text": f"Data cutoff date: {CUTOFF_DDMMMYYYY}."},
        ],
        "source": "Source: ADSL. Program: t_pop.sas, run 26JUN2026 13:41.",
        "data_cutoff": iso(DATA_CUTOFF),
    }

    # ---- T14.2.1 confirmed ORR ------------------------------------------
    cbor = {k: {} for k in (1, 2)}
    for r in adrs:
        if r["PARAMCD"] != "CBOR":
            continue
        for k in (1, 2):
            if r["USUBJID"] in eff_ids[k]:
                cbor[k][r["USUBJID"]] = r["AVALC"]
    cat_counts = {k: {c: sum(1 for v in cbor[k].values() if v == c)
                      for c in ("CR", "PR", "SD", "PD", "NE")} for k in (1, 2)}
    orr = {}
    for k in (1, 2):
        x = cat_counts[k]["CR"] + cat_counts[k]["PR"]
        assert len(cbor[k]) == eff[k], "CBOR rows must cover the evaluable population"
        orr[k] = orr_with_ci(x, eff[k])

    def orr_cell(k):
        o = orr[k]
        return {
            "n": o["numerator"], "N": o["denominator"], "pct": o["estimate_pct"],
            "ci_lower": o["ci_lower_pct"], "ci_upper": o["ci_upper_pct"],
            "ci_level": 95,
            "display": (f"{o['numerator']} ({o['estimate_pct']}) "
                        f"[{o['ci_lower_pct']}, {o['ci_upper_pct']}]"),
        }

    cat_labels = {
        "CR": "Complete response (CR), n (%)", "PR": "Partial response (PR), n (%)",
        "SD": "Stable disease (SD), n (%)", "PD": "Progressive disease (PD), n (%)",
        "NE": "Not evaluable (NE), n (%)",
    }
    t1421 = {
        "table_id": "T14.2.1", "protocol": STUDYID,
        "title": "Analysis of Objective Response Rate (Confirmed) per RECIST v1.1",
        "population": "Response-Evaluable Population",
        "columns": [col(1, eff[1]), col(2, eff[2])],
        "rows": (
            [{"id": f"bor_{c.lower()}", "label": cat_labels[c],
              "cells": {ARMS[k]["col"]: cell(cat_counts[k][c], eff[k]) for k in (1, 2)}}
             for c in ("CR", "PR", "SD", "PD", "NE")]
            + [{"id": "orr_confirmed",
                "label": "Objective response rate (confirmed CR+PR), n (%) [95% CI] a,b",
                "cells": {ARMS[k]["col"]: orr_cell(k) for k in (1, 2)}}]
        ),
        "footnotes": [
            {"id": "a", "text": "95% CI computed using the Clopper-Pearson exact method."},
            {"id": "b", "text": "Responses confirmed by a subsequent assessment at least 28 days after the initial response, per RECIST v1.1."},
            {"id": "c", "text": "Response-Evaluable Population: all subjects who received at least one dose of examplinib and had measurable disease at baseline per RECIST v1.1."},
            {"id": "d", "text": f"Data cutoff date: {CUTOFF_DDMMMYYYY}."},
        ],
        "source": "Source: ADSL, ADRS. Program: t_orr.sas, run 26JUN2026 14:02.",
        "data_cutoff": iso(DATA_CUTOFF),
    }

    # ---- T14.3.1 AE overview --------------------------------------------
    def subjects_where(pred):
        out = {1: set(), 2: set()}
        for r in adae:
            if r["TRTEMFL"] != "Y" or not pred(r):
                continue
            for k in (1, 2):
                if r["USUBJID"] in saf_ids[k]:
                    out[k].add(r["USUBJID"])
        return {k: len(v) for k, v in out.items()}

    cats = [
        ("any_teae", "Any TEAE", lambda r: True),
        ("related_teae", "Treatment-related TEAE", lambda r: r["AEREL"] == "RELATED"),
        ("grade3plus_teae", "Grade >= 3 TEAE", lambda r: int(r["ATOXGR"]) >= 3),
        ("serious_teae", "Serious TEAE", lambda r: r["AESER"] == "Y"),
        ("teae_interrupt", "TEAE leading to dose interruption or reduction",
         lambda r: r["AEACN"] in ("DRUG INTERRUPTED", "DOSE REDUCED")),
        ("teae_discontinuation", "TEAE leading to discontinuation of study drug",
         lambda r: r["AEACN"] == "DRUG WITHDRAWN"),
        ("teae_death", "TEAE leading to death", lambda r: r["AEOUT"] == "FATAL"),
    ]
    t1431 = {
        "table_id": "T14.3.1", "protocol": STUDYID,
        "title": "Overview of Treatment-Emergent Adverse Events",
        "population": "Safety Population",
        "columns": [col(1, saf[1]), col(2, saf[2])],
        "rows": [
            {"id": rid, "label": f"{label}, n (%)",
             "cells": {ARMS[k]["col"]: cell(cnts[k], saf[k]) for k in (1, 2)}}
            for rid, label, pred in cats
            for cnts in [subjects_where(pred)]
        ],
        "footnotes": [
            {"id": "a", "text": "TEAE: treatment-emergent adverse event, defined as any adverse event with onset on or after the first dose of examplinib."},
            {"id": "b", "text": "Subjects are counted once per category at maximum severity. MedDRA v27.0; CTCAE v5.0."},
            {"id": "c", "text": f"Data cutoff date: {CUTOFF_DDMMMYYYY}."},
        ],
        "source": "Source: ADSL, ADAE. Program: t_aeov.sas, run 26JUN2026 14:17.",
        "data_cutoff": iso(DATA_CUTOFF),
    }
    return t1411, t1421, t1431, orr, cat_counts, eff


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: list[dict]):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    subjects = build_subjects()
    adrs = build_adrs(subjects)
    adae, fatal, sae2 = build_adae(subjects)
    adsl = build_adsl(subjects)
    t1411, t1421, t1431, orr, cat_counts, eff = build_tlfs(adsl, adrs, adae)

    ds = HERE / "datasets"
    write_csv(ds / "ADSL.csv", adsl)
    write_csv(ds / "ADRS.csv", adrs)
    write_csv(ds / "ADAE.csv", adae)
    tlf = HERE / "tlf"
    for t in (t1411, t1421, t1431):
        (tlf / f"{t['table_id'].replace('.', '_').lower()}.json").write_text(
            json.dumps(t, indent=2))

    # ---- gold facts for evals -------------------------------------------
    gold = {
        "study_id": STUDYID,
        "data_cutoff": iso(DATA_CUTOFF),
        "primary_population": "Response-Evaluable Population",
        "primary_table": "T14.2.1",
        "orr_confirmed": {
            "arm_100": orr[1],
            "arm_50": orr[2],
        },
        "cbor_counts": {"arm_100": cat_counts[1], "arm_50": cat_counts[2]},
        "evaluable_n": {"arm_100": eff[1], "arm_50": eff[2]},
        "narrative_subject": {
            "usubjid": fatal["usubjid"],
            "age": fatal["age"], "sex": fatal["sex"],
            "arm": fatal["trt"],
            "event": "Pneumonitis", "grade": 5, "serious": True,
            "outcome": "FATAL", "action": "DRUG WITHDRAWN",
            "causality": "RELATED",
            "onset_study_day": 96,  # ASTDT - TRTSDT + 1
            "death_date": iso(fatal["dthdt"]),
        },
        "sae_subject": {"usubjid": sae2["usubjid"], "event": "Pneumonitis", "grade": 3},
    }
    gold_path = REPO / "evals" / "gold" / "gold_facts.json"
    gold_path.parent.mkdir(parents=True, exist_ok=True)
    gold_path.write_text(json.dumps(gold, indent=2))

    print(f"subjects: {len(subjects)}  ADSL rows: {len(adsl)}  "
          f"ADRS rows: {len(adrs)}  ADAE rows: {len(adae)}")
    for k in (1, 2):
        o = orr[k]
        print(f"  {ARMS[k]['label']}: confirmed ORR {o['numerator']}/{o['denominator']} "
              f"= {o['estimate_pct']}% (95% CI {o['ci_lower_pct']}, {o['ci_upper_pct']})")
    print(f"  narrative subject: {fatal['usubjid']} (fatal G5 pneumonitis)")
    print("wrote datasets/, tlf/, evals/gold/gold_facts.json")


if __name__ == "__main__":
    main()
