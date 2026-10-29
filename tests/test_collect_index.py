"""Unit tests for collection and indexing helpers (no Salesforce org needed).

Run: python -m unittest discover -s tests -v      (from the repo root)
"""

import json
import shutil
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_index  # noqa: E402
import collect  # noqa: E402
from common import run_cmd  # noqa: E402
from digests import field_usage  # noqa: E402
from flows import parse_flow  # noqa: E402
from org_facts import build_org_facts  # noqa: E402

MD = 'xmlns="http://soap.sforce.com/2006/04/metadata"'


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sfhealth-unit-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str = "x") -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class ManifestTest(TempDirCase):
    def test_write_package_escapes_member_names(self):
        path = self.tmp / "p.xml"
        types = {"Layout": ["Case-Sales & Service Layout", "Account-<Odd> \"Layout\""], "ApexClass": ["A"]}
        collect.write_package(path, types, "62.0")
        parsed = collect.parse_package(path)          # would raise ParseError if unescaped
        self.assertEqual(sorted(parsed["Layout"]), sorted(types["Layout"]))
        self.assertEqual(parsed["ApexClass"], ["A"])

    def test_chunking_puts_profiles_in_access_pass_with_related_types(self):
        types = {
            "ApexClass": [f"C{i}" for i in range(5)], "CustomObject": ["Account", "Invoice__c"],
            "Layout": ["Account-Account Layout"], "Report": ["r1", "r2", "r3"],
            "Profile": [f"P{i}" for i in range(3)], "PermissionSet": ["PS1", "PS2"],
            "MutingPermissionSet": ["M1"], "PermissionSetGroup": ["G1"],
        }
        main, access = collect.plan_chunks(types, size=4, access_size=2, related_size=100)
        main_types = {t for c in main for t in c}
        self.assertFalse(main_types & set(collect.ACCESS_TYPES))
        self.assertIn("PermissionSetGroup", main_types)
        self.assertTrue(all(sum(map(len, c.values())) <= 4 for c in main))
        # every non-access member retrieved exactly once in the main pass
        main_members = sorted((t, m) for c in main for t, ms in c.items() for m in ms)
        expected = sorted((t, m) for t, ms in types.items() if t not in collect.ACCESS_TYPES for m in ms)
        self.assertEqual(main_members, expected)
        # 6 access members, 2 per batch -> 3 access chunks, each with ALL related types
        self.assertEqual(len(access), 3)
        for chunk in access:
            self.assertEqual(sorted(chunk["ApexClass"]), types["ApexClass"])
            self.assertEqual(chunk["CustomObject"], types["CustomObject"])
            self.assertEqual(chunk["Layout"], types["Layout"])
            self.assertNotIn("Report", chunk)
            self.assertEqual(sum(len(chunk.get(t, [])) for t in collect.ACCESS_TYPES), 2)
        got = sorted(m for c in access for t in collect.ACCESS_TYPES for m in c.get(t, []))
        self.assertEqual(got, sorted(["P0", "P1", "P2", "PS1", "PS2", "M1"]))

    def test_access_related_types_split_into_groups_when_large(self):
        types = {"ApexClass": [f"C{i}" for i in range(5)], "Profile": ["Admin"]}
        _, access = collect.plan_chunks(types, size=100, access_size=25, related_size=2)
        self.assertEqual(len(access), 3)                   # 5 classes in groups of 2
        self.assertTrue(all(c["Profile"] == ["Admin"] for c in access))
        self.assertEqual(sorted(m for c in access for m in c["ApexClass"]), types["ApexClass"])

    def test_no_profiles_means_no_access_chunks(self):
        main, access = collect.plan_chunks({"ApexClass": ["A"]}, size=10)
        self.assertEqual(access, [])
        self.assertEqual(main, [{"ApexClass": ["A"]}])

    def test_access_merge_keeps_main_objects_and_unions_profiles(self):
        dest = self.tmp / "force-app"
        self.write("force-app/main/default/objects/Account/fields/Full__c.field-meta.xml", "MAIN")
        prof = '<?xml version="1.0" encoding="UTF-8"?>\n<Profile {md}>\n{body}\n    <custom>false</custom>\n</Profile>\n'
        s1 = self.tmp / "s1" / "force-app"
        self.write("s1/force-app/main/default/objects/Account/fields/Full__c.field-meta.xml", "PARTIAL")
        self.write("s1/force-app/main/default/profiles/Admin.profile-meta.xml", prof.format(
            md=MD, body="    <classAccesses><apexClass>A</apexClass><enabled>true</enabled></classAccesses>"))
        s2 = self.tmp / "s2" / "force-app"
        self.write("s2/force-app/main/default/profiles/Admin.profile-meta.xml", prof.format(
            md=MD, body="    <classAccesses><apexClass>B</apexClass><enabled>true</enabled></classAccesses>"))
        collect.merge_staged(s1, dest, access=True)
        collect.merge_staged(s2, dest, access=True)
        self.assertEqual((dest / "main/default/objects/Account/fields/Full__c.field-meta.xml").read_text(), "MAIN")
        root = ET.parse(dest / "main/default/profiles/Admin.profile-meta.xml").getroot()
        ns = "{http://soap.sforce.com/2006/04/metadata}"
        self.assertEqual(sorted(e.findtext(ns + "apexClass") for e in root.findall(ns + "classAccesses")), ["A", "B"])
        self.assertEqual(len(root.findall(ns + "custom")), 1)

    def test_main_merge_skips_uncopyable_file_and_continues(self):
        # Stand-in for a path over the Windows limit: the destination folder is a file, so the copy fails.
        dest = self.tmp / "force-app"
        self.write("s/force-app/main/default/layouts/Bad.layout-meta.xml", "X")
        self.write("s/force-app/main/default/classes/A.cls", "class A {}")
        self.write("force-app/main/default/layouts", "not a folder")
        n, skipped = collect.merge_staged(self.tmp / "s" / "force-app", dest, access=False)
        self.assertEqual((n, skipped), (1, ["main/default/layouts/Bad.layout-meta.xml"]))
        self.assertEqual((dest / "main/default/classes/A.cls").read_text(), "class A {}")


