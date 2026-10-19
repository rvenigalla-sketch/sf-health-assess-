"""Planner / validator / scorer regression tests (no Salesforce org, no AI).

Run: python -m unittest discover -s tests -v      (from the repo root)
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FIXTURE = REPO / "tests" / "fixtures" / "seeded-org"
sys.path.insert(0, str(SCRIPTS))

import diff_baseline  # noqa: E402
import plan_shards  # noqa: E402
import validate_findings as vf  # noqa: E402
from score import content_fingerprint, dedupe  # noqa: E402


def run_script(name, *args, ok=True):
    proc = subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)], cwd=SCRIPTS,
                          capture_output=True, text=True, encoding="utf-8")
    if ok and proc.returncode != 0:
        raise AssertionError(f"{name} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")
    return proc


def wjson(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


BASE = {"area": "apex", "category": "performance", "severity": "High", "evidence_ids": [], "basis": "llm-observation",
        "confidence": "high", "impact": "x", "recommendation": "y", "effort": "S", "related_components": []}


def finding(fid, component="ApexClass:A", title="SOQL inside a loop", quote="for (Account a : accounts) {", **kw):
    return {**BASE, "finding_id": fid, "shard_id": fid.split("#")[0], "component": component, "title": title,
            "evidence": [{"path": "metadata/A.cls", "line_start": 3, "line_end": 3, "quote": quote}], **kw}


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sfhealth3-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class StaleOutputsTest(TmpCase):
    def test_plan_clears_stale_raw_verdicts_and_derived_files(self):
        run = self.tmp / "Fixture" / "2026-01-01"
        shutil.copytree(FIXTURE, run)
        for sub in ("findings", "shards"):
            shutil.rmtree(run / sub, ignore_errors=True)
        run_script("build_index.py", "--run", run)
        run_script("rule_checks.py", "--run", run)
        f = run / "findings"
        wjson(f / "raw" / "apex-01.json", {"shard_id": "apex-01", "findings": []})
        wjson(f / "raw" / "followup-01.json", {"shard_id": "followup-01", "findings": []})
        wjson(f / "verdicts" / "apex-01__1.json", {"finding_id": "apex-01#1", "verdict": "refuted", "reason": "old"})
        wjson(f / "verdicts" / "_expected.json", {"apex-01#1": "abc"})
        (f / "validated.jsonl").write_text("{}\n", encoding="utf-8")
        wjson(f / "findings.json", {})
        run_script("plan_shards.py", "--run", run)
        self.assertFalse((f / "raw" / "apex-01.json").exists())
        self.assertFalse((f / "raw" / "followup-01.json").exists())
        self.assertFalse((f / "verdicts" / "apex-01__1.json").exists())
        self.assertFalse((f / "verdicts" / "_expected.json").exists())
        self.assertFalse((f / "validated.jsonl").exists())
        self.assertFalse((f / "findings.json").exists())
        self.assertTrue((f / "raw" / "rules.json").exists())      # rule checks run before planning


class VerdictTest(TmpCase):
    def make_run(self, findings, verdicts):
        run = self.tmp / "C" / "2026-01-01"
        (run / "findings").mkdir(parents=True)
        (run / "findings" / "validated.jsonl").write_text("\n".join(json.dumps(x) for x in findings) + "\n",
                                                          encoding="utf-8")
        for v in verdicts:
            wjson(run / "findings" / "verdicts" / (v["finding_id"].replace("#", "__") + ".json"), v)
        wjson(run / "shards" / "plan.json", {"areas": ["apex"], "assessedByArea": {"apex": 1}})
        return run

    def scored(self, run):
        proc = run_script("score.py", "--run", run)
        return {f["finding_id"]: f for f in json.loads((run / "findings" / "findings.json").read_text(encoding="utf-8"))["findings"]}, proc.stderr

    def test_verdict_for_other_content_is_ignored(self):
        old = finding("apex-01#1", title="Old finding that was refuted", quote="System.debug(oldThing);")
        new = finding("apex-01#1", title="Different new finding", quote="for (Account a : accounts) {")
        run = self.make_run([new], [{"finding_id": "apex-01#1", "verdict": "refuted", "reason": "no",
                                     "corrected_severity": "High", "fingerprint": content_fingerprint(old)}])
        out, err = self.scored(run)
        self.assertIn("apex-01#1", out)                                  # not silently dropped
        self.assertEqual(out["apex-01#1"]["verification"]["verdict"], "unverified")
        self.assertIn("ignoring", err)

    def test_matching_verdict_applies(self):
        f = finding("apex-01#1")
        run = self.make_run([f], [{"finding_id": "apex-01#1", "verdict": "refuted", "reason": "no",
                                   "corrected_severity": "High", "fingerprint": content_fingerprint(f)}])
        out, _ = self.scored(run)
        self.assertNotIn("apex-01#1", out)

    def test_downgrade_must_lower_severity(self):
        up = finding("apex-01#1")
        down = finding("apex-01#2", component="ApexClass:B")
        same = finding("apex-01#3", component="ApexClass:C")
        v = lambda f, sev: {"finding_id": f["finding_id"], "verdict": "downgraded", "corrected_severity": sev,
                            "reason": "r", "fingerprint": content_fingerprint(f)}
        conf = finding("apex-01#4", component="ApexClass:D")
        run = self.make_run([up, down, same, conf], [v(up, "Critical"), v(down, "Medium"), v(same, "High"),
                            {**v(conf, "Low"), "verdict": "confirmed"}])
        out, err = self.scored(run)
        self.assertEqual(out["apex-01#1"]["severity"], "High")
        self.assertEqual(out["apex-01#1"]["verification"]["verdict"], "confirmed")
        self.assertEqual(out["apex-01#2"]["severity"], "Medium")
        self.assertEqual(out["apex-01#2"]["original_severity"], "High")
        self.assertEqual(out["apex-01#3"]["severity"], "High")
        self.assertEqual(out["apex-01#4"]["severity"], "High")          # confirmed keeps the original
        self.assertIn("not lower", err)

    def test_verify_args_write_expected_and_skip_only_matching_verdicts(self):
        a, b = finding("apex-01#1"), finding("apex-01#2", component="ApexClass:B")
        run = self.make_run([a, b], [{"finding_id": "apex-01#1", "verdict": "confirmed", "reason": "r",
                                      "fingerprint": content_fingerprint(a)},
                                     {"finding_id": "apex-01#2", "verdict": "confirmed", "reason": "r",
                                      "fingerprint": "stale"}])
        args = json.loads(run_script("workflow_args.py", "verify", "--run", run).stdout)
        self.assertEqual([f["finding_id"] for f in args["findings"]], ["apex-01#2"])
        expected = json.loads((run / "findings" / "verdicts" / "_expected.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["apex-01#2"], content_fingerprint(b))

    def test_journal_requires_run_id(self):
        proc = run_script("workflow_args.py", "journal", ok=False)
        self.assertNotEqual(proc.returncode, 0)


class ExtractTest(TmpCase):
    def test_unknown_ids_rejected_and_fingerprint_attached(self):
        run = self.tmp / "C" / "2026-01-01"
        wjson(run / "shards" / "apex-01.json", {"shard_id": "apex-01"})
        wjson(run / "findings" / "verdicts" / "_expected.json", {"apex-01#1": "fp1"})
        review = lambda sid: {"shard_id": sid, "findings": [], "coverage": {}, "follow_up": []}
        journal = self.tmp / "journal.jsonl"
        journal.write_text("\n".join(json.dumps({"result": x}) for x in [
            review("apex-01"), review("apex-99"),
            {"finding_id": "apex-01#1", "verdict": "confirmed", "reason": "r"},
            {"finding_id": "apex-07#3", "verdict": "refuted", "reason": "r"}]) + "\n", encoding="utf-8")
        proc = run_script("extract_results.py", "--journal", journal, "--run", run, "--kind", "review")
        self.assertIn("apex-99", proc.stderr)
        self.assertEqual(sorted(p.name for p in (run / "findings" / "raw").glob("*.json")), ["apex-01.json"])
        proc = run_script("extract_results.py", "--journal", journal, "--run", run, "--kind", "verify")
        self.assertIn("apex-07#3", proc.stderr)
        names = sorted(p.name for p in (run / "findings" / "verdicts").glob("*.json"))
        self.assertEqual(names, ["_expected.json", "apex-01__1.json"])
        v = json.loads((run / "findings" / "verdicts" / "apex-01__1.json").read_text(encoding="utf-8"))
        self.assertEqual(v["fingerprint"], "fp1")


    def test_label_id_is_authoritative(self):
        run = self.tmp / "C" / "2026-01-01"
        for sid in ("apex-01", "apex-02"):
            wjson(run / "shards" / f"{sid}.json", {"shard_id": sid})
        review = lambda sid, n: {"shard_id": sid, "findings": [{"n": n}], "coverage": {}, "follow_up": []}
        journal = self.tmp / "journal.jsonl"
        journal.write_text("\n".join(json.dumps(x) for x in [
            {"type": "started", "key": "k1", "agentId": "a1", "label": "review:apex-01"},
            {"type": "started", "key": "k2", "agentId": "a2", "label": "review:apex-02"},
            {"type": "result", "key": "k1", "agentId": "a1", "result": review("apex-01", 1)},
            {"type": "result", "key": "k2", "agentId": "a2", "result": json.dumps(review("apex-01", 2))},  # wrong id
        ]) + "\n", encoding="utf-8")
        proc = run_script("extract_results.py", "--journal", journal, "--run", run, "--kind", "review")
        self.assertIn("using 'apex-02'", proc.stderr)
        raw = run / "findings" / "raw"
        self.assertEqual(json.loads((raw / "apex-01.json").read_text(encoding="utf-8"))["findings"], [{"n": 1}])
        two = json.loads((raw / "apex-02.json").read_text(encoding="utf-8"))
        self.assertEqual((two["shard_id"], two["findings"]), ("apex-02", [{"n": 2}]))


class DedupeTest(unittest.TestCase):
    def test_same_tool_evidence_different_components_not_merged(self):
        a = finding("apex-01#1", component="ApexClass:A", evidence_ids=["CA-1", "CA-2"])
        b = finding("apex-01#2", component="ApexClass:B", evidence_ids=["CA-1"])
        c = finding("apex-01#3", component="ApexClass:A", category="security", evidence_ids=["CA-1"])
        d = finding("apex-01#4", component="ApexClass:A", evidence_ids=["CA-2"], quote="other line here!!")
        d["evidence"][0]["line_start"] = 9
        kept = dedupe([a, b, c, d])
        self.assertEqual(sorted(f["finding_id"] for f in kept), ["apex-01#1", "apex-01#2", "apex-01#3"])


class DirtySetTest(TmpCase):
    def item(self, id_, sha="1", **kw):
        t, name = id_.split(":", 1)
        return {"id": id_, "type": t, "name": name, "object": kw.pop("object", ""), "path": kw.pop("path", f"x/{name}"),
                "sha": sha, **kw}

    def test_removed_reference_marks_old_callee(self):
        prev = [self.item("ApexClass:A", refs={"classes": ["B"], "objects": []}), self.item("ApexClass:B")]
        cur = [self.item("ApexClass:A", sha="2", refs={"classes": [], "objects": []}), self.item("ApexClass:B")]
        self.assertIn("ApexClass:B", plan_shards.changed_components(cur, prev))

    def test_duplicate_rule_object_from_name(self):
        rule = lambda sha: self.item("DuplicateRule:Account", sha=sha)
        users = [self.item("ApexTrigger:AccTrigger", object="Account"),
                 self.item("ApexClass:Svc", refs={"classes": [], "objects": ["Account"]}),
                 self.item("ApexClass:Other", refs={"classes": [], "objects": ["Contact"]})]
        dirty = plan_shards.changed_components([rule("2"), *users], [rule("1"), *users])
        self.assertTrue({"ApexTrigger:AccTrigger", "ApexClass:Svc"} <= dirty)
        self.assertNotIn("ApexClass:Other", dirty)
        self.assertEqual(plan_shards.object_of({"type": "SharingRules", "name": "Case", "object": ""}), "Case")

    def test_changed_subflow_marks_parent_flow(self):
        ns = "http://soap.sforce.com/2006/04/metadata"
        (self.tmp / "flows").mkdir()
        (self.tmp / "flows" / "Parent.flow-meta.xml").write_text(
            f'<?xml version="1.0"?><Flow xmlns="{ns}"><subflows><name>Call</name><flowName>Child</flowName>'
            f'</subflows></Flow>', encoding="utf-8")
        (self.tmp / "flows" / "Child.flow-meta.xml").write_text(f'<Flow xmlns="{ns}"/>', encoding="utf-8")
        mk = lambda name, sha: self.item(f"Flow:{name}", sha=sha, path=f"flows/{name}.flow-meta.xml")
        dirty = plan_shards.changed_components([mk("Parent", "1"), mk("Child", "2")],
                                               [mk("Parent", "1"), mk("Child", "1")], self.tmp, self.tmp)
        self.assertIn("Flow:Parent", dirty)


class QuoteValidationTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "run"
        (self.root / "metadata").mkdir(parents=True)
        (self.root / "metadata" / "A.cls").write_text(
            "public class A {\n    void run() {\n        x = 1;\n"
            "        List<Account> accts = [SELECT Id FROM Account WHERE Name = :n];\n"
            "        if (total &lt; limitValue &amp;&amp; isOpen) { update accts; }\n    }\n}\n", encoding="utf-8")
        (self.root / "metadata" / "B.cls").write_text("public class B {\n    Integer counterValue = 0;\n}\n", encoding="utf-8")
        (self.tmp / "run-other").mkdir()
        (self.tmp / "run-other" / "A.cls").write_text("x = 1;\nsecretTokenValue = 2;\n", encoding="utf-8")
        self.items = {"ApexClass:A": {"id": "ApexClass:A", "type": "ApexClass", "path": "metadata/A.cls"},
                      "ApexClass:B": {"id": "ApexClass:B", "type": "ApexClass", "path": "metadata/B.cls"}}
        self.shard = {"shard_id": "apex-01", "components": ["ApexClass:A"], "context_components": [],
                      "files": [{"id": "ApexClass:A", "path": "metadata/A.cls"}]}

    def reasons(self, quote, line=3, path="metadata/A.cls", shard="default"):
        f = {**BASE, "shard_id": "apex-01", "component": "ApexClass:A", "title": "t",
             "evidence": [{"path": path, "line_start": line, "line_end": line, "quote": quote}]}
        return vf.check_finding(f, self.items, set(), vf.SourceCache(self.root),
                                self.shard if shard == "default" else shard)

    def test_short_quote_rejected(self):
        self.assertTrue(any("too short" in r for r in self.reasons("x = 1;")))
        self.assertEqual(self.reasons("accts"), [], "a distinctive identifier is enough")

    def test_elision_accepted_in_order_only(self):
        self.assertEqual(self.reasons("List<Account> accts = ... WHERE Name = :n];", line=4), [])
        self.assertEqual(self.reasons("List<Account> accts … FROM Account", line=4), [])
        self.assertTrue(self.reasons("WHERE Name ... List<Account> accts", line=4))

    def test_entities_normalized(self):
        self.assertEqual(self.reasons("if (total < limitValue && isOpen)", line=5), [])

    def test_sibling_folder_and_foreign_paths_rejected(self):
        self.assertTrue(any("not found" in r for r in self.reasons("secretTokenValue = 2;", line=2,
                                                                   path="../run-other/A.cls")))
        # Only evidence from another component: rejected (nothing ties the finding to its own component).
        self.assertTrue(any("no evidence from" in r for r in self.reasons("Integer counterValue = 0;", line=2,
                                                                         path="metadata/B.cls")))
        # Run bookkeeping is never evidence.
        (self.root / "index").mkdir()
        (self.root / "index" / "inventory.json").write_text('[{"id": "ApexClass:A"}]\n', encoding="utf-8")
        self.assertTrue(any("not an org metadata file" in r for r in self.reasons(
            '[{"id": "ApexClass:A"}]', line=1, path="index/inventory.json")))

    def test_supporting_evidence_from_other_metadata_accepted(self):
        f = {**BASE, "shard_id": "apex-01", "component": "ApexClass:A", "title": "t", "evidence": [
            {"path": "metadata/A.cls", "line_start": 4, "line_end": 4, "quote": "List<Account> accts"},
            {"path": "metadata/B.cls", "line_start": 2, "line_end": 2, "quote": "Integer counterValue = 0;"}]}
        self.assertEqual(vf.check_finding(f, self.items, set(), vf.SourceCache(self.root), self.shard), [])

    def test_carried_finding_skips_scope_check(self):
        f = {**BASE, "shard_id": "carried", "component": "ApexClass:A", "title": "t", "evidence": [
            {"path": "metadata/B.cls", "line_start": 2, "line_end": 2, "quote": "Integer counterValue = 0;"}]}
        self.assertEqual(vf.check_finding(f, self.items, set(), vf.SourceCache(self.root), None), [])

    def test_unknown_shard_rejected(self):
        self.assertTrue(any("not planned" in r for r in self.reasons("accts", shard=None)))


class DiffBaselineTest(unittest.TestCase):
    def test_reworded_title_matches_and_duplicates_counted(self):
        old = [finding("a#1", title="SOQL query inside for loop in InvoiceService"),
               finding("a#2", title="SOQL query inside for loop in InvoiceService", quote="while (it.hasNext()) {"),
               finding("a#3", component="ApexClass:B", title="Hard-coded record id in batch class")]
        cur = [finding("b#1", title="Query executed per iteration of the accounts loop"),           # same quote
               finding("b#2", component="ApexClass:B", title="Hard-coded record ids in batch class",
                       quote="different evidence entirely")]                                       # similar title
        pairs = diff_baseline.match_findings(old, cur)
        self.assertEqual(pairs, {0: 0, 1: 2})
        self.assertEqual(len(old) - len(set(pairs.values())), 1)        # a#2 is fixed, not collapsed into a#1


if __name__ == "__main__":
    unittest.main()
