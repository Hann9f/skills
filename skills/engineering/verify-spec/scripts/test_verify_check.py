#!/usr/bin/env python3
"""Tests for verify_check.py: python3 -m unittest test_verify_check.py (run from scripts/)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import verify_check as vc

SPEC = """# Spec
## User Stories
1. A user can sign in and see the quantity.
2. An operator adjustment increases stock exactly once.
## Out of Scope
Mobile apps are out of scope.
"""


def run_cli(*args) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = vc.main([str(a) for a in args])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            err.write(str(exc.code))
    return code, out.getvalue(), err.getvalue()


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vc-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "app.py").write_text("print('v1')\n")
        (self.repo / ".gitignore").write_text(".env\nbuild/\n")
        (self.repo / ".env").write_text("TOKEN=fixture\n")
        self.spec = self.tmp / "spec.md"
        self.spec.write_text(SPEC)
        self.root = self.tmp / "evidence"

    def freeze(self, kind="initial", *extra) -> tuple[int, Path | None, str]:
        spec = ["--spec", self.spec] if kind == "initial" else []
        code, out, err = run_cli("freeze", "--repo", self.repo, *spec, "--lineage", "feat",
                                 "--kind", kind, "--root", self.root, *extra)
        return code, (Path(out.strip()) if code == 0 else None), err

    def new_run(self, kind="initial", *extra) -> Path:
        code, run, err = self.freeze(kind, *extra)
        self.assertEqual(code, 0, err)
        self.assertEqual(run_cli("export", run)[0], 0)
        return run

    def write(self, run: Path, rel: str, text: str) -> str:
        path = run / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return rel

    def results(self, run: Path, ac2="PASS", suite_skipped=0, binding=True, red=True, **overrides) -> dict:
        manifest = json.loads((run / "manifest.json").read_text())
        ev = self.write(run, "evidence/http.txt", "HTTP/1.0 200 OK\nquantity 15\n")
        suite = self.write(run, "evidence/suite.txt", "Ran 4 tests OK\n")
        red_ev = self.write(run, "evidence/red.txt", "mutated: exit 1; restored: exit 0\n")
        repro = self.write(run, "probes/repro_ac2.py", "print('replay applies twice')\n")
        self.write(run, "probes/p.py", "assert page_quantity(port) == 15, 'stock must read 15'\n")
        checks = [
            {"id": "C-1", "kind": "http", "service": "app", "real": True, "cmd": "python3 probes/p.py login",
             "exit_code": 0, "status": "PASS", "evidence": [ev]},
            {"id": "C-2", "kind": "http", "service": "app", "real": True, "cmd": "python3 probes/p.py adjust",
             "exit_code": 0 if ac2 == "PASS" else 1, "status": "PASS" if ac2 == "PASS" else "FAIL", "evidence": [ev]},
            {"id": "C-3", "kind": "suite", "real": True, "cmd": "python3 -m unittest", "exit_code": 0,
             "counts": {"executed": 4, "failed": 0, "skipped": suite_skipped}, "mandatory": True,
             "mandatory_source": "README: CI runs unittest", "status": "PASS", "evidence": [suite]},
        ]
        data = {
            "run_id": manifest["run_id"], "candidate_tree": manifest["candidate"]["tree"],
            "binding": [{"service": "app", "cmd": "python3 app.py", "cwd": str(run / "checkout"), "pid": 1}]
            if binding else [],
            "criteria": [
                {"id": "AC-001", "quote": "A user can sign in and see the quantity.", "status": "PASS", "checks": ["C-1"]},
                {"id": "AC-002", "quote": "An operator adjustment increases stock exactly once.", "status": ac2,
                 "checks": ["C-2"], **({} if ac2 == "PASS" else {"reason": "replay applied twice"})},
            ],
            "checks": checks,
            "red_controls": [{"check": "C-1", "mutation": "set stock to 3 in the fixture db", "mutated_exit": 1,
                              "restored_exit": 0, "evidence": [red_ev]}] if red else [],
            "defects": [] if ac2 != "FAIL" else [{"row": "AC-002", "title": "replay double-applies",
                                                  "expected": "15", "observed": "20", "repro": repro}],
            "limits": [],
        }
        data.update(overrides)
        (run / "results.json").write_text(json.dumps(data))
        return data

    def audit(self, run: Path, status="AUDIT_PASS", **overrides):
        seal_sha = vc.sha256_file(run / "seal.json")
        replay = self.write(run, "audit/replay.txt", "replayed C-1: quantity 15\n")
        results = json.loads((run / "results.json").read_text())
        quotes = {"C-3": "Ran 4 tests OK"}
        criteria = results.get("criteria", []) if isinstance(results, dict) else []
        reviewed = [{"row": c["id"], "check": c["checks"][0], "evidence_quote": quotes.get(c["checks"][0], "quantity 15"),
                     "probe_quote": "stock must read 15"}
                    for c in criteria if isinstance(c, dict) and c.get("status") == "PASS" and c.get("checks")]
        data = {"status": status, "seal_sha256": seal_sha, "findings": [], "missing_obligations": [],
                "reviewed": reviewed,
                "replayed": [{"check": "C-1", "matched": True, "evidence": [replay]}],
                "red_controls_genuine": True}
        data.update(overrides)
        self.write(run, "audit/audit.json", json.dumps(data))

    def finish(self, run: Path, audit=True, **audit_overrides) -> dict:
        self.assertEqual(run_cli("seal", run)[0], 0)
        if audit:
            self.audit(run, **audit_overrides)
        code, out, err = run_cli("verdict", run)
        verdict = json.loads((run / "verdict.json").read_text())
        self.assertEqual(code, vc.EXIT_NEXT[verdict["next"]], err)
        return verdict


class VerdictTests(Harness):
    def test_clean_pass(self):
        run = self.new_run()
        self.results(run)
        verdict = self.finish(run)
        self.assertEqual((verdict["headline"], verdict["next"]), ("PASS", "continue"), verdict)
        self.assertTrue((run / "report.md").read_text().startswith("# Verification report"))

    def test_pass_needs_audit_first(self):
        run = self.new_run()
        self.results(run)
        verdict = self.finish(run, audit=False)
        self.assertEqual((verdict["headline"], verdict["next"]), ("PENDING_AUDIT", "audit"))
        self.audit(run)
        code, _, _ = run_cli("verdict", run)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((run / "verdict.json").read_text())["headline"], "PASS")

    def test_fail_routes_to_fix_without_audit(self):
        run = self.new_run()
        self.results(run, ac2="FAIL")
        verdict = self.finish(run, audit=False)
        self.assertEqual((verdict["headline"], verdict["next"]), ("FAIL", "fix"))
        self.assertEqual(verdict["defects"][0]["id"], "DEF-AC-002")

    def test_fail_beats_blocked(self):
        run = self.new_run()
        data = self.results(run, ac2="FAIL")
        data["criteria"][0].update(status="BLOCKED", reason="db down")
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run, audit=False)["headline"], "FAIL")

    def test_skipped_suite_is_not_proof(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][0]["checks"] = ["C-3"]
        data["checks"][2]["counts"]["skipped"] = 1
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED", verdict)

    def test_zero_executed_suite_blocks_gate(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"][2]["counts"] = {"executed": 0, "failed": 0, "skipped": 1, }
        data["checks"][2]["skipped_reason"] = "placeholder"
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertIn("C-3", " ".join(verdict["reasons"]))

    def test_mocked_check_is_not_proof(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"][1]["real"] = False
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_unbound_service_blocks(self):
        run = self.new_run()
        self.results(run, binding=False)
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_binding_outside_export_is_integrity_error(self):
        run = self.new_run()
        data = self.results(run)
        data["binding"][0]["cwd"] = str(self.repo)
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertTrue(any("not inside" in p for p in verdict["problems"]))

    def test_missing_red_control_blocks(self):
        run = self.new_run()
        self.results(run, red=False)
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_invented_evidence_blocks(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"][0]["evidence"] = ["evidence/never-written.txt"]
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_quote_must_come_from_spec(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][1]["quote"] = "Adjustments are fine."
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_spec_question_is_inconclusive_and_not_retryable(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][1] = {"id": "AC-002", "quote": "increases stock exactly once", "status": "NEEDS_SPEC_DECISION",
                               "checks": [], "question": "per request or per key?"}
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run, audit=False)
        self.assertEqual((verdict["headline"], verdict["next"], verdict["retry_allowed"]), ("INCONCLUSIVE", "stop", False))

    def test_not_applicable_needs_exclusion_quote(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"].append({"id": "AC-003", "quote": "Mobile apps are out of scope.", "status": "NOT_APPLICABLE",
                                 "checks": [], "exclusion_quote": "Mobile apps are out of scope."})
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "PASS")

    def test_edited_checkout_is_integrity_error(self):
        run = self.new_run()
        self.results(run)
        (run / "checkout" / "app.py").write_text("print('patched by verifier')\n")
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertTrue(any("differs" in p for p in verdict["problems"]))

    def test_undeclared_generated_file_is_flagged(self):
        run = self.new_run()
        self.results(run)
        (run / "checkout" / "stray.txt").write_text("x\n")
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")
        run2 = self.new_run("retry")
        self.results(run2, generated=[{"path": "stray.txt", "reason": "app cache"}])
        (run2 / "checkout" / "stray.txt").write_text("x\n")
        self.assertEqual(self.finish(run2)["headline"], "PASS")

    def test_gitignored_build_output_is_fine(self):
        run = self.new_run()
        self.results(run)
        (run / "checkout" / "build").mkdir()
        (run / "checkout" / "build" / "out.bin").write_text("x")
        self.assertEqual(self.finish(run)["headline"], "PASS")

    def test_evidence_changed_after_seal(self):
        run = self.new_run()
        self.results(run)
        self.assertEqual(run_cli("seal", run)[0], 0)
        self.audit(run)
        (run / "evidence" / "http.txt").write_text("edited after seal\n")
        run_cli("verdict", run)
        self.assertEqual(json.loads((run / "verdict.json").read_text())["headline"], "BLOCKED")

    def test_audit_must_name_current_seal(self):
        run = self.new_run()
        self.results(run)
        verdict = self.finish(run, seal_sha256="0" * 64)
        self.assertEqual((verdict["headline"], verdict["audit"]["state"]), ("BLOCKED", "invalid"))

    def test_audit_quotes_must_exist(self):
        run = self.new_run()
        self.results(run)
        lazy = [{"row": "AC-001", "check": "C-1", "evidence_quote": "quantity 99", "probe_quote": "stock must read 15"},
                {"row": "AC-002", "check": "C-2", "evidence_quote": "quantity 15", "probe_quote": "stock must read 15"}]
        self.assertEqual(self.finish(run, reviewed=lazy)["headline"], "BLOCKED")

    def test_lazy_audit_rejected(self):
        variants = [
            {"reviewed": [{"row": "AC-001", "check": "C-1", "evidence_quote": "OK", "probe_quote": "OK"}]},
            {"reviewed": [{"row": r, "check": "C-2", "evidence_quote": "quantity 15", "probe_quote": "stock must read 15"}
                          for r in ("AC-001", "AC-002")]},
            {"replayed": [{"check": "NOPE", "matched": True, "evidence": ["audit/replay.txt"]}]},
            {"replayed": [{"check": "C-1", "matched": True, "evidence": ["evidence/http.txt"]}]},
        ]
        for number, overrides in enumerate(variants):
            with self.subTest(number=number):
                self.root = self.tmp / f"evidence-lazy-{number}"
                run = self.new_run()
                self.results(run)
                verdict = self.finish(run, **overrides)
                self.assertEqual((verdict["headline"], verdict["audit"]["state"]), ("BLOCKED", "invalid"))

    def test_audit_findings_block_pass(self):
        run = self.new_run()
        self.results(run)
        verdict = self.finish(run, status="AUDIT_FINDINGS",
                              findings=[{"rows": ["AC-002"], "detail": "check asserts only 200", "must_fix": True}])
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertEqual(verdict["next"], "stop")
        self.assertTrue(verdict["retry_allowed"])


    def test_accepted_risk_needs_human_entry_for_this_tree(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][1].update(status="BLOCKED", reason="payment sandbox down")
        (run / "results.json").write_text(json.dumps(data))
        ledger_path = run.parent / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        tree = json.loads((run / "manifest.json").read_text())["candidate"]["tree"]
        ledger["acceptances"] = [{"row": "AC-002", "candidate_tree": tree, "decided_by": "Ana (product owner)",
                                  "reason": "sandbox outage; covered by staging smoke"}]
        ledger_path.write_text(json.dumps(ledger))
        self.assertEqual(self.finish(run)["headline"], "PASS_WITH_ACCEPTED_RISK")

    def test_pass_on_drifted_repo_stops(self):
        run = self.new_run()
        self.results(run)
        (self.repo / "app.py").write_text("print('changed after freeze')\n")
        verdict = self.finish(run)
        self.assertEqual((verdict["headline"], verdict["next"]), ("PASS", "stop"))

    def test_dropped_obligation_is_integrity_error(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"] = data["criteria"][:1]
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertTrue(any("no criterion quoting" in p for p in verdict["problems"]))

    def test_trivial_quote_rejected(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][0]["quote"] = "the"
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_acceptance_cannot_waive_fail(self):
        run = self.new_run()
        self.results(run, ac2="FAIL")
        ledger_path = run.parent / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        tree = json.loads((run / "manifest.json").read_text())["candidate"]["tree"]
        ledger["acceptances"] = [{"row": "AC-002", "candidate_tree": tree, "decided_by": "x", "reason": "y"}]
        ledger_path.write_text(json.dumps(ledger))
        self.assertEqual(self.finish(run, audit=False)["headline"], "FAIL")

    def test_malformed_results(self):
        run = self.new_run()
        (run / "results.json").write_text("[1, 2]")
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_accept_command(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][1].update(status="BLOCKED", reason="payment sandbox down")
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertIn("payment sandbox down", " ".join(verdict["reasons"]))
        code, _, err = run_cli("accept", run, "--row", "AC-001", "--by", "Ana", "--reason", "x")
        self.assertNotEqual(code, 0, "a PASS row is not an open gap")
        code, _, err = run_cli("accept", run, "--row", "AC-002", "--by", "Ana (owner)", "--reason", "sandbox outage")
        self.assertEqual(code, 0, err)
        run_cli("verdict", run)
        verdict = json.loads((run / "verdict.json").read_text())
        self.assertEqual((verdict["headline"], verdict["next"]), ("PASS_WITH_ACCEPTED_RISK", "continue"))

    def test_unhashable_status_does_not_crash(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][0]["status"] = ["PASS"]
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertTrue(verdict["retry_allowed"])

    def test_non_dict_audit_does_not_crash(self):
        run = self.new_run()
        self.results(run)
        run_cli("seal", run)
        self.write(run, "audit/audit.json", "[1]")
        code, _, err = run_cli("verdict", run)
        self.assertEqual(json.loads((run / "verdict.json").read_text())["headline"], "BLOCKED", err)

    def test_dead_verifier_can_be_sealed_and_retried(self):
        run = self.new_run()
        self.assertEqual(run_cli("seal", run)[0], 0)
        run_cli("verdict", run)
        verdict = json.loads((run / "verdict.json").read_text())
        self.assertEqual((verdict["headline"], verdict["next"], verdict["retry_allowed"]), ("BLOCKED", "stop", True))
        self.assertEqual(self.freeze("retry")[0], 0)

    def test_all_not_applicable_or_static_cannot_pass(self):
        run = self.new_run()
        data = self.results(run, red=False)
        for c in data["criteria"]:
            c.update(status="PASS", static=True, checks=["C-9"])
        data["checks"].append({"id": "C-9", "kind": "inspection", "status": "PASS", "evidence": ["evidence/http.txt"]})
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertIn("RUNTIME", " ".join(verdict["reasons"]))

    def test_wide_quote_cannot_cover_many_stories(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"] = [{"id": "AC-001", "status": "PASS", "checks": ["C-1"],
                             "quote": ["A user can sign in and see the quantity.",
                                       "An operator adjustment increases stock exactly once.",
                                       "Mobile apps are out of scope.", "## User Stories 1. A user"]}]
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_mandatory_suite_skips_need_base_comparison(self):
        base = vc.identity(self.repo)["tree"]
        run = self.new_run("initial", "--base", base)
        data = self.results(run, suite_skipped=1)
        data["checks"][2].update(skipped_reason="browser placeholder", base_counts={"executed": 4, "failed": 0, "skipped": 1})
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED", "base_counts without base_evidence")
        run2 = self.new_run("retry")
        data = self.results(run2, suite_skipped=1)
        base_ev = self.write(run2, "evidence/suite-base.txt", "Ran 4 tests OK (skipped=1)\n")
        data["checks"][2].update(skipped_reason="browser placeholder", base_evidence=[base_ev],
                                 base_counts={"executed": 4, "failed": 0, "skipped": 1})
        (run2 / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run2)["headline"], "PASS")

    def test_base_counts_need_a_frozen_base(self):
        run = self.new_run()
        data = self.results(run, suite_skipped=39)
        ev = self.write(run, "evidence/suite-base.txt", "whatever\n")
        data["checks"][2].update(skipped_reason="x", base_evidence=[ev], base_counts={"executed": 40, "failed": 0, "skipped": 99})
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_uncited_failing_check_is_not_swallowed(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"].append({"id": "RISK-001", "kind": "http", "service": "app", "real": True, "cmd": "p risk",
                               "exit_code": 1, "status": "FAIL", "evidence": ["evidence/http.txt"]})
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run, audit=False)
        self.assertNotIn(verdict["headline"], ("PASS", "PENDING_AUDIT"))
        self.assertIn("RISK-001", " ".join(verdict["reasons"]))

    def test_review_must_name_a_proving_check(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"].append({"id": "C-9", "kind": "inspection", "status": "PASS", "evidence": ["evidence/http.txt"]})
        data["criteria"][0]["checks"] = ["C-1", "C-9"]
        (run / "results.json").write_text(json.dumps(data))
        lazy = [{"row": "AC-001", "check": "C-9", "evidence_quote": "quantity 15", "probe_quote": "stock must read 15"},
                {"row": "AC-002", "check": "C-2", "evidence_quote": "quantity 15", "probe_quote": "stock must read 15"}]
        self.assertEqual(self.finish(run, reviewed=lazy)["headline"], "BLOCKED")

    def test_docs_only_spec_passes_with_accepted_runtime(self):
        run = self.new_run()
        data = self.results(run, red=False)
        data["checks"].append({"id": "C-9", "kind": "inspection", "status": "PASS", "evidence": ["evidence/http.txt"]})
        for c in data["criteria"]:
            c.update(status="PASS", static=True, checks=["C-9"])
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run, audit=False)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertEqual(run_cli("accept", run, "--row", "RUNTIME", "--by", "Ana", "--reason", "docs-only spec")[0], 0)
        self.audit(run, reviewed=[{"row": r, "check": "C-9", "evidence_quote": "quantity 15",
                                   "probe_quote": "stock must read 15"} for r in ("AC-001", "AC-002")], replayed=[])
        run_cli("verdict", run)
        self.assertEqual(json.loads((run / "verdict.json").read_text())["headline"], "PASS_WITH_ACCEPTED_RISK")

    def test_report_survives_scalar_fields(self):
        run = self.new_run()
        data = self.results(run)
        data["limits"] = "none"
        data["checks"][0]["evidence"] = "evidence/http.txt"
        (run / "results.json").write_text(json.dumps(data))
        run_cli("seal", run)
        code, _, err = run_cli("verdict", run)
        self.assertIn(code, (0, 3, 4, 5), err)
        self.assertTrue((run / "report.md").is_file())

    def test_reseal_after_crash_is_idempotent(self):
        run = self.new_run()
        self.results(run)
        self.assertEqual(run_cli("seal", run)[0], 0)
        self.assertEqual(run_cli("seal", run)[0], 0)
        run_cli("verdict", run)
        self.assertNotEqual(run_cli("seal", run)[0], 0)

    def test_count_types_checked(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"][2]["counts"] = {"executed": "4", "failed": 0, "skipped": -1}
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_pycache_after_seal_is_ignored(self):
        run = self.new_run()
        self.results(run)
        run_cli("seal", run)
        self.audit(run)
        self.write(run, "probes/__pycache__/p.cpython-312.pyc", "bytecode")
        run_cli("verdict", run)
        self.assertEqual(json.loads((run / "verdict.json").read_text())["headline"], "PASS")

    @unittest.skipIf(os.name == "nt", "needs /proc and POSIX signals")
    def test_seal_refuses_live_service_from_run(self):
        run = self.new_run()
        data = self.results(run)
        proc = subprocess.Popen(["sleep", "30"], cwd=run / "checkout")
        try:
            data["binding"][0]["pid"] = proc.pid
            (run / "results.json").write_text(json.dumps(data))
            code, _, err = run_cli("seal", run)
            self.assertNotEqual(code, 0)
            self.assertIn("still running", err)
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(run_cli("seal", run)[0], 0)

    def test_evidence_in_checkout_rejected(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"][0]["evidence"] = ["checkout/app.py"]
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_bool_exits_rejected(self):
        run = self.new_run()
        data = self.results(run)
        data["red_controls"][0].update(mutated_exit=True, restored_exit=False)
        (run / "results.json").write_text(json.dumps(data))
        self.assertEqual(self.finish(run)["headline"], "BLOCKED")

    def test_audit_blocked_then_retry_failing_again_stops(self):
        run = self.new_run()
        self.results(run)
        verdict = self.finish(run, status="AUDIT_BLOCKED", findings=[{"rows": [], "detail": "auditor died"}])
        self.assertTrue(verdict["retry_allowed"])
        retry = self.new_run("retry")
        self.results(retry)
        verdict = self.finish(retry, status="AUDIT_FINDINGS", findings=[{"rows": ["AC-001"], "detail": "x", "must_fix": True}])
        self.assertEqual((verdict["headline"], verdict["next"], verdict["retry_allowed"]), ("BLOCKED", "stop", False))

    def test_explained_skips_still_are_not_proof(self):
        run = self.new_run()
        data = self.results(run)
        data["checks"].append({"id": "C-4", "kind": "suite", "real": True, "cmd": "pytest -k ac1", "exit_code": 0,
                               "counts": {"executed": 3, "failed": 0, "skipped": 1}, "skipped_reason": "flaky",
                               "status": "PASS", "evidence": ["evidence/suite.txt"]})
        data["criteria"][0]["checks"] = ["C-4"]
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run)
        self.assertEqual(verdict["headline"], "BLOCKED")
        self.assertIn("no runtime proof", " ".join(verdict["reasons"]))


class LoopTests(Harness):
    def fail_run(self, kind="initial") -> Path:
        run = self.new_run(kind)
        self.results(run, ac2="FAIL")
        self.finish(run, audit=False)
        return run

    def test_full_loop_fail_fix_pass(self):
        first = self.fail_run()
        (self.repo / "app.py").write_text("print('v2 fixed')\n")
        second = self.new_run("fix")
        manifest = json.loads((second / "manifest.json").read_text())
        self.assertEqual(manifest["prior"]["failed_rows"], ["AC-002"])
        self.assertEqual(manifest["prior"]["run_id"], first.name)
        self.results(second)
        verdict = self.finish(second)
        self.assertEqual((verdict["headline"], verdict["next"]), ("PASS", "continue"))

    def test_fix_cap(self):
        self.fail_run()
        for n in (2, 3):
            (self.repo / "app.py").write_text(f"print('attempt {n}')\n")
            run = self.new_run("fix")
            self.results(run, ac2="FAIL")
            verdict = self.finish(run, audit=False)
        self.assertEqual((verdict["headline"], verdict["next"]), ("FAIL", "stop"))
        (self.repo / "app.py").write_text("print('attempt 4')\n")
        code, _, err = self.freeze("fix")
        self.assertEqual(code, vc.REFUSED, err)
        code, _, err = self.freeze("initial")
        self.assertEqual(code, vc.REFUSED)
        self.assertIn("human-decision", err)
        code, _, err = self.freeze("initial", "--human-decision", "Owner allows two more attempts")
        self.assertEqual(code, 0, err)

    def test_fix_needs_changed_code(self):
        self.fail_run()
        code, _, err = self.freeze("fix")
        self.assertEqual(code, vc.REFUSED)
        self.assertIn("unchanged", err)

    def test_retry_rules(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][1].update(status="BLOCKED", reason="db container down")
        (run / "results.json").write_text(json.dumps(data))
        self.assertTrue(self.finish(run, audit=False)["retry_allowed"])
        retry = self.new_run("retry")
        data = self.results(retry)
        data["criteria"][1].update(status="BLOCKED", reason="still down")
        (retry / "results.json").write_text(json.dumps(data))
        verdict = self.finish(retry, audit=False)
        self.assertFalse(verdict["retry_allowed"])
        code, _, _ = self.freeze("retry")
        self.assertEqual(code, vc.REFUSED)

    def test_retry_refused_after_fail(self):
        self.fail_run()
        code, _, err = self.freeze("retry")
        self.assertEqual(code, vc.REFUSED)

    def test_unfinished_run_allows_one_retry(self):
        self.new_run()
        code, _, err = self.freeze("fix")
        self.assertEqual(code, vc.REFUSED)
        self.assertIn("no verdict yet", err)
        code, _, err = self.freeze("initial")
        self.assertEqual(code, vc.REFUSED)
        retry = self.new_run("retry")
        code, _, err = self.freeze("retry")
        self.assertEqual(code, vc.REFUSED, "a second dead run must not retry again")
        self.results(retry)
        self.assertEqual(self.finish(retry)["headline"], "PASS")

    def test_fix_run_inherits_base_overlay_notes(self):
        run = self.new_run("initial", "--overlay", ".env", "--note", "never touch prod",
                           "--base", vc.identity(self.repo)["tree"], "--depth", "focused")
        self.results(run, ac2="FAIL")
        self.finish(run, audit=False)
        (self.repo / "app.py").write_text("print('fixed')\n")
        fix = self.new_run("fix", "--note", "fix cycle 1")
        manifest = json.loads((fix / "manifest.json").read_text())
        self.assertEqual([o["path"] for o in manifest["overlay"]], [".env"])
        self.assertEqual(manifest["notes"], ["never touch prod", "fix cycle 1"])
        self.assertIsNotNone(manifest["base"])
        self.assertEqual(manifest["depth"], "focused")
        self.assertTrue((fix / "checkout" / ".env").is_file())

    def test_spec_question_blocks_retry_freeze(self):
        run = self.new_run()
        data = self.results(run)
        data["criteria"][1].update(status="NEEDS_SPEC_DECISION", question="per request or per key?")
        (run / "results.json").write_text(json.dumps(data))
        verdict = self.finish(run, audit=False)
        self.assertEqual(verdict["spec_questions"], [{"row": "AC-002", "question": "per request or per key?"}])
        self.assertIn("per request or per key?", " ".join(verdict["reasons"]))
        code, _, err = self.freeze("retry")
        self.assertEqual(code, vc.REFUSED)

    def test_pending_audit_blocks_next_freeze(self):
        run = self.new_run()
        self.results(run)
        self.finish(run, audit=False)
        code, _, _ = self.freeze("initial")
        self.assertEqual(code, vc.REFUSED)

    def test_new_round_after_pass_needs_no_decision(self):
        run = self.new_run()
        self.results(run)
        self.finish(run)
        (self.repo / "app.py").write_text("print('next feature')\n")
        code, _, err = self.freeze("initial")
        self.assertEqual(code, 0, err)

    def test_fix_and_retry_reuse_frozen_spec(self):
        first = self.fail_run()
        (self.repo / "app.py").write_text("print('v2')\n")
        code, _, err = run_cli("freeze", "--repo", self.repo, "--spec", self.spec, "--lineage", "feat",
                               "--kind", "fix", "--root", self.root)
        self.assertEqual(code, vc.REFUSED)
        self.spec.write_text(SPEC + "3. A late change nobody agreed to.\n")
        run = self.new_run("fix")
        self.assertEqual((run / "spec.md").read_text(), (first / "spec.md").read_text())

    def test_hand_edited_ledger_runs_are_ignored(self):
        self.fail_run()
        ledger_path = self.root / "feat" / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["runs"] = []
        ledger_path.write_text(json.dumps(ledger))
        code, _, err = self.freeze("initial")
        self.assertEqual(code, vc.REFUSED, err)


class IdentityTests(Harness):
    def test_uncommitted_identity_is_tree_and_leaves_index_alone(self):
        ident = vc.identity(self.repo)
        self.assertTrue(ident["uncommitted"])
        self.assertIsNone(ident["commit"])
        self.assertFalse((self.repo / ".git" / "index").exists())
        listing = subprocess.run(["git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", ident["tree"]],
                                 capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(sorted(listing), [".gitignore", "app.py"])

    def test_export_excludes_ignored_and_overlay_copies(self):
        run = self.new_run("initial", "--overlay", ".env")
        self.assertTrue((run / "checkout" / "app.py").is_file())
        self.assertEqual((run / "checkout" / ".env").read_text(), "TOKEN=fixture\n")
        run2_code, _, _ = run_cli("export", run)
        self.assertNotEqual(run2_code, 0)

    def test_evidence_root_inside_repo_refused(self):
        code, _, err = run_cli("freeze", "--repo", self.repo, "--spec", self.spec, "--lineage", "x",
                               "--root", self.repo / "evidence")
        self.assertNotEqual(code, 0)
        self.assertIn("outside the repository", err)

    def test_overlay_directory_applies_to_both_exports(self):
        (self.repo / "fixtures").mkdir()
        (self.repo / "fixtures" / "users.json").write_text("{}\n")
        (self.repo / ".gitignore").write_text(".env\nbuild/\nfixtures/\n")
        base = vc.identity(self.repo)["tree"]
        code, run, err = self.freeze("initial", "--overlay", "fixtures", "--base", base)
        self.assertEqual(code, 0, err)
        run_cli("export", run)
        run_cli("export", run, "--base")
        self.assertTrue((run / "checkout" / "fixtures" / "users.json").is_file())
        self.assertTrue((run / "base" / "fixtures" / "users.json").is_file())


if __name__ == "__main__":
    unittest.main()