class ManagedFilterTest(unittest.TestCase):
    NS = {"ns", "acme"}

    def check(self, member, mtype, expected):
        self.assertEqual(collect.is_managed(member, mtype, self.NS), expected, f"{mtype} {member}")

    def test_cases(self):
        self.check("ns__Obj__c", "CustomObject", True)
        self.check("Account.ns__Field__c", "CustomField", True)
        self.check("ns__Obj__c.My_Field__c", "CustomField", False)
        self.check("ns__Class", "ApexClass", True)
        self.check("MyClass", "ApexClass", False)
        self.check("Account-ns__X Layout", "Layout", True)
        self.check("ns__Obj__c-My Layout", "Layout", False)
        self.check("ns__Obj__c-ns__Obj Layout", "Layout", True)
        self.check("Account-Acct. Layout v2", "Layout", False)
        self.check("ns__Type.Rec", "CustomMetadata", False)       # subscriber record of a managed type
        self.check("ns__Type.ns__Rec", "CustomMetadata", True)
        self.check("My_Type.acme__Rec", "CustomMetadata", True)
        self.check("folder/ns__Tpl", "EmailTemplate", True)
        self.check("nsx__Class", "ApexClass", False)
        self.assertFalse(collect.is_managed("ns__Class", "ApexClass", set()))


class RunCmdTest(unittest.TestCase):
    def test_timeout_returns_instead_of_raising(self):
        start = time.monotonic()
        code, out, err = run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertEqual(code, 124)
        self.assertIn("TIMEOUT", err)
        self.assertLess(time.monotonic() - start, 25)

    def test_timeout_kills_grandchild_holding_pipes(self):
        # Like sf.cmd -> node: the child starts a grandchild that inherits stdout and keeps it open.
        script = ("import subprocess, sys, time; "
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)")
        start = time.monotonic()
        code, _, _ = run_cmd([sys.executable, "-c", script], timeout=1)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - start, 25)

    def test_missing_executable_returns_code(self):
        code, _, err = run_cmd(["definitely-not-a-real-command-xyz"])
        self.assertEqual(code, 127)
        self.assertTrue(err)

    def test_normal_run(self):
        code, out, _ = run_cmd([sys.executable, "-c", "print('hi')"])
        self.assertEqual((code, out.strip()), (0, "hi"))


class ProgressTest(unittest.TestCase):
    def test_tool_count_matches_data_policy(self):
        from common import load_config
        cfg = load_config(None)
        t = cfg["tools"]
        full = 1 + 2 + len(t["orgCheck"]["views"]) + len(t["hardis"]["commands"])
        self.assertEqual(collect.tool_count(cfg), full)
        cfg["dataPolicy"] = "metadata-only"
        self.assertEqual(collect.tool_count(cfg), 1)

    def test_progress_lines_never_pass_100_percent(self):
        import contextlib
        import io
        from common import Progress
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p = Progress("collect", 2)
            for d in ("a", "b", "c"):
                p.tick(d)
        lines = err.getvalue().splitlines()
        self.assertEqual(lines[0], "PROGRESS collect 1/2 50% [##########----------] a")
        self.assertEqual(lines[2], "PROGRESS collect 3/3 100% [####################] c")


