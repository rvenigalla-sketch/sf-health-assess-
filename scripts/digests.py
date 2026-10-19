"""Deterministic digests and tables for reviewers (called by build_index.py).

  index/workflows/<Object>.txt   Workflow Rules: criteria, actions, field updates
  index/crma/<asset>.txt         CRM Analytics dashboards / recipes / dataflows, as compact text
  index/datamodel/<Object>.txt   custom fields with where-used counts (Apex, Flow, VR, layouts...)
  index/orgfacts/sharing.txt     org-wide defaults and sharing-rule counts per object

Every file is plain text with stable line numbers, so findings can cite it like source.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from common import estimate_tokens

NS = "{http://soap.sforce.com/2006/04/metadata}"
SEP = " | "


def _x(el, tag, default=""):
    f = el.find(NS + tag) if el is not None else None
    return f.text.strip() if f is not None and f.text else default


def _clip(text, n=300):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 3] + "..."


# --------------------------------------------------------------------------- workflow rules
def workflow_digest(path: Path) -> str:
    root = ET.parse(path).getroot()
    obj = path.name.replace(".workflow-meta.xml", "")
    updates = {_x(u, "fullName"): u for u in root.findall(NS + "fieldUpdates")}
    alerts = {_x(a, "fullName") for a in root.findall(NS + "alerts")}
    lines = [f"WORKFLOW RULES on {obj}", ""]
    for rule in root.findall(NS + "rules"):
        crit = [f"{_x(c, 'field')} {_x(c, 'operation')} {_x(c, 'value')!r}" for c in rule.findall(NS + "criteriaItems")]
        formula = _x(rule, "formula")
        lines.append(f"RULE {_x(rule, 'fullName')}  active={_x(rule, 'active')}  trigger={_x(rule, 'triggerType')}")
        lines.append(f"  criteria: {_x(rule, 'booleanFilter') or 'AND'} {' ; '.join(crit)}" + (f" formula={_clip(formula, 400)}" if formula else ""))
        actions = [(a, False) for a in rule.findall(NS + "actions")]
        for tw in rule.findall(NS + "workflowTimeTriggers"):
            actions += [(a, True) for a in tw.findall(NS + "actions")]
        for a, timed in actions:
            name, kind = _x(a, "name"), _x(a, "type")
            detail = ""
            if kind == "FieldUpdate" and name in updates:
                u = updates[name]
                val = _x(u, "formula") or _x(u, "literalValue") or _x(u, "lookupValue") or _x(u, "operation")
                detail = f" sets {_x(u, 'field')} = {_clip(val, 200)}" + (" (re-evaluates rules)" if _x(u, "reevaluateOnChange") == "true" else "")
            lines.append(f"  action{' (time-based)' if timed else ''}: {kind} {name}{detail}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CRM Analytics
def _query_text(q):
    if isinstance(q, str):
        return q
    if isinstance(q, dict):
        return q.get("query") or q.get("pigql") or q.get("saql") or json.dumps(
            {k: v for k, v in q.items() if k in ("measures", "groups", "filters", "values", "order", "limit")})
    return ""


def dashboard_digest(path: Path) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    steps = d.get("steps", {})
    widgets = d.get("widgets", {})
    pages = [p for g in d.get("gridLayouts", []) for p in g.get("pages", [])]
    datasets = Counter()
    lines = [f"DASHBOARD {path.stem}", f"steps={len(steps)} widgets={len(widgets)} pages={len(pages)} "
             f"globalFilters={len(d.get('filters', []))}", ""]
    for name, s in steps.items():
        ds = [x.get("name") or x.get("id") for x in s.get("datasets", []) if isinstance(x, dict)]
        datasets.update(ds)
        q = _query_text(s.get("query"))
        flags = []
        if s.get("type") in ("saql", "aggregateflex") and isinstance(q, str) and "limit" not in q.lower() and s.get("type") == "saql":
            flags.append("NO-LIMIT")
        if s.get("type") == "soql":
            flags.append("LIVE-SOQL")
        lines.append(f"[step] {name}: type={s.get('type')} datasets={','.join(filter(None, ds)) or '-'} "
                     f"facet={s.get('broadcastFacet')}/{bool(s.get('receiveFacetSource'))} "
                     f"query={_clip(q, 350)}" + (f"  !{' !'.join(flags)}" if flags else ""))
    used_steps = set()
    for w in widgets.values():
        step = (w.get("parameters") or {}).get("step")
        if step:
            used_steps.add(step)
    unused = sorted(set(steps) - used_steps - {k for k, s in steps.items() if s.get("type") in ("staticflex",)})
    lines += ["", f"widget types: {dict(Counter(w.get('type') for w in widgets.values()))}",
              f"datasets used: {dict(datasets)}"]
    if unused:
        lines.append(f"FACTS: {len(unused)} step(s) not bound to any widget: {', '.join(unused[:20])}")
    return "\n".join(lines) + "\n"


def recipe_digest(path: Path) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    nodes = d.get("nodes", {})
    lines = [f"RECIPE {path.stem}  version={d.get('version')} runMode={d.get('runMode')} nodes={len(nodes)}", ""]
    for name, n in nodes.items():
        p = n.get("parameters") or {}
        action = n.get("action")
        detail = ""
        if action in ("load", "LOAD_DATASET") or name.startswith("LOAD"):
            ds = p.get("dataset") or {}
            fields = p.get("fields") or []
            detail = f"source={ds.get('sourceObjectName') or ds.get('name') or ds.get('label')} type={ds.get('type')} fields={len(fields)}"
        elif action in ("join",):
            detail = f"joinType={p.get('joinType')} leftKeys={p.get('leftKeys')} rightKeys={p.get('rightKeys')}"
        elif action in ("filter",):
            detail = f"filter={_clip(p.get('filterExpressions') or p.get('filterExpression'), 250)}"
        elif action in ("formula", "computeRelative", "computeExpression"):
            detail = f"fields={[f.get('name') for f in p.get('fields', []) if isinstance(f, dict)][:10]}"
        elif action in ("save", "OUTPUT") or name.startswith("OUTPUT"):
            ds = p.get("dataset") or {}
            detail = f"output={ds.get('name') or ds.get('label')} fields={len(p.get('fields') or [])}"
        elif action in ("aggregate",):
            detail = f"groupings={p.get('groupings')} aggregations={len(p.get('aggregations') or [])}"
        lines.append(f"[{action}] {name} <- {','.join(n.get('sources') or []) or 'start'}  {detail}")
    return "\n".join(lines) + "\n"


def dataflow_digest(path: Path) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    # Retrieved .wdf files are a JSON list of JSON-encoded strings (one per definition version).
    if isinstance(d, list):
        parsed = [json.loads(x) if isinstance(x, str) else x for x in d]
        d = next((x for x in parsed if isinstance(x, dict)), {})
    nodes = d.get("nodes") if isinstance(d.get("nodes"), dict) else d
    lines = [f"DATAFLOW {path.stem} nodes={len(nodes)}", ""]
    for name, n in nodes.items():
        if not isinstance(n, dict):
            continue
        p = n.get("parameters") or {}
        action = n.get("action")
        detail = ""
        if action in ("sfdcDigest", "sobjectDigest", "digest"):
            detail = f"object={p.get('object')} fields={len(p.get('fields') or [])} incremental={p.get('incremental', False)}"
        elif action == "augment":
            detail = _clip(json.dumps(p), 250)
        elif action == "filter":
            detail = f"filter={_clip(p.get('filter') or p.get('saqlFilter'), 200)}"
        elif action in ("sfdcRegister", "register"):
            detail = f"dataset={p.get('name') or p.get('alias')}"
        elif action == "computeExpression":
            detail = f"fields={[f.get('name') for f in p.get('computedFields', [])][:10]}"
        elif action == "edgemart":
            detail = f"dataset={p.get('alias')}"
        lines.append(f"[{action}] {name} <- {','.join(n.get('sources') or []) or 'start'}  {detail}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- data model
USAGE_SOURCES = {
    "ApexClass": "apex", "ApexTrigger": "apex", "ApexPage": "vf", "ApexComponent": "vf",
    "Flow": "flow", "ValidationRule": "vr", "Layout": "layout", "FlexiPage": "page",
    "WorkflowRule": "wf", "WaveRecipe": "crma", "WaveDataflow": "crma", "WaveDashboard": "crma",
    "LightningComponentBundle": "lwc", "AuraDefinitionBundle": "lwc", "reportTypes": "reportType",
    "CustomField": "formula", "quickActions": "action",
}


# Optional qualifier ("Case." / "Account__r." / "inv.") then a custom field or relationship name.
FIELD_TOKEN = re.compile(r"(?:\b([A-Za-z][A-Za-z0-9_]*)\s*\.\s*)?\b([A-Za-z][A-Za-z0-9_]*__[cr])\b", re.I)


def _usage_object(item: dict) -> str:
    """The object a usage source belongs to, when its file is about one object."""
    if item.get("object"):
        return item["object"]
    if item["type"] == "Layout" and "-" in item["name"]:
        return item["name"].split("-", 1)[0]
    if item["type"] == "quickActions" and "." in item["name"]:
        return item["name"].split(".", 1)[0]
    return ""


def field_usage(items: list[dict], run_root: Path, out: Path) -> dict:
    """Count, per custom field, which metadata types mention it.

    Matching is case-insensitive (Apex and SOQL are), and X__r.<anything> counts as use of the lookup
    X__c. A mention is attributed to one object when that can be told: an object qualifier
    (Case.Status__c), a relationship qualifier whose lookup target is known (Account__r.Status__c),
    or the object the file belongs to (trigger, record-triggered flow, validation rule, layout).
    Otherwise it counts for every object that has a field of that name, so a shared name never makes
    a used field look unreferenced."""
    out.mkdir(parents=True, exist_ok=True)
    fields = [i for i in items if i["type"] == "CustomField" and i["name"].split(".")[-1].lower().endswith("__c")]
    # field api name (lower) -> {object (lower)}; object lower -> object spelling
    owners: dict[str, set[str]] = defaultdict(set)
    rel_targets: dict[str, set[str]] = defaultdict(set)     # x__r -> {referenced object (lower)}
    for f in fields:
        api = f["name"].split(".", 1)[1].lower()
        owners[api].add(f["object"].lower())
        for target in filter(None, (f.get("referenceTo") or "").split(",")):
            rel_targets[api[:-3] + "__r"].add(target.strip().lower())

    hits: dict[tuple[str, str], Counter] = defaultdict(Counter)
    seen_paths = set()
    for i in items:
        src = USAGE_SOURCES.get(i["type"])
        if not src or i["path"] in seen_paths:
            continue
        seen_paths.add(i["path"])
        own_obj = _usage_object(i).lower()
        self_key = (i["object"].lower(), i["name"].split(".", 1)[1].lower()) if src == "formula" else None
        p = run_root / i["path"]
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            found: set[tuple[str, str]] = set()
            pairs = FIELD_TOKEN.findall(text)
            # In Account__r.Status__c the qualifier is itself a use of the lookup Account__c.
            pairs += [("", q) for q, _ in pairs if q.lower().endswith("__r")]
            for qual, name in pairs:
                api = name.lower()
                if api.endswith("__r"):
                    api = api[:-3] + "__c"            # Lookup__r.Name uses the lookup Lookup__c
                candidates = owners.get(api)
                if not candidates:
                    continue
                q = qual.lower()
                if q in candidates:
                    objs = {q}
                elif q in rel_targets and rel_targets[q] & candidates:
                    objs = rel_targets[q] & candidates
                elif own_obj in candidates:
                    objs = {own_obj}
                else:
                    objs = candidates
                found |= {(o, api) for o in objs}
            for key in found - {self_key}:            # a field's own definition is not a use
                hits[key][src] += 1
    by_obj = defaultdict(list)
    for i in fields:
        by_obj[i["object"]].append(i)
    summary = {"customFields": len(fields), "unreferenced": 0, "withoutDescription": 0}
    for obj, flds in sorted(by_obj.items()):
        rows = []
        for f in sorted(flds, key=lambda x: x["name"]):
            api = f["name"].split(".", 1)[1]
            h = hits.get((obj.lower(), api.lower()), Counter())
            # Report types list every field of their objects, so they don't count as real use.
            total = sum(v for k, v in h.items() if k != "reportType")
            summary["unreferenced"] += total == 0
            summary["withoutDescription"] += not f.get("hasDescription")
            rows.append(SEP.join([api, f.get("fieldType") or "", f.get("referenceTo") or "",
                                  "Y" if f.get("hasDescription") else "N",
                                  " ".join(f"{k}:{v}" for k, v in sorted(h.items())) or "-", str(total)]))
        header = f"# Custom fields on {obj} ({len(flds)}) with where-used counts across retrieved metadata.\n" \
                 f"# Reports, list views and external integrations are NOT scanned: 0 refs means 'not used in metadata', not 'unused'.\n"
        (out / f"{obj}.txt").write_text(header + SEP.join(["Field", "Type", "ReferenceTo", "HasDescription", "UsedIn", "TotalRefs"])
                                        + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return summary


def sharing_table(items: list[dict], run_root: Path, out_file: Path) -> None:
    rule_counts = Counter()
    for i in items:
        if i["type"] == "SharingRules":
            text = (run_root / i["path"]).read_text(encoding="utf-8", errors="replace")
            rule_counts[i["name"]] = text.count("<sharingCriteriaRules>") + text.count("<sharingOwnerRules>")
    rows = []
    for i in sorted((i for i in items if i["type"] == "CustomObject"), key=lambda x: x["name"]):
        root = ET.parse(run_root / i["path"]).getroot()
        internal = _x(root, "sharingModel")
        if not internal:
            continue
        rows.append(SEP.join([i["name"], internal, _x(root, "externalSharingModel"), str(rule_counts.get(i["name"], 0))]))
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("# Org-wide defaults (internal / external) and sharing rule counts per object\n"
                        + SEP.join(["Object", "InternalOWD", "ExternalOWD", "SharingRules"]) + "\n" + "\n".join(rows) + "\n",
                        encoding="utf-8")


# --------------------------------------------------------------------------- entry
CRMA_DIGESTERS = {"WaveDashboard": dashboard_digest, "WaveRecipe": recipe_digest, "WaveDataflow": dataflow_digest}


def build_digests(items: list[dict], run_root: Path, index_dir: Path) -> dict:
    stats = {"workflowDigests": 0, "crmaDigests": 0, "crmaDigestErrors": []}
    wf_done = set()
    for i in items:
        p = run_root / i["path"]
        if i["type"] == "WorkflowRule" and i["object"] not in wf_done:
            wf_done.add(i["object"])
            out = index_dir / "workflows" / f"{i['object']}.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(workflow_digest(p), encoding="utf-8")
            stats["workflowDigests"] += 1
        if i["type"] in ("WorkflowRule",):
            i["digestPath"] = (index_dir / "workflows" / f"{i['object']}.txt").relative_to(run_root).as_posix()
        if i["type"] in CRMA_DIGESTERS:
            out = index_dir / "crma" / f"{i['type']}-{i['name']}.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                text = CRMA_DIGESTERS[i["type"]](p)
            except (ValueError, AttributeError, TypeError) as e:
                stats["crmaDigestErrors"].append(f"{i['id']}: {e}")
                continue
            out.write_text(text, encoding="utf-8")
            i["digestPath"] = out.relative_to(run_root).as_posix()
            i["digestTokens"] = estimate_tokens(text)
            stats["crmaDigests"] += 1
    stats["fieldUsage"] = field_usage(items, run_root, index_dir / "datamodel")
    sharing_table(items, run_root, index_dir / "orgfacts" / "sharing.txt")
    return stats
