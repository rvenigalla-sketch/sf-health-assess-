"""Deterministic pipeline tests on the synthetic seeded org (no Salesforce org, no AI).

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

import plan_shards  # noqa: E402
from common import load_config  # noqa: E402
from score import score_areas  # noqa: E402


def run_script(name, *args):
    proc = subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)], cwd=SCRIPTS,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError(f"{name} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")
    return proc


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            run_script("../tests/make_fixture.py")
        cls.tmp = Path(tempfile.mkdtemp(prefix="sfhealth-"))
        cls.run_dir = cls.tmp / "Fixture" / "2026-01-01"
        shutil.copytree(FIXTURE, cls.run_dir)
        run_script("build_index.py", "--run", cls.run_dir)
        run_script("plan_shards.py", "--run", cls.run_dir)
        cls.inv = {i["id"]: i for i in json.loads((cls.run_dir / "index" / "inventory.json").read_text(encoding="utf-8"))}
        cls.auto = json.loads((cls.run_dir / "index" / "automation_map.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------- index
    def test_inventory_types(self):
        self.assertTrue(self.inv["ApexClass:InvoiceServiceTest"]["isTest"])
        self.assertFalse(self.inv["ApexClass:InvoiceService"]["isTest"])
        self.assertEqual(self.inv["ApexTrigger:InvoiceTrigger"]["object"], "Invoice__c")
        self.assertIn("InvoiceService", self.inv["ApexTrigger:InvoiceTrigger"]["refs"]["classes"])

    def test_automation_map_and_overlaps(self):
        a = self.auto["Invoice__c"]
        self.assertEqual(len(a["triggers"]), 1)
        self.assertEqual(len(a["flowsAfterSave"]), 2)
        self.assertEqual(len(a["processBuilders"]), 1)
        self.assertEqual(len(a["workflowRules"]), 1)
        writers = a["fieldOverlaps"]
        self.assertIn("Amount_Total__c", writers)
        self.assertTrue(any(w.startswith("WorkflowRule:") for w in writers["Amount_Total__c"]))
        self.assertIn("Status__c", writers)
        self.assertTrue(any(w.startswith("ProcessBuilder:") for w in writers["Status__c"]))

    def test_flow_digest_flags(self):
        digest = (self.run_dir / self.inv["Flow:Invoice_Line_Sync"]["digestPath"]).read_text(encoding="utf-8")
        self.assertIn("IN-LOOP:Each_Sibling", digest)
        self.assertIn("NO-FAULT-PATH", digest)
        self.assertIn("possible hard-coded record IDs", digest)

    def test_field_usage(self):
        table = (self.run_dir / "index" / "datamodel" / "Invoice__c.txt").read_text(encoding="utf-8")
        rows = {l.split(" | ")[0]: l.split(" | ") for l in table.splitlines() if " | " in l}
        self.assertEqual(rows["Legacy_Code__c"][-1], "0")
        self.assertNotEqual(rows["AmountTotal__c"][-1], "0")
        self.assertNotEqual(rows["Amount_Total__c"][-1], "0")

    def test_automation_summary_order(self):
        text = (self.run_dir / "index" / "automation" / "Invoice__c.txt").read_text(encoding="utf-8")
        self.assertLess(text.index("1. Before-save"), text.index("5. Apex AFTER triggers"))
        self.assertLess(text.index("7. Process Builders"), text.index("8. After-save"))
        self.assertIn("Amount_Total__c", text.split("FIELDS WRITTEN BY MORE THAN ONE")[1])

    # ---------------- shards
    def test_shards_cover_everything(self):
        shards = [json.loads(p.read_text(encoding="utf-8")) for p in (self.run_dir / "shards").glob("*.json")
                  if p.name != "plan.json"]
        covered = {c for s in shards for c in s["components"]}
        for cid in ("ApexClass:InvoiceService", "ApexClass:InvoiceSearchController", "ApexClass:InvoiceServiceTest",
                    "Flow:Invoice_Line_Sync", "FieldUsage:Invoice__c", "AutomationSummary:Invoice__c"):
            self.assertIn(cid, covered)
        for s in shards:
            self.assertLessEqual(s["sourceTokens"], 45000)

    def test_pack_limits_and_balance(self):
        items = [{"tokens": t} for t in (30, 30, 30, 30, 5, 5, 5)]
        bins = plan_shards.pack([[i] for i in items], budget=70, cost=lambda i: i["tokens"], max_items=3)
        for b in bins:
            self.assertLessEqual(sum(i["tokens"] for i in b), 70)
            self.assertLessEqual(len(b), 3)
        self.assertEqual(sum(len(b) for b in bins), len(items))
        self.assertEqual(len(bins), 3)

    # ---------------- validator
    def test_validator_rejects_fabrication(self):
        raw = self.run_dir / "findings" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        path = self.inv["ApexClass:InvoiceService"]["path"]
        base = {"area": "apex", "category": "bulkification", "severity": "High", "component": "ApexClass:InvoiceService",
                "evidence_ids": [], "basis": "llm-observation", "confidence": "high", "impact": "x",
                "recommendation": "y", "effort": "S", "related_components": []}
        good = {**base, "title": "real", "evidence": [{"path": path, "line_start": 4, "line_end": 4,
                "quote": "List<Invoice__c> siblings = [SELECT Id, Amount__c FROM Invoice__c"}]}
        bad_quote = {**base, "title": "bad quote", "evidence": [{"path": path, "line_start": 4, "line_end": 4,
                     "quote": "Database.delete(everything);"}]}
        bad_file = {**base, "title": "bad file", "evidence": [{"path": "metadata/nope.cls", "line_start": 1,
                    "line_end": 1, "quote": "public class"}]}
        bad_id = {**base, "title": "bad id", "basis": "tool-confirmed", "evidence_ids": ["CA-99999"],
                  "evidence": good["evidence"]}
        escape = {**base, "title": "escape", "evidence": [{"path": "../../../../Windows/win.ini", "line_start": 1,
                  "line_end": 1, "quote": "fonts"}]}
        shard_id = next(p.stem for p in (self.run_dir / "shards").glob("apex-0*.json"))
        (raw / f"{shard_id}.json").write_text(json.dumps({"shard_id": shard_id,
            "findings": [good, bad_quote, bad_file, bad_id, escape],
            "coverage": {"components_reviewed": ["ApexClass:InvoiceService"], "skipped": []}, "follow_up": []}),
            encoding="utf-8")
        run_script("validate_findings.py", "--run", self.run_dir)
        ok = [json.loads(l) for l in (self.run_dir / "findings" / "validated.jsonl").read_text(encoding="utf-8").splitlines()]
        bad = [json.loads(l) for l in (self.run_dir / "findings" / "rejected.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([f["title"] for f in ok], ["real"])
        self.assertEqual(sorted(f["title"] for f in bad), ["bad file", "bad id", "bad quote", "escape"])
        cov = json.loads((self.run_dir / "findings" / "coverage.json").read_text(encoding="utf-8"))
        self.assertTrue(cov["shards"][shard_id]["missing"], "unreviewed components must be reported")

        # Score, review gate (with real decisions) and render on top of the validated finding.
        run_script("score.py", "--run", self.run_dir)
        run_script("review_gate.py", "export", "--run", self.run_dir)
        from openpyxl import load_workbook
        xlsx = self.run_dir / "review" / "findings_review.xlsx"
        wb = load_workbook(xlsx)
        ws = wb.active
        ws.cell(2, 12).value = "Edit"
        ws.cell(2, 13).value = "Medium"
        wb.save(xlsx)
        run_script("review_gate.py", "import", "--run", self.run_dir)
        accepted = json.loads((self.run_dir / "findings" / "accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(accepted["findings"][0]["severity"], "Medium")
        self.assertEqual(accepted["findings"][0]["original_severity"], "High")

        accepted["findings"][0]["title"] = "</script><script>window.pwned=1</script>"
        (self.run_dir / "findings" / "accepted.json").write_text(json.dumps(accepted), encoding="utf-8")
        run_script("render.py", "--run", self.run_dir, "--client", "Fixture Co")
        html = (self.run_dir / "report" / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("</script><script>window.pwned", html)
        for name in ("report.docx", "baseline.xlsx", "report.artifact.html"):
            self.assertTrue((self.run_dir / "report" / name).exists())

    # ---------------- scoring
    def test_scoring_formula_and_critical_cap(self):
        cfg = load_config()
        f = lambda sev, area="apex": {"severity": sev, "area": area}
        s = score_areas([f("Low")], cfg, {"apex"})
        self.assertEqual(s["areas"]["apex"]["score"], 98)
        self.assertEqual(s["areas"]["flow"]["rating"], "Not assessed")
        s = score_areas([f("Critical")], cfg, {"apex", "flow", "security", "automation"})
        self.assertNotEqual(s["overallRating"], "Good")


if __name__ == "__main__":
    unittest.main()