class InventoryNamingTest(TempDirCase):
    def test_catch_all_names_are_unique_and_skip_companions(self):
        src = self.tmp / "metadata" / "force-app" / "main" / "default"
        self.write("metadata/force-app/main/default/customMetadata/Config.Rec_A.md-meta.xml", "<a/>")
        self.write("metadata/force-app/main/default/customMetadata/Config.Rec_B.md-meta.xml", "<b/>")
        self.write("metadata/force-app/main/default/pages/MyPage.page", "<apex:page/>")
        self.write("metadata/force-app/main/default/pages/MyPage.page-meta.xml", "<ApexPage/>")
        self.write("metadata/force-app/main/default/quickActions/Account.Log_Call.quickAction-meta.xml", "<q/>")
        self.write("metadata/force-app/main/default/quickActions/Case.Log_Call.quickAction-meta.xml", "<q/>")
        self.write("metadata/force-app/main/default/layouts/Case-Sales & Service Layout.layout-meta.xml", "<l/>")
        self.write("metadata/force-app/main/default/reports/Sales.reportFolder-meta.xml", "<f/>")
        self.write("metadata/force-app/main/default/reports/Sales/Pipeline.report-meta.xml", "<r/>")
        self.write("metadata/force-app/main/default/staticresources/Lib.resource-meta.xml", "<s/>")
        self.write("metadata/force-app/main/default/staticresources/Lib/js/a.js", "var a;")
        self.write("metadata/force-app/main/default/staticresources/Logo.resource", "PNG")
        self.write("metadata/force-app/main/default/staticresources/Logo.resource-meta.xml", "<s/>")
        items, _ = build_index.build_inventory(src, self.tmp, self.tmp / "index")
        ids = [i["id"] for i in items]
        self.assertEqual(len(ids), len(set(ids)), ids)
        for expected in ("CustomMetadata:Config.Rec_A", "CustomMetadata:Config.Rec_B", "ApexPage:MyPage",
                         "quickActions:Account.Log_Call", "quickActions:Case.Log_Call",
                         "Layout:Case-Sales & Service Layout", "reports:Sales", "reports:Sales/Pipeline",
                         "staticresources:Lib", "staticresources:Logo"):
            self.assertIn(expected, ids)
        self.assertEqual(next(i for i in items if i["id"] == "ApexPage:MyPage")["path"].split("/")[-1], "MyPage.page")
        self.assertEqual(len([i for i in ids if i.startswith("staticresources:")]), 2)


class FieldUsageTest(TempDirCase):
    def test_case_insensitive_relationship_and_object_attribution(self):
        self.write("cls/A.cls", "Case c; c.status__C = 'x'; acc.Region__R.Name; "
                                "SELECT Id FROM Account WHERE Account.Tier__c = 1")
        self.write("trig/T.trigger", "trigger T on Case (before insert) { x.Mixed__c = 1; }")
        items = [
            {"type": "CustomField", "name": "Case.Status__c", "object": "Case", "path": "x"},
            {"type": "CustomField", "name": "Account.Status__c", "object": "Account", "path": "x"},
            {"type": "CustomField", "name": "Account.Region__c", "object": "Account", "path": "x",
             "fieldType": "Lookup", "referenceTo": "Region__c"},
            {"type": "CustomField", "name": "Account.Tier__c", "object": "Account", "path": "x"},
            {"type": "CustomField", "name": "Case.Tier__c", "object": "Case", "path": "x"},
            {"type": "CustomField", "name": "Account.Mixed__c", "object": "Account", "path": "x"},
            {"type": "CustomField", "name": "Case.Mixed__c", "object": "Case", "path": "x"},
            {"type": "ApexClass", "name": "A", "object": "", "path": "cls/A.cls"},
            {"type": "ApexTrigger", "name": "T", "object": "Case", "path": "trig/T.trigger"},
        ]
        field_usage(items, self.tmp, self.tmp / "dm")

        def total(obj, field):
            rows = (self.tmp / "dm" / f"{obj}.txt").read_text(encoding="utf-8").splitlines()
            return int(next(r for r in rows if r.startswith(field + " | ")).split(" | ")[-1])

        self.assertGreater(total("Account", "Region__c"), 0)     # Region__R.Name uses the lookup
        self.assertEqual(total("Account", "Tier__c"), 1)         # Account.Tier__c is qualified
        self.assertEqual(total("Case", "Tier__c"), 0)
        self.assertEqual(total("Case", "Mixed__c"), 1)           # trigger on Case
        self.assertEqual(total("Account", "Mixed__c"), 0)
        # unqualified status__C (any case) can't be attributed: counts for both, never as unused
        self.assertEqual(total("Case", "Status__c"), 1)
        self.assertEqual(total("Account", "Status__c"), 1)

    def test_custom_ref_case_insensitive(self):
        self.write("classes/A.cls", "List<invoice__C> x; account a;")
        items = [{"type": "ApexClass", "name": "A", "path": "classes/A.cls", "object": ""},
                 {"type": "CustomObject", "name": "Invoice__c", "path": "x", "object": "Invoice__c"},
                 {"type": "CustomObject", "name": "Account", "path": "x", "object": "Account"}]
        build_index.add_references(items, self.tmp)
        self.assertEqual(items[0]["refs"]["objects"], ["Account", "Invoice__c"])


