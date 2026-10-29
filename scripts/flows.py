"""Flow metadata parsing: facts for the automation map and a compact digest for reviewers.

Raw flow XML is verbose (one pilot org: ~105k lines for 194 flows). Reviewers get a deterministic digest
instead: one line per element with its connectors, plus computed facts (DML in loops, missing
fault paths, hard-coded IDs). The digest is written to index/flows/<Flow>.txt and is a valid
evidence path for findings.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "{http://soap.sforce.com/2006/04/metadata}"
SF_ID = re.compile(r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{3}[A-Za-z0-9]{2}0[A-Za-z0-9]{9}(?:[A-Za-z0-9]{3})?\b")

ELEMENT_TYPES = (
    "actionCalls", "apexPluginCalls", "assignments", "collectionProcessors", "customErrors",
    "decisions", "loops", "orchestratedStages", "recordCreates", "recordDeletes",
    "recordLookups", "recordRollbacks", "recordUpdates", "screens", "steps", "subflows",
    "transforms", "waits",
)
DML_TYPES = {"recordCreates", "recordUpdates", "recordDeletes"}
QUERY_TYPES = {"recordLookups"}
# Elements that can fail at run time and accept a faultConnector. Wait (pause) elements do too:
# a resume can fail, and without a fault path the interview just errors out.
FAULTABLE = DML_TYPES | QUERY_TYPES | {"actionCalls", "apexPluginCalls", "subflows", "waits"}


def _t(el, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(_nspath(path))
    return found.text.strip() if found is not None and found.text else default


def _nspath(path: str) -> str:
    return "/".join(NS + p for p in path.split("/"))


def _value(el) -> str:
    """Render a <value> element (stringValue, elementReference, numberValue, booleanValue...)."""
    if el is None:
        return ""
    for child in el:
        tag = child.tag.replace(NS, "")
        text = (child.text or "").strip()
        if tag == "elementReference":
            return "{!" + text + "}"
        if tag == "stringValue":
            return '"' + text + '"'
        return text
    return ""


def _filters(el, tag: str = "filters") -> list[str]:
    out = []
    for f in el.findall(NS + tag):
        field = _t(f, "field") or _t(f, "leftValueReference")
        op = _t(f, "operator")
        out.append(f"{field} {op} {_value(f.find(NS + 'value') if f.find(NS + 'value') is not None else f.find(NS + 'rightValue'))}".strip())
    return out


def _targets(el, etype: str) -> dict[str, list[str]]:
    """Outgoing connectors of an element, by kind."""
    out: dict[str, list[str]] = {}

    def add(kind, conn):
        if conn is not None:
            ref = _t(conn, "targetReference")
            if ref:
                out.setdefault(kind, []).append(ref)

    add("next", el.find(NS + "connector"))
    add("fault", el.find(NS + "faultConnector"))
    if etype == "decisions":
        for rule in el.findall(NS + "rules"):
            add("rule:" + _t(rule, "name"), rule.find(NS + "connector"))
        add("default", el.find(NS + "defaultConnector"))
    if etype == "loops":
        add("each", el.find(NS + "nextValueConnector"))
        add("done", el.find(NS + "noMoreValuesConnector"))
    if etype == "waits":
        for ev in el.findall(NS + "waitEvents"):
            add("event:" + _t(ev, "name"), ev.find(NS + "connector"))
        add("default", el.find(NS + "defaultConnector"))
    return out


def parse_flow(path: Path) -> dict:
    root = ET.parse(path).getroot()
    name = path.name.replace(".flow-meta.xml", "")
    start = root.find(NS + "start")
    process_type = _t(root, "processType")

    facts = {
        "name": name,
        "label": _t(root, "label"),
        "processType": process_type,
        "status": _t(root, "status"),
        "apiVersion": _t(root, "apiVersion"),
        "object": _t(start, "object") if start is not None else "",
        "triggerType": _t(start, "triggerType") if start is not None else "",
        "recordTriggerType": _t(start, "recordTriggerType") if start is not None else "",
        "entryFilterLogic": _t(start, "filterLogic") if start is not None else "",
        "entryFilters": _filters(start) if start is not None else [],
        "entryFormula": _t(start, "filterFormula") if start is not None else "",
        "isProcessBuilder": process_type == "Workflow",
    }
    if facts["isProcessBuilder"]:
        for pm in root.findall(NS + "processMetadataValues"):
            if _t(pm, "name") == "ObjectType":
                facts["object"] = _value(pm.find(NS + "value")).strip('"')
            if _t(pm, "name") == "TriggerType":
                facts["recordTriggerType"] = _value(pm.find(NS + "value")).strip('"')

    if facts["triggerType"] in ("RecordBeforeSave", "RecordAfterSave", "RecordBeforeDelete"):
        facts["kind"] = "record-triggered"
    elif facts["isProcessBuilder"]:
        facts["kind"] = "process-builder"
    elif facts["triggerType"] == "Scheduled":
        facts["kind"] = "scheduled"
    elif facts["triggerType"] == "PlatformEvent":
        facts["kind"] = "platform-event"
    elif process_type == "Flow":
        facts["kind"] = "screen"
    else:
        facts["kind"] = "autolaunched"

    # ---- elements and graph
    elements: dict[str, dict] = {}
    for etype in ELEMENT_TYPES:
        for el in root.findall(NS + etype):
            ename = _t(el, "name")
            elements[ename] = {"type": etype, "el": el, "targets": _targets(el, etype)}

    # Elements inside each loop body: reachable from the loop's "each" connector without
    # passing back through the loop itself.
    in_loop: dict[str, str] = {}
    for lname, info in elements.items():
        if info["type"] != "loops":
            continue
        stack = list(info["targets"].get("each", []))
        seen = set()
        while stack:
            cur = stack.pop()
            if cur in seen or cur == lname or cur not in elements:
                continue
            seen.add(cur)
            in_loop.setdefault(cur, lname)
            for kind, refs in elements[cur]["targets"].items():
                if kind != "fault":
                    stack.extend(refs)

    fields_written: list[str] = []
    issues: list[str] = []
    lines: list[str] = []
    before_save = facts["triggerType"] == "RecordBeforeSave"

    # Record variables: name -> sObject type, and fields set on them by Assignment elements. An
    # Update Records that saves the variable (inputReference) writes those fields.
    var_types = {_t(v, "name"): _t(v, "objectType") for v in root.findall(NS + "variables")
                 if _t(v, "dataType") == "SObject"}
    assigned: dict[str, set[str]] = {}
    for el in root.findall(NS + "assignments"):
        for item in el.findall(NS + "assignmentItems"):
            var, _, fld = _t(item, "assignToReference").partition(".")
            if fld and "." not in fld:
                assigned.setdefault(var, set()).add(fld)

    def update_record_ref(etype, el) -> bool:
        """'Update triggering record' in a before-save flow: not DML, can't take a fault path."""
        return before_save and etype == "recordUpdates" and not _t(el, "object") \
            and _t(el, "inputReference") == "$Record"

    for ename, info in elements.items():
        etype, el, targets = info["type"], info["el"], info["targets"]
        detail = ""
        if etype in DML_TYPES or etype in QUERY_TYPES:
            obj = _t(el, "object")
            ref = _t(el, "inputReference")
            target = obj or (ref and "{!" + ref + "}")
            assigns = [_t(a, "field") for a in el.findall(NS + "inputAssignments")]
            if etype == "recordUpdates":
                write_obj = obj or ("$Record" if ref.startswith("$Record") else var_types.get(ref) or ref)
                fields_written += [f"{write_obj}.{f}" for f in assigns if f]
                if ref and not obj:
                    fields_written += [f"{write_obj}.{f}" for f in sorted(assigned.get(ref, ()))]
            flt = _filters(el)
            detail = f"{target}" + (f" where {' AND '.join(flt)}" if flt else "")
            if assigns:
                detail += f" set [{', '.join(assigns)}]"
        elif etype == "assignments":
            items = []
            for item in el.findall(NS + "assignmentItems"):
                ref = _t(item, "assignToReference")
                items.append(f"{ref} {_t(item, 'operator')} {_value(item.find(NS + 'value'))}")
                if ref.startswith("$Record.") and before_save:
                    fields_written.append(f"$Record.{ref.split('.', 1)[1]}")
            detail = "; ".join(items)
        elif etype == "decisions":
            rules = []
            for rule in el.findall(NS + "rules"):
                conds = _filters(rule, "conditions")
                logic = _t(rule, "conditionLogic")
                rules.append(f"{_t(rule, 'name')}({(' ' + logic + ' ').join(conds) if logic in ('and', 'or') else logic + ': ' + '; '.join(conds)})")
            detail = " | ".join(rules)
        elif etype == "loops":
            detail = f"over {_t(el, 'collectionReference')}"
        elif etype == "actionCalls":
            detail = f"{_t(el, 'actionType')}:{_t(el, 'actionName')}"
        elif etype == "subflows":
            detail = f"calls {_t(el, 'flowName')}"
        elif etype == "customErrors":
            detail = "custom error"

        conn = " ".join(f"{k}->{','.join(v)}" for k, v in targets.items())
        flags = []
        if ename in in_loop and (etype in DML_TYPES or etype in QUERY_TYPES or etype == "subflows"):
            flags.append(f"IN-LOOP:{in_loop[ename]}")
            issues.append(f"{etype} '{ename}' runs inside loop '{in_loop[ename]}'")
        if etype in FAULTABLE and "fault" not in targets and not update_record_ref(etype, el):
            flags.append("NO-FAULT-PATH")
        lines.append(f"[{etype}] {ename}: {detail}  ({conn or 'end'})" + (f"  !{' !'.join(flags)}" if flags else ""))

    no_fault = [n for n, i in elements.items() if i["type"] in FAULTABLE and "fault" not in i["targets"]
                and not update_record_ref(i["type"], i["el"])]
    if no_fault:
        issues.append(f"{len(no_fault)} element(s) with no fault path: {', '.join(no_fault[:12])}")

    raw = path.read_text(encoding="utf-8", errors="replace")
    ids = sorted(set(SF_ID.findall(raw)))
    if ids:
        issues.append(f"possible hard-coded record IDs: {', '.join(ids[:8])}")

    start_ref = ""
    if start is not None:
        start_ref = _t(start, "connector/targetReference")
    start_ref = start_ref or _t(root, "startElementReference")

    header = [
        f"FLOW {name}  label=\"{facts['label']}\"",
        f"type={facts['kind']} processType={process_type} status={facts['status']} api={facts['apiVersion']}",
    ]
    if facts["object"]:
        header.append(f"object={facts['object']} trigger={facts['triggerType'] or '-'} on={facts['recordTriggerType'] or '-'}")
    if facts["entryFilters"] or facts["entryFormula"]:
        header.append(f"entry: logic={facts['entryFilterLogic'] or '-'} "
                      f"{' ; '.join(facts['entryFilters'])}{(' formula=' + facts['entryFormula']) if facts['entryFormula'] else ''}")
    header.append(f"start -> {start_ref or '?'}   elements={len(elements)}")
    if issues:
        header.append("FACTS: " + " | ".join(issues))

    facts.update({
        "elementCount": len(elements),
        "fieldsWritten": sorted(set(fields_written)),
        "dmlInLoop": [n for n in in_loop if elements[n]["type"] in DML_TYPES | QUERY_TYPES],
        "noFaultPath": no_fault,
        "hardcodedIds": ids,
        "digest": "\n".join(header + [""] + lines) + "\n",
    })
    return facts
