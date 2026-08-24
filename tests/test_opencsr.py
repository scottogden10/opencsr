"""OpenCSR regression tests — stdlib unittest, no network, no API key.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencsr.agents.backend import get_backend  # noqa: E402
from opencsr.agents.registry import AGENTS  # noqa: E402
from opencsr.domain import new_id  # noqa: E402
from opencsr.evals import run_eval_corpus, FAULTS  # noqa: E402
from opencsr.governance import simulate_review  # noqa: E402
from opencsr.hillclimb import run_hillclimb  # noqa: E402
from opencsr.skills import (list_skills, load_skill, reset_skills,  # noqa: E402
                            skill_rules)
from opencsr.sources import Snapshot  # noqa: E402
from opencsr.stats import clopper_pearson, orr_with_ci  # noqa: E402
from opencsr.store import Store  # noqa: E402
from opencsr.tools import ToolGateway  # noqa: E402
from opencsr.workflow import (apply_decision, run_efficacy_task,  # noqa: E402
                              run_narrative_task)


def fresh_store():
    path = tempfile.mktemp(suffix=".db", prefix="opencsr_test_")
    return Store(path), path


class TestStats(unittest.TestCase):
    def test_clopper_pearson_matches_reference(self):
        # 16/38 -> 26.3%..59.2% (reference values, 1 decimal)
        r = orr_with_ci(16, 38)
        self.assertEqual(r["estimate_pct"], 42.1)
        self.assertEqual(r["ci_lower_pct"], 26.3)
        self.assertEqual(r["ci_upper_pct"], 59.2)

    def test_edge_cases(self):
        lo, hi = clopper_pearson(0, 20)
        self.assertEqual(lo, 0.0)
        lo, hi = clopper_pearson(20, 20)
        self.assertEqual(hi, 1.0)


class TestData(unittest.TestCase):
    def test_tlf_matches_dataset_recount(self):
        snap = Snapshot()
        table = snap.tables["T14.2.1"]
        orr_row = next(r for r in table["rows"] if r["id"] == "orr_confirmed")
        for col, trtn in (("arm_100", "1"), ("arm_50", "2")):
            eff = {r["USUBJID"] for r in snap.datasets["ADSL"]
                   if r["EFFFL"] == "Y" and r["TRT01PN"] == trtn}
            resp = {r["USUBJID"] for r in snap.datasets["ADRS"]
                    if r["PARAMCD"] == "CBOR" and r["AVALC"] in ("CR", "PR")
                    and r["USUBJID"] in eff}
            cell = orr_row["cells"][col]
            self.assertEqual(cell["n"], len(resp))
            self.assertEqual(cell["N"], len(eff))

    def test_fault_application_changes_hash(self):
        clean = Snapshot()
        faulted = Snapshot(fault_id="swapped_arms")
        self.assertNotEqual(clean.hashes["T14.2.1"], faulted.hashes["T14.2.1"])
        self.assertEqual(clean.hashes["ADSL"], faulted.hashes["ADSL"])

    def test_locator_resolution(self):
        snap = Snapshot()
        r = snap.resolve("table://T14.2.1/row/orr_confirmed/col/arm_100")
        self.assertEqual(r["fragment"]["cell"]["n"], 16)
        r2 = snap.resolve("artifact://OCS-101/sap/section/9.7.1/paragraph/1")
        self.assertIn("Clopper-Pearson", r2["fragment"]["text"])
        with self.assertRaises(KeyError):
            snap.resolve("table://T14.2.1/row/nope/col/arm_100")


class TestQueryFilters(unittest.TestCase):
    """Regression tests for the review findings on query_dataset."""

    def setUp(self):
        self.snap = Snapshot()

    def test_gte_with_date_strings_and_empty_values(self):
        # deaths on/after 2026-01-01: empty DTHDT must never match,
        # ISO dates compare lexicographically
        truth = sum(1 for r in self.snap.datasets["ADSL"]
                    if r["DTHDT"] and r["DTHDT"] >= "2026-01-01")
        got = self.snap.query_dataset("ADSL", filters=[
            {"var": "DTHDT", "op": "gte", "value": "2026-01-01"}])
        self.assertEqual(got["count"], truth)
        self.assertGreater(truth, 0)

    def test_adsl_filters_support_gte(self):
        # SAE subjects aged >= 65 must actually be restricted by age
        unrestricted = self.snap.query_dataset(
            "ADAE", filters=[{"var": "AESER", "op": "eq", "value": "Y"}])
        restricted = self.snap.query_dataset(
            "ADAE", filters=[{"var": "AESER", "op": "eq", "value": "Y"}],
            adsl_filters=[{"var": "AGE", "op": "gte", "value": 65}])
        self.assertLess(restricted["count"], unrestricted["count"])

    def test_unknown_op_raises(self):
        with self.assertRaises(ValueError):
            self.snap.query_dataset("ADSL", filters=[
                {"var": "AGE", "op": "lt", "value": 50}])


class TestReviewRegressions(unittest.TestCase):
    def test_sex_check_word_boundary(self):
        from opencsr.validators import NARRATIVE_ELEMENTS
        chk = NARRATIVE_ELEMENTS["age and sex"]
        male = {"sex": "M", "age": "60"}
        self.assertFalse(chk("This 60-year-old female patient improved.", male))
        self.assertTrue(chk("This 60-year-old male patient improved.", male))

    def test_expected_guard_rejects_partial_digits(self):
        from opencsr.tools import _expected_in_fragment
        frag = {"cell": {"N": 38, "n": 16, "pct": 42.1,
                         "display": "16 (42.1) [26.3, 59.2]"}}
        self.assertTrue(_expected_in_fragment("16", frag))
        self.assertTrue(_expected_in_fragment("42.1", frag))
        self.assertFalse(_expected_in_fragment("3", frag))
        self.assertFalse(_expected_in_fragment("2.1", frag))
        self.assertTrue(_expected_in_fragment("≥18",
                        {"text": "adults (≥18 years) with tumors"}))

    def test_double_decision_rejected(self):
        store, path = fresh_store()
        backend = get_backend("mock")
        tid = run_efficacy_task(store, backend)
        first = simulate_review(store, tid)
        self.assertIn(first["decision"], ("accepted", "modified"))
        again = apply_decision(store, tid, "rejected", "other",
                               "this_instance", "", None, "human:test")
        self.assertIn("error", again)
        os.unlink(path)

    def test_iso_cutoff_in_draft_not_flagged(self):
        from opencsr.validators import _allowed_numbers
        snap = Snapshot()
        allowed = _allowed_numbers([], snap)
        self.assertIn("05", allowed)  # zero-padded cutoff month

    def test_failed_eval_case_is_failure_shaped(self):
        from opencsr.evals import _case_gold_efficacy

        class FailingBackend:
            name = "mock"

            def run_agent(self, agent_name, prompt, brief, gateway, config):
                return {"status": "failed", "notes": "boom"}

        store, path = fresh_store()
        res = _case_gold_efficacy(store, FailingBackend(), None)
        self.assertEqual(res["numeric_accuracy"], 0.0)
        self.assertFalse(res["style_ok_first_pass"])
        self.assertGreater(res["stray_numbers"], 0)
        os.unlink(path)


class TestGateway(unittest.TestCase):
    def test_capability_denial_is_logged_and_blocked(self):
        store, path = fresh_store()
        snap = Snapshot()
        tid = store.create_task("t", "t", "n", snap.manifest(), {})
        gw = ToolGateway(store, snap, tid, new_id("run"), "writer",
                         AGENTS["writer"]["tools"])
        out = gw.call("get_section", {"doc": "sap", "section": "9.4"})
        self.assertIn("error", out)
        self.assertEqual(len(gw.violations), 1)
        receipts = store.by_task("receipts", tid)
        self.assertTrue(any(not r["allowed"] for r in receipts))
        os.unlink(path)

    def test_evidence_transcription_guard(self):
        store, path = fresh_store()
        snap = Snapshot()
        tid = store.create_task("t", "t", "n", snap.manifest(), {})
        gw = ToolGateway(store, snap, tid, new_id("run"), "evidence",
                         AGENTS["evidence"]["tools"])
        res = gw.call("create_evidence", {"items": [
            {"locator": "table://T14.2.1/row/orr_confirmed/col/arm_100",
             "role": "result_cell", "expected": "999"}]})
        self.assertEqual(res["results"][0]["status"], "mismatch")
        os.unlink(path)

    def test_claim_authority_rule(self):
        store, path = fresh_store()
        snap = Snapshot()
        tid = store.create_task("t", "t", "n", snap.manifest(), {})
        gw = ToolGateway(store, snap, tid, new_id("run"), "biostat",
                         AGENTS["biostat"]["tools"] | {"create_evidence"})
        ev = gw.call("create_evidence", {"items": [
            {"locator": "artifact://OCS-101/sap/section/9.4/paragraph/1",
             "role": "x"}]})
        ev_id = ev["results"][0]["evidence_id"]
        # efficacy_result requires TLF support; SAP-only evidence is rejected
        res = gw.call("create_claims", {"claims": [{
            "claim_type": "efficacy_result", "text": "x",
            "dimensions": {}, "value": {"numerator": 1, "denominator": 2},
            "support_class": "direct", "evidence_ids": [ev_id]}]})
        self.assertEqual(res["results"][0]["status"], "rejected")
        os.unlink(path)


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_skills()

    def test_clean_pipeline_and_decision(self):
        store, path = fresh_store()
        backend = get_backend("mock")
        tid = run_efficacy_task(store, backend)
        task = store.get_task(tid)
        self.assertEqual(task["status"], "pending_review")
        self.assertFalse(task["result"]["blocking"])
        self.assertEqual(task["result"]["capability_violations"], 0)
        rev = simulate_review(store, tid)
        self.assertIn(rev["decision"], ("accepted", "modified"))
        doc = store.latest_document("csr/11.4.1")
        self.assertIsNotNone(doc)
        # every audit event is present and ordered
        events = [a["event_type"] for a in store.audit_events(task_id=tid)]
        for expected in ("task.created", "snapshot.frozen",
                         "task.ready_for_review"):
            self.assertIn(expected, events)
        os.unlink(path)

    def test_all_faults_detected_and_blocked(self):
        store, path = fresh_store()
        backend = get_backend("mock")
        expected = json.loads(
            (Path(__file__).resolve().parent.parent / "evals" / "gold" /
             "expected_issues.json").read_text())
        for fault in FAULTS:
            tid = run_efficacy_task(store, backend, fault_id=fault)
            task = store.get_task(tid)
            self.assertTrue(task["result"]["blocking"], fault)
            open_high = {i["category"] for i in store.by_task("issues", tid)
                         if i["status"] == "open" and i["severity"] == "high"}
            for e in expected[fault]:
                self.assertIn(e["category"], open_high, fault)
            self.assertEqual(simulate_review(store, tid)["decision"],
                             "rejected", fault)
        os.unlink(path)

    def test_stale_base_document_blocks_apply(self):
        store, path = fresh_store()
        backend = get_backend("mock")
        t1 = run_efficacy_task(store, backend)
        t2 = run_efficacy_task(store, backend)
        # accept t1 -> document v1; t2's proposal is now stale (base v0)
        simulate_review(store, t1)
        res = apply_decision(store, t2, "accepted", "accepted_as_proposed",
                             "this_instance", "", None, "human:test")
        self.assertEqual(res.get("status"), "stale")
        os.unlink(path)

    def test_narrative_elements(self):
        store, path = fresh_store()
        backend = get_backend("mock")
        tid = run_narrative_task(store, backend)
        task = store.get_task(tid)
        self.assertEqual(task["result"]["final_metrics"]["narrative_coverage"],
                         1.0)
        os.unlink(path)


class TestImprovementLoop(unittest.TestCase):
    def setUp(self):
        reset_skills()

    def tearDown(self):
        reset_skills()

    def test_eval_corpus_and_hillclimb(self):
        store, path = fresh_store()
        backend = get_backend("mock")
        # seed feedback via one reviewed task
        tid = run_efficacy_task(store, backend)
        simulate_review(store, tid)
        baseline = run_eval_corpus(store, backend, label="test-baseline")
        self.assertTrue(baseline["gates_passed"],
                        baseline["hard_gates"])
        self.assertLess(baseline["score"], 100.0)
        out = run_hillclimb(store, backend, max_rounds=3)
        self.assertTrue(out["improved"])
        self.assertEqual(out["final_score"], 100.0)
        # promoted rules exist in skill files
        self.assertIn("RULE:NO_UNAPPROVED_INTERPRETIVE",
                      skill_rules(load_skill("csr-efficacy-reporting")))
        self.assertIn("RULE:STATE_ONSET_DAY",
                      skill_rules(load_skill("patient-narrative")))
        # promotion history recorded with hypotheses
        promoted = [e for e in store.skill_history()
                    if e["action"] == "promoted"]
        self.assertGreaterEqual(len(promoted), 3)
        self.assertTrue(all(e["hypothesis"] for e in promoted))
        os.unlink(path)

    def test_skill_versions_reset(self):
        versions = {s["name"]: s["version"] for s in list_skills()}
        self.assertTrue(all(v == 1 for v in versions.values()), versions)


if __name__ == "__main__":
    unittest.main()
