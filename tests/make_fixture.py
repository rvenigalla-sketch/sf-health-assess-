"""Build tests/fixtures/seeded-org: a tiny synthetic Salesforce org with planted problems.

No client data. tests/fixtures/expected_findings.json lists every planted problem; the unit tests
check that scripts catch the deterministic ones, and tests/eval.py scores AI reviewers against it.

Usage: python tests/make_fixture.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent / "fixtures" / "seeded-org"
B = ROOT / "metadata" / "force-app" / "main" / "default"
NS = 'xmlns="http://soap.sforce.com/2006/04/metadata"'


def w(rel: str, text: str) -> None:
    p = B / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.lstrip("\n"), encoding="utf-8", newline="\n")


def apex_meta(kind: str, version: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<{kind} {NS}>\n    <apiVersion>{version}</apiVersion>\n'
            f'    <status>Active</status>\n</{kind}>\n')


def field(name: str, ftype: str, desc: str = "") -> None:
    d = f"\n    <description>{desc}</description>" if desc else ""
    w(f"objects/Invoice__c/fields/{name}.field-meta.xml",
      f'<?xml version="1.0" encoding="UTF-8"?>\n<CustomField {NS}>\n    <fullName>{name}</fullName>\n'
      f'    <label>{name}</label>\n    <type>{ftype}</type>{d}\n</CustomField>\n')


def flow(name: str, body: str) -> None:
    w(f"flows/{name}.flow-meta.xml", f'<?xml version="1.0" encoding="UTF-8"?>\n<Flow {NS}>\n{body.strip(chr(10))}\n</Flow>\n')


def build() -> None:
    # ---------------- Apex
    w("triggers/InvoiceTrigger.trigger", """
trigger InvoiceTrigger on Invoice__c (after insert, after update) {
    InvoiceService.recalculate(Trigger.new);
}
""")
    w("triggers/InvoiceTrigger.trigger-meta.xml", apex_meta("ApexTrigger", "45.0"))
    w("classes/InvoiceService.cls", """
public class InvoiceService {
    public static void recalculate(List<Invoice__c> invoices) {
        for (Invoice__c inv : invoices) {
            List<Invoice__c> siblings = [SELECT Id, Amount__c FROM Invoice__c WHERE Status__c = :inv.Status__c];
            inv.Amount_Total__c = 0;
            for (Invoice__c s : siblings) {
                inv.Amount_Total__c += s.Amount__c;
            }
            try {
                update new Invoice__c(Id = inv.Id, Owner_Queue__c = '00G5e000003XyZaEAK');
            } catch (Exception e) {
            }
        }
    }
}
""")
    w("classes/InvoiceService.cls-meta.xml", apex_meta("ApexClass", "45.0"))
    w("classes/InvoiceServiceTest.cls", """
@IsTest(SeeAllData=true)
private class InvoiceServiceTest {
    @IsTest
    static void runsWithoutAsserting() {
        Invoice__c inv = new Invoice__c(Amount__c = 10, Status__c = 'Open');
        insert inv;
        InvoiceService.recalculate(new List<Invoice__c>{ inv });
    }
}
""")
    w("classes/InvoiceServiceTest.cls-meta.xml", apex_meta("ApexClass", "62.0"))
    w("classes/InvoiceSearchController.cls", """
public without sharing class InvoiceSearchController {
    @AuraEnabled
    public static List<Invoice__c> search(String term) {
        String q = 'SELECT Id, Amount__c FROM Invoice__c WHERE Name LIKE \\'%' + term + '%\\'';
        return Database.query(q);
    }
}
""")
    w("classes/InvoiceSearchController.cls-meta.xml", apex_meta("ApexClass", "62.0"))
    w("classes/CleanFormatter.cls", """
public with sharing class CleanFormatter {
    public static String money(Decimal amount) {
        return amount == null ? '' : '$' + amount.setScale(2).format();
    }
}
""")
    w("classes/CleanFormatter.cls-meta.xml", apex_meta("ApexClass", "62.0"))

    # ---------------- Object, fields, validation rule
    w("objects/Invoice__c/Invoice__c.object-meta.xml",
      f'<?xml version="1.0" encoding="UTF-8"?>\n<CustomObject {NS}>\n    <label>Invoice</label>\n'
      f'    <sharingModel>ReadWrite</sharingModel>\n    <externalSharingModel>ReadWrite</externalSharingModel>\n</CustomObject>\n')
    field("Amount__c", "Currency", "Invoice amount")
    field("Amount_Total__c", "Currency")
    field("AmountTotal__c", "Text")
    field("Status__c", "Picklist", "Lifecycle status")
    field("Owner_Queue__c", "Text")
    field("Legacy_Code__c", "Text")
    w("objects/Invoice__c/validationRules/Amount_Required.validationRule-meta.xml",
      f'<?xml version="1.0" encoding="UTF-8"?>\n<ValidationRule {NS}>\n    <fullName>Amount_Required</fullName>\n'
      f'    <active>true</active>\n'
      f"    <errorConditionFormula>ISBLANK(Amount__c) &amp;&amp; ISPICKVAL(Status__c, 'Approved') &amp;&amp; AmountTotal__c != ''</errorConditionFormula>\n"
      f'    <errorMessage>Amount is required</errorMessage>\n</ValidationRule>\n')

    # ---------------- Flows
    flow("Invoice_Set_Status", """
    <apiVersion>62.0</apiVersion>
    <label>Invoice Set Status</label>
    <processType>AutoLaunchedFlow</processType>
    <recordUpdates>
        <name>Set_Status</name>
        <label>Set Status</label>
        <inputAssignments>
            <field>Status__c</field>
            <value><stringValue>Review</stringValue></value>
        </inputAssignments>
        <inputAssignments>
            <field>Amount_Total__c</field>
            <value><numberValue>0.0</numberValue></value>
        </inputAssignments>
        <inputReference>$Record</inputReference>
    </recordUpdates>
    <start>
        <connector><targetReference>Set_Status</targetReference></connector>
        <object>Invoice__c</object>
        <recordTriggerType>CreateAndUpdate</recordTriggerType>
        <triggerType>RecordAfterSave</triggerType>
    </start>
    <status>Active</status>