class FlowFactsTest(TempDirCase):
    def flow(self, body):
        return parse_flow(self.write("F.flow-meta.xml",
                                     f'<?xml version="1.0" encoding="UTF-8"?>\n<Flow {MD}>\n{body}\n</Flow>\n'))

    def test_before_save_record_update_is_not_missing_fault_path(self):
        facts = self.flow("""
<processType>AutoLaunchedFlow</processType><status>Active</status>
<start><object>Case</object><triggerType>RecordBeforeSave</triggerType><recordTriggerType>Update</recordTriggerType></start>
<recordUpdates><name>Upd</name><inputReference>$Record</inputReference></recordUpdates>
<recordLookups><name>Get</name><object>Account</object></recordLookups>
<waits><name>Pause</name></waits>""")
        self.assertEqual(sorted(facts["noFaultPath"]), ["Get", "Pause"])
        self.assertNotIn("Upd:", " ".join(l for l in facts["digest"].splitlines() if "NO-FAULT-PATH" in l))

    def test_assignment_then_update_by_reference_records_fields_written(self):
        facts = self.flow("""
<processType>AutoLaunchedFlow</processType><status>Active</status>
<start><object>Case</object><triggerType>RecordAfterSave</triggerType><recordTriggerType>Update</recordTriggerType></start>
<variables><name>acc</name><dataType>SObject</dataType><objectType>Account</objectType></variables>
<assignments><name>Set</name><assignmentItems><assignToReference>acc.Rating__c</assignToReference><operator>Assign</operator></assignmentItems>
<assignmentItems><assignToReference>$Record.Priority__c</assignToReference><operator>Assign</operator></assignmentItems></assignments>
<recordUpdates><name>SaveAcc</name><inputReference>acc</inputReference></recordUpdates>
<recordUpdates><name>SaveRec</name><inputReference>$Record</inputReference></recordUpdates>""")
        self.assertIn("Account.Rating__c", facts["fieldsWritten"])
        self.assertIn("$Record.Priority__c", facts["fieldsWritten"])


class OrgFactsTest(TempDirCase):
    def test_hardis_error_is_not_assessed_not_zero(self):
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "hardis-underusedpermsets.json").write_text(json.dumps(
            {"status": 1, "name": "Error", "message": "INVALID_SESSION_ID"}), encoding="utf-8")
        (tools / "hardis-releaseupdates.json").write_text(json.dumps({"error": "timed out"}), encoding="utf-8")
        # hardis status 1 WITH a result = it ran and found issues
        (tools / "hardis-limits.json").write_text(json.dumps(
            {"status": 1, "result": {"limitEntries": [{"name": "X", "used": 9, "max": 10, "percentUsed": 90}]}}),
            encoding="utf-8")
        summary = build_org_facts(tools, self.tmp / "orgfacts")
        self.assertNotIn("underusedPermissionSets", summary)
        self.assertNotIn("pendingReleaseUpdates", summary)
        self.assertEqual(set(summary["notAssessed"]), {"hardis-underusedpermsets.json", "hardis-releaseupdates.json"})
        self.assertIn("INVALID_SESSION_ID", summary["notAssessed"]["hardis-underusedpermsets.json"])
        self.assertEqual(summary["limitsAbove50Pct"], 1)


if __name__ == "__main__":
    unittest.main()
