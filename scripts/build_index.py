"""Stage 2 - Index. Deterministic, no LLM.

Reads <run>/metadata and <run>/tools and writes <run>/index/:
  inventory.json       every component: id, type, name, path, object, lines, tokens, refs
  automation_map.json  per object: triggers, record-triggered flows (before/after save),
                       process builders, workflow rules, validation rules, and fields written
                       by more than one automation (overlaps)
  evidence.jsonl       tool findings normalized to {evidence_id, source, rule, component, path, line, severity, message}
  metrics.json         counts and facts that feed scoring directly
  flows/<Flow>.txt     compact flow digests (reviewers read these instead of raw XML)

Usage:
  python build_index.py --run <run folder>
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from glob import escape as glob_escape
from pathlib import Path

from common import Run, Timer, content_hash, estimate_tokens, info, load_run_config, read_json, write_json
from flows import parse_flow
from digests import build_digests
from org_facts import build_org_facts

NS = "{http://soap.sforce.com/2006/04/metadata}"
TRIGGER_HEAD = re.compile(r"trigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)
# Apex / SOQL are case-insensitive: invoice__C is the same object as Invoice__c.
CUSTOM_REF = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*__(?:c|mdt|e|x|b))\b", re.I)
IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

# Folder name -> component type, for everything not parsed specially.
FOLDER_TYPES = {
    "classes": "ApexClass", "triggers": "ApexTrigger", "flows": "Flow", "workflows": "Workflow",
    "lwc": "LightningComponentBundle", "aura": "AuraDefinitionBundle", "pages": "ApexPage",
    "components": "ApexComponent", "permissionsets": "PermissionSet", "profiles": "Profile",
    "permissionsetgroups": "PermissionSetGroup", "layouts": "Layout", "flexipages": "FlexiPage",
    "approvalProcesses": "ApprovalProcess", "assignmentRules": "AssignmentRules",
    "autoResponseRules": "AutoResponseRules", "escalationRules": "EscalationRules",
    "duplicateRules": "DuplicateRule", "matchingRules": "MatchingRules", "sharingRules": "SharingRules",
    "roles": "Role", "groups": "Group", "queues": "Queue", "customMetadata": "CustomMetadata",
    "labels": "CustomLabels", "wave": "WaveAsset", "tabs": "CustomTab", "applications": "CustomApplication",
    "settings": "Settings", "namedCredentials": "NamedCredential", "connectedApps": "ConnectedApp",
    "remoteSiteSettings": "RemoteSiteSetting", "platformEventChannels": "PlatformEventChannel",
}
WAVE_SUFFIX = {".wdpr": "WaveRecipe", ".wdf": "WaveDataflow", ".wdash": "WaveDashboard",
               ".wlens": "WaveLens", ".wds": "WaveDataset", ".xmd": "WaveXmd", ".wapp": "WaveApplication"}


def xml_text(el, tag: str, default: str = "") -> str:
    found = el.find(NS + tag) if el is not None else None
    return found.text.strip() if found is not None and found.text else default


def component_name(f: Path, folder: Path) -> str:
    """Member name of a file in a generic metadata folder: its path under the folder minus the type
    suffix. Account.Rec_1.md-meta.xml -> Account.Rec_1 (custom metadata records keep their type),
    Account.Log_Call.quickAction-meta.xml -> Account.Log_Call, MyPage.page -> MyPage,
    reports/Sales/Pipeline.report-meta.xml -> Sales/Pipeline (folder-qualified, like the manifest)."""
    rel = f.relative_to(folder).as_posix()
    if rel.endswith("-meta.xml"):
        rel = rel[: -len("-meta.xml")]
    head, _, last = rel.rpartition("/")
    if "." in last:
        last = last.rsplit(".", 1)[0]
    return f"{head}/{last}" if head else last


def _is_companion(f: Path) -> bool:
    """X.page-meta.xml next to X.page: the metadata half of one component, not a second one."""
    return f.name.endswith("-meta.xml") and f.with_name(f.name[: -len("-meta.xml")]).is_file()


def _bundle_dirs(folder: Path) -> set[Path]:
    """Sub-folders that are one component (an unzipped static resource, an experience bundle):
    a sibling X.<suffix>-meta.xml describes them. Report / email / document folders also have a
    sibling X.<kind>Folder-meta.xml but are containers, not components."""
    out = set()
    for d in (p for p in folder.iterdir() if p.is_dir()):
        metas = [m for m in folder.glob(f"{glob_escape(d.name)}.*-meta.xml") if m.is_file()]
        if metas and not any(m.name.endswith("Folder-meta.xml") for m in metas):
            out.add(d)
    return out


def find_source_root(meta: Path) -> Path:
    """Metadata lands in <run>/metadata/force-app/main/default (sf retrieve layout)."""
    for candidate in (meta / "force-app" / "main" / "default", meta / "force-app", meta):
        if candidate.exists() and any(candidate.iterdir()):
            return candidate
    return meta


# --------------------------------------------------------------------------- inventory
def build_inventory(src: Path, rel_base: Path, index_dir: Path) -> tuple[list[dict], dict]:
    items: list[dict] = []
    flows: dict[str, dict] = {}
    digest_dir = index_dir / "flows"
    digest_dir.mkdir(parents=True, exist_ok=True)

    def add(ctype, name, path: Path, obj="", **extra):
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        items.append({
            "id": f"{ctype}:{name}", "type": ctype, "name": name,
            "path": path.relative_to(rel_base).as_posix(), "object": obj,
            "lines": text.count("\n") + 1 if text else 0,
            "tokens": estimate_tokens(text) if text else 0, **extra,
        })
        return text

    for folder in sorted(p for p in src.iterdir() if p.is_dir()):
        kind = folder.name
        if kind == "classes":
            for f in sorted(folder.glob("*.cls")):
                text = f.read_text(encoding="utf-8", errors="replace")
                meta = f.with_name(f.name + "-meta.xml")
                api = xml_text(ET.parse(meta).getroot(), "apiVersion") if meta.exists() else ""
                is_test = bool(re.search(r"@istest\b|\btestmethod\b", text, re.I))
                add("ApexClass", f.stem, f, isTest=is_test, apiVersion=api)
        elif kind == "triggers":
            for f in sorted(folder.glob("*.trigger")):
                text = f.read_text(encoding="utf-8", errors="replace")
                m = TRIGGER_HEAD.search(text)
                obj = m.group(2) if m else ""
                events = [e.strip().lower() for e in m.group(3).split(",")] if m else []
                meta = f.with_name(f.name + "-meta.xml")
                root = ET.parse(meta).getroot() if meta.exists() else None
                add("ApexTrigger", f.stem, f, obj=obj, events=events,
                    active=xml_text(root, "status", "Active") == "Active", apiVersion=xml_text(root, "apiVersion"),
                    managedTool="dlrs" if "dlrs" in f.stem.lower() else "")
        elif kind == "flows":
            for f in sorted(folder.glob("*.flow-meta.xml")):
                facts = parse_flow(f)
                digest_path = digest_dir / f"{facts['name']}.txt"
                digest_path.write_text(facts["digest"], encoding="utf-8")
                flows[facts["name"]] = facts
                add("Flow", facts["name"], f, obj=facts["object"], kind=facts["kind"],
                    status=facts["status"], triggerType=facts["triggerType"],
                    recordTriggerType=facts["recordTriggerType"], apiVersion=facts["apiVersion"],
                    elementCount=facts["elementCount"],
                    digestPath=digest_path.relative_to(rel_base).as_posix(),
                    digestTokens=estimate_tokens(facts["digest"]))
        elif kind == "workflows":
            for f in sorted(folder.glob("*.workflow-meta.xml")):
                obj = f.name.replace(".workflow-meta.xml", "")
                root = ET.parse(f).getroot()
                updates = {xml_text(u, "fullName"): xml_text(u, "field") for u in root.findall(NS + "fieldUpdates")}
                for rule in root.findall(NS + "rules"):
                    actions = [xml_text(a, "name") for a in rule.findall(NS + "actions")]
                    timed = [xml_text(a, "name") for tw in rule.findall(NS + "workflowTimeTriggers")
                             for a in tw.findall(NS + "actions")]
                    add("WorkflowRule", f"{obj}.{xml_text(rule, 'fullName')}", f, obj=obj,
                        active=xml_text(rule, "active") == "true", triggerType=xml_text(rule, "triggerType"),
                        actions=actions + timed,
                        fieldsWritten=sorted({f"{obj}.{updates[a]}" for a in actions + timed if a in updates}))
        elif kind == "objects":
            for odir in sorted(p for p in folder.iterdir() if p.is_dir()):
                obj = odir.name
                ofile = odir / f"{obj}.object-meta.xml"
                if ofile.exists():
                    root = ET.parse(ofile).getroot()
                    add("CustomObject", obj, ofile, obj=obj, sharingModel=xml_text(root, "sharingModel"))
                for f in sorted((odir / "fields").glob("*.field-meta.xml")):
                    root = ET.parse(f).getroot()
                    add("CustomField", f"{obj}.{f.name.split('.')[0]}", f, obj=obj,
                        fieldType=xml_text(root, "type"), referenceTo=xml_text(root, "referenceTo"),
                        hasDescription=bool(xml_text(root, "description")),
                        isFormula=bool(xml_text(root, "formula")))
                for f in sorted((odir / "validationRules").glob("*.validationRule-meta.xml")):
                    root = ET.parse(f).getroot()
                    add("ValidationRule", f"{obj}.{f.name.split('.')[0]}", f, obj=obj,
                        active=xml_text(root, "active") == "true")
                for sub, ctype in (("recordTypes", "RecordType"), ("listViews", None), ("webLinks", "WebLink"),
                                   ("fieldSets", "FieldSet"), ("compactLayouts", None), ("businessProcesses", "BusinessProcess")):
                    if ctype:
                        for f in sorted((odir / sub).glob("*-meta.xml")):
                            add(ctype, f"{obj}.{f.name.split('.')[0]}", f, obj=obj)
        elif kind == "wave":
            for f in sorted(folder.iterdir()):
                ctype = next((t for s, t in WAVE_SUFFIX.items() if f.name.endswith(s)), None)
                if ctype:
                    add(ctype, f.name.split(".")[0], f)
        elif kind in ("lwc", "aura"):
            for bundle in sorted(p for p in folder.iterdir() if p.is_dir()):
                files = [p for p in bundle.rglob("*") if p.is_file()]
                text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
                items.append({"id": f"{FOLDER_TYPES[kind]}:{bundle.name}", "type": FOLDER_TYPES[kind],
                              "name": bundle.name, "path": bundle.relative_to(rel_base).as_posix(),
                              "object": "", "lines": text.count("\n") + 1, "tokens": estimate_tokens(text),
                              "files": len(files)})
        else:
            ctype = FOLDER_TYPES.get(kind, kind)
            bundles = _bundle_dirs(folder)
            for b in sorted(bundles):
                files = [p for p in b.rglob("*") if p.is_file()]
                text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
                items.append({"id": f"{ctype}:{b.name}", "type": ctype, "name": b.name,
                              "path": b.relative_to(rel_base).as_posix(), "object": "",
                              "lines": text.count("\n") + 1 if text else 0,
                              "tokens": estimate_tokens(text) if text else 0, "files": len(files)})
            bundle_metas = {m for b in bundles for m in folder.glob(f"{glob_escape(b.name)}.*-meta.xml")}
            for f in sorted(p for p in folder.rglob("*") if p.is_file()):
                if f in bundle_metas or any(b in f.parents for b in bundles) or _is_companion(f):
                    continue
                add(ctype, component_name(f, folder), f)
    return items, flows


def add_references(items: list[dict], rel_base: Path) -> None:
    """Which classes / objects each Apex class, trigger and flow refers to (name matching)."""
    class_names = {i["name"] for i in items if i["type"] == "ApexClass"}
    lower_objects = {i["name"].lower(): i["name"] for i in items if i["type"] == "CustomObject"}
    lower_classes = {c.lower(): c for c in class_names}
    for item in items:
        if item["type"] not in ("ApexClass", "ApexTrigger", "Flow"):
            continue
        text = (rel_base / item["path"]).read_text(encoding="utf-8", errors="replace")
        if item["type"] != "Flow":
            text = re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.S)
        idents = set(IDENT.findall(text))
        refs_classes = sorted({lower_classes[i.lower()] for i in idents if i.lower() in lower_classes} - {item["name"]})
        # Case-insensitive; reported in the object's own spelling when it was retrieved.
        mentioned = {i.lower() for i in idents if i.lower() in lower_objects}
        mentioned |= {r.lower() for r in CUSTOM_REF.findall(text)}
        own = (item.get("object") or "").lower()
        refs_objects = sorted({lower_objects.get(o, o) for o in mentioned if o != own})
        item["refs"] = {"classes": refs_classes, "objects": [o for o in refs_objects if o]}


# --------------------------------------------------------------------------- automation map
def build_automation_map(items: list[dict], flows: dict) -> dict:
    by_obj: dict[str, dict] = defaultdict(lambda: {
        "triggers": [], "flowsBeforeSave": [], "flowsAfterSave": [], "flowsBeforeDelete": [],
        "processBuilders": [], "workflowRules": [], "validationRules": [], "writers": defaultdict(list)})

    for i in items:
        obj = i.get("object")
        if not obj:
            continue
        if i["type"] == "ApexTrigger":
            by_obj[obj]["triggers"].append({"name": i["name"], "events": i["events"], "active": i["active"],
                                            "managedTool": i.get("managedTool", ""), "path": i["path"]})
        elif i["type"] == "Flow" and i["kind"] in ("record-triggered", "process-builder"):
            f = flows[i["name"]]
            entry = {"name": i["name"], "status": f["status"], "on": f["recordTriggerType"],
                     "entry": f["entryFilters"] or ([f["entryFormula"]] if f["entryFormula"] else []),
                     "fieldsWritten": f["fieldsWritten"], "digestPath": i["digestPath"]}
            bucket = {"RecordBeforeSave": "flowsBeforeSave", "RecordAfterSave": "flowsAfterSave",
                      "RecordBeforeDelete": "flowsBeforeDelete"}.get(f["triggerType"], "processBuilders")
            by_obj[obj][bucket].append(entry)
            if f["status"] == "Active":
                label = "ProcessBuilder" if f["isProcessBuilder"] else "Flow"
                for fw in f["fieldsWritten"]:
                    field = fw.split(".", 1)[1] if "." in fw else fw
                    if fw.startswith("$Record") or fw.startswith(obj + "."):
                        by_obj[obj]["writers"][field].append(f"{label}:{i['name']}")
        elif i["type"] == "WorkflowRule":
            by_obj[obj]["workflowRules"].append({"name": i["name"], "active": i["active"],
                                                 "triggerType": i["triggerType"], "actions": i["actions"],
                                                 "fieldsWritten": i["fieldsWritten"]})
            if i["active"]:
                for fw in i["fieldsWritten"]:
                    by_obj[obj]["writers"][fw.split(".", 1)[1]].append(f"WorkflowRule:{i['name']}")
        elif i["type"] == "ValidationRule":
            by_obj[obj]["validationRules"].append({"name": i["name"], "active": i["active"]})

    out = {}
    for obj, data in sorted(by_obj.items()):
        writers = data.pop("writers")
        active = (sum(t["active"] for t in data["triggers"])
                  + sum(f["status"] == "Active" for k in ("flowsBeforeSave", "flowsAfterSave", "flowsBeforeDelete", "processBuilders") for f in data[k])
                  + sum(w["active"] for w in data["workflowRules"]))
        if not active and not data["validationRules"]:
            continue
        data["activeAutomationCount"] = active
        data["fieldOverlaps"] = {f: w for f, w in sorted(writers.items()) if len(set(w)) > 1}
        out[obj] = data
    return out


def write_automation_summaries(automation: dict, items: list[dict], out: Path) -> None:
    """Per object, everything that runs on save, in Salesforce order of execution."""
    out.mkdir(parents=True, exist_ok=True)
    dup_rules = Counter(i["name"].split(".")[0] for i in items if i["type"] == "DuplicateRule")
    for obj, a in automation.items():
        act = lambda xs: [x for x in xs if x.get("status", "Active") == "Active"]
        lines = [f"AUTOMATION ON {obj}  (active automations: {a['activeAutomationCount']})",
                 "Order of execution on save (Salesforce):", ""]

        def section(title, rows):
            lines.append(f"{title}:")
            lines.extend(f"  - {r}" for r in rows) if rows else lines.append("  (none)")

        fmt_flow = lambda f: f"{f['name']} on={f['on']} entry={'; '.join(f['entry'])[:200] or 'always'} writes={', '.join(f['fieldsWritten'])[:300] or '-'}"
        section("1. Before-save record-triggered flows", [fmt_flow(f) for f in act(a["flowsBeforeSave"])])
        before = [t for t in a["triggers"] if t["active"] and any(e.startswith("before") for e in t["events"])]
        section("2. Apex BEFORE triggers", [f"{t['name']} {t['events']}{' (dlrs generated)' if t['managedTool'] else ''}" for t in before])
        section("3. Validation rules (active)", [v["name"] for v in a["validationRules"] if v["active"]])
        section("4. Duplicate rules", [f"{dup_rules[obj]} rule(s)"] if dup_rules.get(obj) else [])
        after = [t for t in a["triggers"] if t["active"] and any(e.startswith("after") for e in t["events"])]
        section("5. Apex AFTER triggers", [f"{t['name']} {t['events']}{' (dlrs generated)' if t['managedTool'] else ''}" for t in after])
        section("6. Workflow rules (active; field updates re-run before/after update triggers once)",
                [f"{w['name']} trigger={w['triggerType']} writes={', '.join(w['fieldsWritten']) or '-'}" for w in a["workflowRules"] if w["active"]])
        section("7. Process Builders (active)", [fmt_flow(f) for f in act(a["processBuilders"])])
        section("8. After-save record-triggered flows", [fmt_flow(f) for f in act(a["flowsAfterSave"])])
        inactive = [f["name"] for k in ("flowsBeforeSave", "flowsAfterSave", "processBuilders") for f in a[k] if f["status"] != "Active"]
        lines += ["", f"Inactive / draft versions on this object (not running): {', '.join(inactive) or 'none'}",
                  f"Inactive workflow rules: {', '.join(w['name'] for w in a['workflowRules'] if not w['active']) or 'none'}", "",
                  "FIELDS WRITTEN BY MORE THAN ONE ACTIVE AUTOMATION:"]
        lines += [f"  {f}: {', '.join(w)}" for f, w in a["fieldOverlaps"].items()] or ["  none"]
        (out / f"{obj}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def register_generated(items: list[dict], run_root: Path, index_dir: Path) -> None:
    """Generated tables become inventory components so findings about them can be validated."""
    for folder, ctype in (("orgfacts", "OrgFact"), ("datamodel", "FieldUsage"), ("automation", "AutomationSummary")):
        for f in sorted((index_dir / folder).glob("*.txt")):
            text = f.read_text(encoding="utf-8")
            items.append({"id": f"{ctype}:{f.stem}", "type": ctype, "name": f.stem,
                          "path": f.relative_to(run_root).as_posix(), "object": f.stem if ctype != "OrgFact" else "",
                          "lines": text.count("\n"), "tokens": estimate_tokens(text)})


def add_hashes(items: list[dict], run_root: Path) -> None:
    """Content hash per component for incremental runs: the source, plus the digest reviewers read
    (so a change in how digests are built also counts as a change)."""
    for i in items:
        p = run_root / i["path"]
        if p.is_dir():
            text = "".join(f.relative_to(p).as_posix() + f.read_text(encoding="utf-8", errors="replace")
                           for f in sorted(x for x in p.rglob("*") if x.is_file()))
        else:
            text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        if i.get("digestPath") and (run_root / i["digestPath"]).is_file():
            text += (run_root / i["digestPath"]).read_text(encoding="utf-8", errors="replace")
        i["sha"] = content_hash(text)


def write_api_versions(items: list[dict], out_file: Path) -> None:
    rows = sorted((float(i["apiVersion"]), i["type"], i["name"]) for i in items
                  if i.get("apiVersion") and i["type"] in ("ApexClass", "ApexTrigger", "Flow"))
    out_file.write_text("# API version per Apex class / trigger / flow, oldest first\nApiVersion | Type | Name\n"
                        + "\n".join(f"{v} | {t} | {n}" for v, t, n in rows) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- evidence
def normalize_code_analyzer(tools_dir: Path, path_index: dict, cfg: dict) -> tuple[list[dict], dict]:
    """Keep severity <= maxSeverity (minus style rules) as evidence; count every rule."""
    data = read_json(tools_dir / "code-analyzer.json")
    if not data:
        return [], {}
    ev_cfg = cfg["evidence"]
    exclude, include = set(ev_cfg["excludeRules"]), set(ev_cfg["includeRules"])
    # Code Analyzer paths are relative to the workspace root (metadata/), e.g. force-app\main\...
    by_suffix = {p.split("metadata/", 1)[-1]: p for p in path_index}
    out, rule_counts = [], Counter()
    for n, v in enumerate(data.get("violations", []), 1):
        rule_counts[f"{v.get('engine')}:{v.get('rule')}:sev{v.get('severity')}"] += 1
        sev = v.get("severity") or 5
        if v.get("rule") not in include and (sev > ev_cfg["maxSeverity"] or v.get("rule") in exclude):
            continue
        locs = v.get("locations") or [{}]
        loc = locs[v.get("primaryLocationIndex", 0) or 0] if locs else {}
        file = (loc.get("file") or "").replace("\\", "/")
        key = file.split("metadata/", 1)[-1]
        rel = by_suffix.get(key)
        if rel is None and key.endswith("-meta.xml"):
            # e.g. classes/X.cls-meta.xml belongs to classes/X.cls
            main = by_suffix.get(key[: -len("-meta.xml")])
            rel = "metadata/" + key if main else None
            component = path_index.get(main, "") if main else ""
        else:
            component = path_index.get(rel, "") if rel else ""
        if not component:
            # Files inside an LWC / Aura / static-resource bundle belong to the bundle folder: walk
            # up the file's parent folders (one dict lookup each, not a scan of the whole inventory).
            parts = key.split("/")
            bundle = next((by_suffix[d] for d in ("/".join(parts[:n]) for n in range(len(parts) - 1, 0, -1))
                           if d in by_suffix), None)
            component = path_index.get(bundle, "") if bundle else ""
        rel = rel or "metadata/" + key
        out.append({
            "evidence_id": f"CA-{n:05d}", "source": "code-analyzer", "engine": v.get("engine"),
            "rule": v.get("rule"), "severity": v.get("severity"), "message": v.get("message"),
            "component": component, "path": rel, "line": loc.get("startLine"),
        })
    return out, dict(rule_counts.most_common())


# --------------------------------------------------------------------------- metrics
def build_metrics(items: list[dict], flows: dict, automation: dict, evidence: list[dict]) -> dict:
    by_type = Counter(i["type"] for i in items)
    classes = [i for i in items if i["type"] == "ApexClass"]
    flow_items = [i for i in items if i["type"] == "Flow"]
    api_versions = Counter(i.get("apiVersion") for i in items if i.get("apiVersion"))
    triggers_per_obj = Counter(i["object"] for i in items if i["type"] == "ApexTrigger" and i.get("active"))
    return {
        "componentCounts": dict(sorted(by_type.items())),
        "apex": {
            "classes": len(classes), "testClasses": sum(c.get("isTest") for c in classes),
            "lines": sum(c["lines"] for c in classes),
            "triggers": by_type.get("ApexTrigger", 0),
            "objectsWithMultipleTriggers": {o: n for o, n in triggers_per_obj.items() if n > 1},
        },
        "flows": {
            "total": len(flow_items),
            "byKind": dict(Counter(f["kind"] for f in flow_items)),
            "byStatus": dict(Counter(f["status"] for f in flow_items)),
            "activeProcessBuilders": sum(1 for f in flows.values() if f["isProcessBuilder"] and f["status"] == "Active"),
            "withDmlInLoop": sorted(n for n, f in flows.items() if f["dmlInLoop"] and f["status"] == "Active"),
            "withHardcodedIds": sorted(n for n, f in flows.items() if f["hardcodedIds"]),
            # noFaultPath leaves out before-save "update triggering record" steps (not DML, can't
            # have a fault path) -- the same exclusion rule_checks.py applies.
            "activeElementsWithoutFaultPath": sum(len(f["noFaultPath"]) for f in flows.values() if f["status"] == "Active"),
        },
        "workflowRules": {
            "total": by_type.get("WorkflowRule", 0),
            "active": sum(1 for i in items if i["type"] == "WorkflowRule" and i["active"]),
        },
        "automation": {
            "objectsWithAutomation": len(automation),
            "objectsWithFieldOverlaps": sorted(o for o, a in automation.items() if a["fieldOverlaps"]),
            "busiestObjects": sorted(((o, a["activeAutomationCount"]) for o, a in automation.items()),
                                     key=lambda x: -x[1])[:10],
        },
        "dataModel": {
            "customObjects": sum(1 for i in items if i["type"] == "CustomObject" and i["name"].endswith("__c")),
            "fields": by_type.get("CustomField", 0),
            "fieldsWithoutDescription": sum(1 for i in items if i["type"] == "CustomField" and not i.get("hasDescription")
                                            and "__c" in i["name"]),
        },
        "apiVersions": dict(sorted(api_versions.items(), key=lambda x: float(x[0] or 0))),
        "evidence": dict(Counter(f"{e['source']}:{e.get('severity')}" for e in evidence)),
        "totalSourceTokens": sum(i["tokens"] for i in items),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run folder created by collect.py")
    ap.add_argument("--config")
    args = ap.parse_args()
    run = Run(Path(args.run))
    cfg = load_run_config(run, args.config)

    with Timer() as t:
        # Rebuild from scratch so no digest from an earlier index run lingers.
        for sub in ("flows", "workflows", "crma", "datamodel", "orgfacts", "automation"):
            shutil.rmtree(run.path("index", sub), ignore_errors=True)
        src = find_source_root(run.path("metadata"))
        items, flows = build_inventory(src, run.root, run.path("index"))
        add_references(items, run.root)
        digest_stats = build_digests(items, run.root, run.path("index"))
        automation = build_automation_map(items, flows)
        path_index = {i["path"]: i["id"] for i in items}
        evidence, rule_counts = normalize_code_analyzer(run.path("tools"), path_index, cfg)
        metrics = build_metrics(items, flows, automation, evidence)
        metrics["codeAnalyzerRuleCounts"] = rule_counts
        metrics["org"] = build_org_facts(run.path("tools"), run.path("index", "orgfacts"))
        metrics["digests"] = digest_stats
        write_api_versions(items, run.path("index", "orgfacts", "api_versions.txt"))
        write_automation_summaries(automation, items, run.path("index", "automation"))
        register_generated(items, run.root, run.path("index"))
        add_hashes(items, run.root)

        write_json(run.path("index", "inventory.json"), items)
        write_json(run.path("index", "automation_map.json"), automation)
        write_json(run.path("index", "metrics.json"), metrics)
        with run.path("index", "evidence.jsonl").open("w", encoding="utf-8") as fh:
            for e in evidence:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    run.log_step("index", "ok", t.seconds, components=len(items), flows=len(flows),
                 automatedObjects=len(automation), evidence=len(evidence))
    info(f"INDEX: {len(items)} components, {len(flows)} flows, {len(automation)} automated objects, "
         f"{len(evidence)} evidence rows ({t.seconds:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