""")
    flow("Invoice_Line_Sync", """
    <apiVersion>50.0</apiVersion>
    <label>Invoice Line Sync</label>
    <processType>AutoLaunchedFlow</processType>
    <recordLookups>
        <name>Get_Siblings</name>
        <label>Get Siblings</label>
        <connector><targetReference>Each_Sibling</targetReference></connector>
        <object>Invoice__c</object>
        <filters>
            <field>OwnerId</field>
            <operator>EqualTo</operator>
            <value><stringValue>0055e000001AbCdAAK</stringValue></value>
        </filters>
        <getFirstRecordOnly>false</getFirstRecordOnly>
    </recordLookups>
    <loops>
        <name>Each_Sibling</name>
        <label>Each Sibling</label>
        <collectionReference>Get_Siblings</collectionReference>
        <nextValueConnector><targetReference>Update_Sibling</targetReference></nextValueConnector>
    </loops>
    <recordUpdates>
        <name>Update_Sibling</name>
        <label>Update Sibling</label>
        <connector><targetReference>Each_Sibling</targetReference></connector>
        <inputAssignments>
            <field>Status__c</field>
            <value><stringValue>Synced</stringValue></value>
        </inputAssignments>
        <inputReference>Each_Sibling</inputReference>
    </recordUpdates>
    <start>
        <connector><targetReference>Get_Siblings</targetReference></connector>
        <object>Invoice__c</object>
        <recordTriggerType>Update</recordTriggerType>
        <triggerType>RecordAfterSave</triggerType>
    </start>
    <status>Active</status>
""")
    flow("Invoice_PB", """
    <apiVersion>48.0</apiVersion>
    <label>Invoice PB</label>
    <processType>Workflow</processType>
    <processMetadataValues>
        <name>ObjectType</name>
        <value><stringValue>Invoice__c</stringValue></value>
    </processMetadataValues>
    <processMetadataValues>
        <name>TriggerType</name>
        <value><stringValue>onAllChanges</stringValue></value>
    </processMetadataValues>
    <recordUpdates>
        <name>myRule_1_A1</name>
        <label>Set status</label>
        <filters>
            <field>Id</field>
            <operator>EqualTo</operator>
            <value><elementReference>myVariable_current.Id</elementReference></value>
        </filters>
        <inputAssignments>
            <field>Status__c</field>
            <value><stringValue>Processed</stringValue></value>
        </inputAssignments>
        <object>Invoice__c</object>
    </recordUpdates>
    <startElementReference>myRule_1_A1</startElementReference>
    <status>Active</status>
""")

    # ---------------- Workflow rule writing a field the flows also write
    w("workflows/Invoice__c.workflow-meta.xml", f"""
<?xml version="1.0" encoding="UTF-8"?>
<Workflow {NS}>
    <fieldUpdates>
        <fullName>Reset_Total</fullName>
        <field>Amount_Total__c</field>
        <formula>Amount__c</formula>
        <name>Reset Total</name>
        <operation>Formula</operation>
    </fieldUpdates>
    <rules>
        <fullName>Reset Total On Edit</fullName>
        <actions>
            <name>Reset_Total</name>
            <type>FieldUpdate</type>
        </actions>
        <active>true</active>
        <criteriaItems>
            <field>Invoice__c.Status__c</field>
            <operation>notEqual</operation>
            <value>Closed</value>
        </criteriaItems>
        <triggerType>onAllChanges</triggerType>
    </rules>
</Workflow>
""")
    (ROOT / "metadata" / "sfdx-project.json").write_text(
        '{"packageDirectories":[{"path":"force-app","default":true}],"sourceApiVersion":"62.0"}\n', encoding="utf-8")


if __name__ == "__main__":
    build()
    print(f"fixture written to {ROOT}")
