"""Redaction of personal data before AI stages (scripts/privacy.py)."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import privacy  # noqa: E402
from common import Run, load_config  # noqa: E402

CLS = Path("metadata/force-app/main/default/classes")


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run = Run(self.tmp / "run")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = self.run.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
        return p

    def cfg(self, **privacy_cfg):
        cfg = load_config()
        cfg["privacy"] = {**cfg["privacy"], **privacy_cfg}
        return cfg

    def test_emails_phones_ssn_redacted_everywhere(self):
        flow = self.write("metadata/force-app/main/default/flows/F.flow-meta.xml",
                          "<recipient>jane.doe@clinic.org</recipient>\r\n<value>(555) 123-4567</value>\r\n")
        tool = self.write("tools/hardis-unusedusers.json", '{"u": "a.b@x.co.uk", "s": "123-45-6789"}')
        privacy.redact_run(self.run, self.cfg())
        self.assertEqual(flow.read_bytes(), b"<recipient>[redacted-email]</recipient>\r\n<value>[redacted-phone]</value>\r\n")
        self.assertEqual(json.loads(tool.read_text()), {"u": "[redacted-email]", "s": "[redacted-ssn]"})
        report = json.loads((self.run.root / "privacy.json").read_text())
        self.assertEqual(report["redactedTotal"], {"email": 2, "phone": 1, "ssn": 1})
        self.assertNotIn("jane", json.dumps(report))

    def test_redacted_xml_html_and_apex_stay_valid(self):
        import xml.etree.ElementTree as ET
        xml = self.write("metadata/force-app/main/default/workflows/W.workflow-meta.xml",
                         '<?xml version="1.0"?><Workflow><alerts><ccEmails>a@b.com</ccEmails>'
                         '<description>call 555-123-4567</description></alerts></Workflow>')
        apex = self.write(CLS / "T.cls", "@isTest class T { String e = 'a@b.com'; String n = 'Jane'; }")
        privacy.redact_run(self.run, self.cfg(redactTestData=True))
        ET.parse(xml)                                   # still well-formed
        self.assertEqual(apex.read_text(),
                         "@isTest class T { String e = '[redacted-email]'; String n = '[redacted-text]'; }")

    def test_idempotent_and_counts_accumulate(self):
        self.write("metadata/force-app/main/default/workflows/W.workflow-meta.xml", "x@y.com")
        privacy.redact_run(self.run, self.cfg())
        privacy.redact_run(self.run, self.cfg())
        report = json.loads((self.run.root / "privacy.json").read_text())
        self.assertEqual(report["redactedTotal"], {"email": 1})
        self.assertEqual(report["lastPass"]["filesChanged"], 0)

    def test_code_constructs_not_mistaken_for_personal_data(self):
        src = ("import x from '@salesforce/apex/Ctl.run';\n"
               "String v = 'lodash@4.17.21'; Decimal d = 2024.10;\n"
               "@IsTest static void t() {}\nString ip = '10.123.456.7890';\n")
        p = self.write("metadata/force-app/main/default/lwc/c/c.js", src)
        privacy.redact_run(self.run, self.cfg())
        self.assertEqual(p.read_text(), src)

    def test_test_class_literals_only_when_enabled(self):
        src = ("@isTest\nprivate class PTest {\n"
               "  static void t() { Contact c = new Contact(LastName='Smith', Birthdate__c='1980-02-03', "
               "Type__c='Patient__c', X=''); }\n}\n")
        p = self.write(CLS / "PTest.cls", src)
        prod = self.write(CLS / "P.cls", "public class P { String s = 'Smith'; }\n")
        privacy.redact_run(self.run, self.cfg())
        self.assertIn("'Smith'", p.read_text())
        privacy.redact_run(self.run, self.cfg(redactTestData=True))
        text = p.read_text()
        self.assertNotIn("Smith", text)
        self.assertNotIn("1980", text)
        self.assertIn("'Patient__c'", text)        # API names kept
        self.assertIn("X=''", text)                # empty literal kept
        self.assertIn("'Smith'", prod.read_text())  # only test classes

    def test_redaction_off_is_recorded(self):
        f = self.write("metadata/a.xml", "x@y.com")
        privacy.redact_run(self.run, self.cfg(redact=False))
        self.assertEqual(f.read_text(), "x@y.com")
        self.assertFalse(json.loads((self.run.root / "privacy.json").read_text())["redact"])

    def test_hipaa_preset(self):
        cfg = load_config(str(Path(__file__).resolve().parent.parent / "config" / "hipaa.yml"))
        self.assertEqual(cfg["dataPolicy"], "metadata-only")
        self.assertTrue(cfg["privacy"]["redact"] and cfg["privacy"]["redactTestData"])
        self.assertFalse(cfg["library"]["enabled"])


if __name__ == "__main__":
    unittest.main()
