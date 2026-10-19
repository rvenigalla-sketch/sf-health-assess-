"""Stage 3 - Shard planner. Deterministic, no LLM.

Splits the org into review shards that each fit a token budget, so no reviewer sees more than
it can read carefully. Each shard file lists the components to review (with paths), the tool
evidence for exactly those files, and small context (related components, automation summary).

Writes <run>/shards/<shard_id>.json and <run>/shards/plan.json (counts + token/agent estimate).

Incremental (--baseline): only components that changed since the previous snapshot (content hash),
plus components one call away from a change, are planned for review. Findings from the previous
run on untouched components are carried forward with their verifier verdicts
(findings/raw/carried.json); validate_findings.py re-checks their quotes against the new files.

Usage:
  python plan_shards.py --run <run folder> [--areas apex,flow,automation] [--config client.yml]
                        [--baseline auto|<previous run folder>|none]
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import library
from common import Run, Timer, earlier_run, info, load_run_config, read_json, write_json
from score import content_fingerprint

# Incremental runs: inventory ids to review again. None = review everything (full run).
DIRTY: set[str] | None = None


def fresh(i: dict) -> bool:
    return DIRTY is None or i["id"] in DIRTY

# Per-agent overhead (system prompt, rubric, shard file, tool calls) and re-read factor across
# turns, used only for the pre-launch estimate. Calibrated from run logs over time.
AGENT_OVERHEAD_TOKENS = 15000
TURN_FACTOR = 2.0
OUTPUT_TOKENS_PER_AGENT = 4000


def load_evidence(run: Run) -> dict[str, list[dict]]:
    by_path: dict[str, list[dict]] = defaultdict(list)
    p = run.path("index", "evidence.jsonl")
    if p.exists():
        for line in p.open(encoding="utf-8"):
            e = json.loads(line)
            by_path[e["path"]].append(e)
    return by_path


def evidence_for(items: list[dict], ev_by_path: dict) -> list[dict]:
    rows = []
    for i in items:
        for path in (i["path"], i["path"] + "-meta.xml"):
            rows += [{k: e[k] for k in ("evidence_id", "rule", "severity", "line", "message", "path")}
                     for e in ev_by_path.get(path, [])]
        if i["type"] in ("LightningComponentBundle", "AuraDefinitionBundle"):
            rows += [{k: e[k] for k in ("evidence_id", "rule", "severity", "line", "message", "path")}
                     for p, es in ev_by_path.items() if p.startswith(i["path"] + "/") for e in es]
    return rows


MAX_ITEMS = 30   # components per shard; overridden from config (shards.maxComponents)


def pack(groups: list[list], budget: int, cost, max_items: int | None = None, count=None) -> list[list]:
    """Pack groups into as few shards as possible, then balance them.

    Each group stays together unless it alone exceeds the token budget or the item cap.
    `count(x)` is how many components an element represents (default 1).
    Step 1 (first-fit decreasing) finds the minimum shard count; step 2 re-deals units
    largest-first into the least-loaded shard so no shard is a tiny leftover.
    """
    max_items = max_items or MAX_ITEMS
    count = count or (lambda x: 1)
    units: list[list] = []
    for g in groups:
        if sum(cost(i) for i in g) <= budget and sum(count(i) for i in g) <= max_items:
            units.append(g)
            continue
        chunk, size, n = [], 0, 0
        for i in g:                      # g is in BFS order, so neighbours stay together
            if chunk and (size + cost(i) > budget or n + count(i) > max_items):
                units.append(chunk)
                chunk, size, n = [], 0, 0
            chunk.append(i)
            size += cost(i)
            n += count(i)
        if chunk:
            units.append(chunk)
    ucost = lambda u: sum(cost(i) for i in u)
    ucount = lambda u: sum(count(i) for i in u)
    units.sort(key=lambda u: (-ucost(u), -ucount(u)))

    bins: list[list] = []
    for u in units:                      # step 1: first-fit decreasing
        for b in bins:
            if ucost(b) + ucost(u) <= budget and ucount(b) + ucount(u) <= max_items:
                b.extend(u)
                break
        else:
            bins.append(list(u))

    balanced: list[list] = [[] for _ in bins]   # step 2: same shard count, balanced load
    for u in units:
        fits = [b for b in balanced if ucost(b) + ucost(u) <= budget and ucount(b) + ucount(u) <= max_items]
        if not fits:
            return [b for b in bins if b]
        min(fits, key=lambda b: (ucost(b) / budget + ucount(b) / max_items)).extend(u)
    return [b for b in balanced if b]


def clusters(items: list[dict]) -> list[list[dict]]:
    """Connected groups by class references (trigger -> handler -> service), in BFS order."""
    by_name = {i["name"]: i for i in items}
    adj: dict[str, set[str]] = defaultdict(set)
    for i in items:
        for ref in i.get("refs", {}).get("classes", []):
            if ref in by_name:
                adj[i["name"]].add(ref)
                adj[ref].add(i["name"])
    seen, out = set(), []
    # Start from triggers so each trigger's chain is grouped from its entry point.
    for start in sorted(by_name, key=lambda n: (by_name[n]["type"] != "ApexTrigger", n)):
        if start in seen:
            continue
        group, queue = [], [start]
        seen.add(start)
        while queue:
            cur = queue.pop(0)
            group.append(by_name[cur])
            for nxt in sorted(adj[cur]):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        out.append(group)
    return out


def component_entry(i: dict, path_key: str = "path") -> dict:
    return {"id": i["id"], "path": i[path_key], "lines": i["lines"], "tokens": i["tokens"]}


# --------------------------------------------------------------------------- areas
def plan_apex(items, automation, ev_by_path, budget) -> list[dict]:
    apex = [i for i in items if i["type"] in ("ApexClass", "ApexTrigger")
            and not i.get("managedTool")]          # dlrs triggers are generated code
    code = [i for i in apex if not i.get("isTest") and fresh(i)]
    tests = [i for i in apex if i.get("isTest") and fresh(i)]
    by_name = {i["name"]: i for i in apex}
    cost = lambda i: i["tokens"]

    shards = []
    for n, group in enumerate(pack(clusters(code), budget, cost), 1):
        names = {i["name"] for i in group}
        context_ids = sorted({by_name[r]["id"] for i in group for r in i.get("refs", {}).get("classes", [])
                              if r in by_name and r not in names and not by_name[r].get("isTest")})
        objects = sorted({i["object"] for i in group if i["type"] == "ApexTrigger"})
        shards.append({
            "shard_id": f"apex-{n:02d}", "area": "apex", "reviewer": "apex-reviewer",
            "components": [i["id"] for i in group],
            "files": [component_entry(i) for i in group],
            "context_components": context_ids,
            "context": {
                "relatedClasses": [{"id": c, "path": next(i["path"] for i in apex if i["id"] == c)} for c in context_ids],
                "triggerObjects": {o: summarize_object(automation.get(o)) for o in objects},
            },
            "evidence": evidence_for(group, ev_by_path),
        })
    # Tests are reviewed for quality (assertions, SeeAllData, coverage-only tests) in their own shards.
    for n, group in enumerate(pack([[t] for t in tests], budget, cost), 1):
        shards.append({
            "shard_id": f"apex-tests-{n:02d}", "area": "apex", "reviewer": "apex-reviewer",
            "focus": "test-quality",
            "components": [i["id"] for i in group],
            "files": [component_entry(i) for i in group],
            "context_components": [], "context": {},
            "evidence": evidence_for(group, ev_by_path),
        })
    return shards


def summarize_object(a: dict | None) -> dict:
    """Small, name-only view of an object's automation for context."""
    if not a:
        return {}
    active = lambda xs, key="status", val="Active": [x["name"] for x in xs if x.get(key) == val]
    return {
        "triggers": [t["name"] for t in a["triggers"] if t["active"]],
        "flowsBeforeSave": active(a["flowsBeforeSave"]),
        "flowsAfterSave": active(a["flowsAfterSave"]),
        "processBuilders": active(a["processBuilders"]),
        "workflowRules": [w["name"] for w in a["workflowRules"] if w["active"]],
        "fieldOverlaps": a.get("fieldOverlaps", {}),
    }


def _by_id(items):
    return {i["id"]: i for i in items}


def _read_entry(i: dict) -> dict:
    """File a reviewer reads for this component: its digest when one exists, else the source."""
    path = i.get("digestPath") or i["path"]
    return {"id": i["id"], "path": path, "sourcePath": i["path"], "lines": i["lines"],
            "tokens": i.get("digestTokens") or i["tokens"]}


def _cost(i: dict) -> int:
    return i.get("digestTokens") or i["tokens"]


def _orgfact(items, *names) -> list[dict]:
    idx = _by_id(items)
    return [idx[f"OrgFact:{n}"] for n in names if f"OrgFact:{n}" in idx]


def plan_automation(items, automation, ev_by_path, budget) -> list[dict]:
    """Objects with 2+ active automations. Each object's automation stays together (summary in
    order of execution + triggers + flow digests + workflow digest + validation rules); small
    objects share a shard, a very large object is split with its summary repeated in every part."""
    idx = _by_id(items)
    flows = {i["name"]: i for i in items if i["type"] == "Flow"}
    whole_objects, split_shards = [], []
    for obj, a in sorted(automation.items(), key=lambda kv: -kv[1]["activeAutomationCount"]):
        if a["activeAutomationCount"] < 2:
            continue
        summary = idx.get(f"AutomationSummary:{obj}")
        members = [idx[f"ApexTrigger:{t['name']}"] for t in a["triggers"] if t["active"] and not t["managedTool"]]
        for k in ("flowsBeforeSave", "flowsAfterSave", "processBuilders"):
            members += [flows[f["name"]] for f in a[k] if f["status"] == "Active" and f["name"] in flows]
        wf = [i for i in items if i["type"] == "WorkflowRule" and i["object"] == obj and i["active"]]
        members += wf[:1]                       # one digest file covers all of the object's rules
        vrs = [i for i in items if i["type"] == "ValidationRule" and i["object"] == obj and i["active"]]
        fixed = ([summary] if summary else [])
        extra_ids = [w["id"] for w in wf[1:]]
        group = fixed + members + vrs
        if not any(fresh(i) for i in group) and not any(fresh({"id": x}) for x in extra_ids):
            continue                            # incremental: nothing on this object changed
        if sum(_cost(i) for i in group) <= budget and len(group) <= MAX_ITEMS:
            whole_objects.append((obj, group, extra_ids))
            continue
        room = max(budget - sum(_cost(f) for f in fixed), budget // 3)
        parts = pack([[m] for m in members + vrs], room, _cost, MAX_ITEMS - len(fixed))
        for n, part in enumerate(parts, 1):
            split_shards.append((f"automation-{obj}-{n}", [obj], fixed + part, extra_ids if n == 1 else [],
                                 f"{n} of {len(parts)}"))

    shards = []
    # Keep each small object whole and pack several objects per shard.
    for n, bundle in enumerate(pack([[o] for o in whole_objects], budget, lambda o: sum(_cost(i) for i in o[1]),
                                    MAX_ITEMS, count=lambda o: len(o[1])), 1):
        objs = [o for o, _, _ in bundle]
        group = [i for _, g, _ in bundle for i in g]
        extra = [x for _, _, e in bundle for x in e]
        split_shards.append((f"automation-{n:02d}", objs, group, extra, "complete"))

    for shard_id, objs, group, extra, part in split_shards:
        handlers = sorted({f"ApexClass:{c}" for t in group if t["type"] == "ApexTrigger"
                           for c in t.get("refs", {}).get("classes", []) if f"ApexClass:{c}" in idx})
        shards.append({
            "shard_id": shard_id, "area": "automation", "reviewer": "automation-reviewer", "objects": objs,
            "components": [i["id"] for i in group] + extra,
            "files": [_read_entry(i) for i in group],
            "context_components": handlers,
            "context": {"triggerHandlers": [{"id": h, "path": idx[h]["path"]} for h in handlers], "part": part},
            "evidence": [],
        })
    return shards


def plan_flow(items, automation, ev_by_path, budget) -> list[dict]:
    """Internal quality of every ACTIVE flow (digests), batched by object."""
    active = sorted((i for i in items if i["type"] == "Flow" and i.get("status") == "Active" and fresh(i)),
                    key=lambda i: (i["object"] or "~", i["name"]))
    errors = _orgfact(items, "runtime_errors")
    groups = defaultdict(list)
    for f in active:
        groups[f["object"] or f["kind"]].append(f)
    # Every flow shard also carries the runtime-errors fact; a huge fact file must not squeeze the
    # room left for flows to nothing (one flow per shard), so the flow budget has a floor.
    room = max(budget - sum(_cost(e) for e in errors), budget // 2)
    shards = []
    for n, group in enumerate(pack(list(groups.values()), room, _cost), 1):
        group = sorted(group, key=lambda i: (i["object"], i["name"]))
        shards.append({
            "shard_id": f"flow-{n:02d}", "area": "flow", "reviewer": "flow-reviewer",
            "components": [i["id"] for i in group] + [e["id"] for e in errors],
            "files": [_read_entry(i) for i in group] + [_read_entry(e) for e in errors],
            "context_components": [], "context": {"note": "Each flow file is a digest; raw XML is at sourcePath."},
            "evidence": [e for e in evidence_for(group, ev_by_path)
                         if e["rule"] not in ("MissingFaultHandler",)],   # already marked NO-FAULT-PATH in digests
        })
    return shards


def plan_security(items, automation, ev_by_path, budget) -> list[dict]:
    facts = _orgfact(items, "healthcheck", "permissions", "privileged_users", "underused_permsets",
                     "licenses_users", "connected_apps", "sharing")
    # The facts are read together (permissions vs privileged users...): any change re-reviews all.
    if not facts or not any(fresh(f) for f in facts):
        return []
    return [{
        "shard_id": f"security-{n:02d}", "area": "security", "reviewer": "security-reviewer",
        "components": [i["id"] for i in part], "files": [_read_entry(i) for i in part],
        "context_components": [], "context": {}, "evidence": [],
    } for n, part in enumerate(pack([[f] for f in facts], budget, _cost), 1)]


def plan_datamodel(items, automation, ev_by_path, budget) -> list[dict]:
    tables = sorted((i for i in items if i["type"] == "FieldUsage" and fresh(i)), key=lambda i: -i["tokens"])
    return [{
        "shard_id": f"datamodel-{n:02d}", "area": "datamodel", "reviewer": "datamodel-reviewer",
        "components": [i["id"] for i in part], "files": [_read_entry(i) for i in part],
        "context_components": [], "context": {}, "evidence": [],
    } for n, part in enumerate(pack([[t] for t in tables], budget, _cost), 1)]


def plan_crma(items, automation, ev_by_path, budget) -> list[dict]:
    assets = sorted((i for i in items if i["type"] in ("WaveDashboard", "WaveRecipe", "WaveDataflow")
                     and i.get("digestPath") and fresh(i)),
                    key=lambda i: (i["type"], i["name"]))
    by_type = defaultdict(list)
    for a in assets:
        by_type[a["type"]].append(a)
    shards = []
    # Recipes and dataflows together (data prep); dashboards separately.
    for label, group in (("prep", by_type["WaveRecipe"] + by_type["WaveDataflow"]), ("dash", by_type["WaveDashboard"])):
        for n, part in enumerate(pack([[a] for a in group], budget, _cost), 1):
            shards.append({
                "shard_id": f"crma-{label}-{n:02d}", "area": "crma", "reviewer": "crma-reviewer",
                "components": [i["id"] for i in part], "files": [_read_entry(i) for i in part],
                "context_components": [], "context": {"note": "Files are digests; raw JSON is at sourcePath."},
                "evidence": [],
            })
    return shards


def plan_platform(items, automation, ev_by_path, budget) -> list[dict]:
    """Integrations and release readiness: one shard of org facts plus integration endpoints."""
    facts = _orgfact(items, "release_updates", "legacy_api", "api_versions", "unused_async_apex", "limits", "runtime_errors")
    endpoints = [i for i in items if i["type"] in ("RemoteSiteSetting", "NamedCredential", "authproviders",
                                                   "ConnectedApp", "externalClientApps", "certs")]
    group = facts + endpoints
    if not group or not any(fresh(g) for g in group):
        return []
    return [{
        "shard_id": f"platform-{n:02d}", "area": "release", "reviewer": "platform-reviewer",
        "components": [i["id"] for i in part], "files": [_read_entry(i) for i in part],
        "context_components": [], "context": {}, "evidence": [],
    } for n, part in enumerate(pack([[g] for g in group], budget, _cost), 1)]


PLANNERS = {
    "automation": plan_automation, "apex": plan_apex, "flow": plan_flow, "security": plan_security,
    "datamodel": plan_datamodel, "crma": plan_crma, "platform": plan_platform,
}


# Changing one of these marks its whole object as changed.
OBJECT_LEVEL_TYPES = {"CustomObject", "CustomField", "ValidationRule", "RecordType", "BusinessProcess",
                      "DuplicateRule", "MatchingRules", "SharingRules", "AssignmentRules", "AutoResponseRules",
                      "EscalationRules", "ApprovalProcess"}
# Types indexed by file name only (object ""): the object is the name's prefix
# (Account.My_Rule.duplicateRule-meta.xml, Account.sharingRules-meta.xml, Case.assignmentRules-meta.xml).
NAME_PREFIX_TYPES = {"DuplicateRule", "MatchingRules", "SharingRules", "AssignmentRules", "AutoResponseRules",
                     "EscalationRules", "ApprovalProcess", "Workflow"}
FLOW_NS = "{http://soap.sforce.com/2006/04/metadata}"


def object_of(i: dict) -> str:
    if i.get("object"):
        return i["object"]
    if i.get("type") in NAME_PREFIX_TYPES:
        return (i.get("name") or "").split(".")[0]
    return ""


def subflow_refs(root: Path | None, i: dict) -> set[str]:
    """Flow names this flow calls as subflows (<subflows><flowName>), read from its XML. The index
    records class/object references only, so subflow calls are derived here."""
    if root is None or i.get("type") != "Flow" or not i.get("path"):
        return set()
    if "subflows" in i.get("refs", {}):
        return set(i["refs"]["subflows"])
    try:
        tree = ET.parse(root / i["path"]).getroot()
    except (OSError, ET.ParseError):
        return set()
    return {el.findtext(FLOW_NS + "flowName").strip() for el in tree.iter(FLOW_NS + "subflows")
            if (el.findtext(FLOW_NS + "flowName") or "").strip()}


def changed_components(items: list[dict], prev_items: list[dict], root: Path | None = None,
                       prev_root: Path | None = None) -> set[str] | None:
    """Ids whose content changed (or are new), plus one hop through Apex/flow class references and
    flow -> subflow calls in both directions: callers of a changed class / subflow and what a changed
    component calls. References are the union of the previous and current snapshot, so removing a
    call still marks the old callee. None when the previous run has no hashes."""
    prev = {i["id"]: i.get("sha") for i in prev_items}
    if not any(prev.values()):
        return None
    prev_by_id = {p["id"]: p for p in prev_items}
    cur_ids = {i["id"] for i in items}
    changed = {i["id"] for i in items if not i.get("sha") or prev.get(i["id"]) != i["sha"]}
    deleted = [p for p in prev_items if p["id"] not in cur_ids]
    # Deleted components can change behaviour of what remains (e.g. a removed validation rule).
    changed_objects = {object_of(p) for p in deleted} - {"", None}
    class_ids = {i["name"]: i["id"] for i in items if i["type"] == "ApexClass"}
    flow_ids = {i["name"]: i["id"] for i in items if i["type"] == "Flow"}
    changed_classes = {i["name"] for i in items if i["id"] in changed and i["type"] == "ApexClass"} \
        | {p["name"] for p in deleted if p.get("type") == "ApexClass"}
    changed_flows = {i["name"] for i in items if i["id"] in changed and i["type"] == "Flow"} \
        | {p["name"] for p in deleted if p.get("type") == "Flow"}
    # A changed object, field, validation rule, record type, duplicate / sharing rule... marks the whole
    # object as changed, so every flow, class and trigger that uses that object is reviewed again.
    changed_objects |= {object_of(i) for i in items if i["id"] in changed
                        and i["type"] in OBJECT_LEVEL_TYPES} - {"", None}

    def refs(i: dict) -> tuple[set[str], set[str], set[str]]:
        p = prev_by_id.get(i["id"], {})
        cur_r, prev_r = i.get("refs", {}), p.get("refs", {})
        return (set(cur_r.get("classes", [])) | set(prev_r.get("classes", [])),
                set(cur_r.get("objects", [])) | set(prev_r.get("objects", [])),
                subflow_refs(root, i) | (subflow_refs(prev_root, p) if p else set()))

    dirty = set(changed)
    for i in items:
        classes, objects, subflows = refs(i)
        if classes & changed_classes or subflows & changed_flows:
            dirty.add(i["id"])                     # calls a changed class / subflow
        if i["id"] in changed:                     # what a changed component calls (or used to call)
            dirty.update(class_ids[r] for r in classes if r in class_ids)
            dirty.update(flow_ids[r] for r in subflows if r in flow_ids)
        if object_of(i) in changed_objects or objects & changed_objects:
            dirty.add(i["id"])
    return dirty


def carry_forward(run: Run, prev_root: Path, items: list[dict], dirty: set[str],
                  planned: set[str] | None = None) -> dict:
    """Previous validated findings whose components are all unchanged -> findings/raw/carried.json,
    with their verdicts copied to findings/verdicts/carried__<n>.json.
    A finding on a component that is in a planned shard is not carried: the reviewer will report it
    again if it still holds, and carrying it too would duplicate it."""
    planned = planned or set()
    ids = {i["id"] for i in items}
    by_path = {p: i["id"] for i in items for p in (i["path"], i.get("digestPath")) if p}
    prev_path = prev_root / "findings" / "validated.jsonl"
    prev = [json.loads(l) for l in prev_path.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if prev_path.exists() else []
    prev_verdicts = {v["finding_id"]: v for v in (read_json(p) for p in (prev_root / "findings" / "verdicts").glob("*.json"))
                     if isinstance(v, dict) and "finding_id" in v}
    for old in run.path("findings", "verdicts").glob("carried__*.json"):
        old.unlink()
    carried, dropped = [], 0
    for f in prev:
        if f.get("shard_id") == "rules":
            continue                                # rule checks are recomputed every run
        involved = {f["component"], *f.get("related_components", []),
                    *(by_path.get(e["path"], "") for e in f.get("evidence", []))} - {""}
        if f["component"] not in ids or involved & dirty or f["component"] in planned:
            dropped += 1
            continue
        keep = {k: v for k, v in f.items() if k not in ("shard_id", "finding_id", "verification")}
        keep["carried_from"] = {"run": prev_root.name, "finding_id": f["finding_id"]}
        carried.append(keep)
        v = prev_verdicts.get(f["finding_id"])
        # A previous verdict is reused only if it was given for this finding's content (when it says).
        if v and (not v.get("fingerprint") or v["fingerprint"] == content_fingerprint(f)):
            # validate_findings.py numbers carried findings carried#1, carried#2, ... in this order
            write_json(run.path("findings", "verdicts", f"carried__{len(carried)}.json"),
                       {**v, "finding_id": f"carried#{len(carried)}", "fingerprint": content_fingerprint(keep)})
    write_json(run.path("findings", "raw", "carried.json"),
               {"shard_id": "carried", "findings": carried,
                "coverage": {"components_reviewed": [], "skipped": []}, "follow_up": []})
    return {"carried": len(carried), "notCarried": dropped}


# Outputs of the review / verify / score stages. A new plan makes all of them stale: finding ids are
# positional ({shard}#{n}), so a leftover verdict would attach to a different new finding and a
# leftover raw file would make a failed shard look reviewed.
DERIVED_FILES = ("validated.jsonl", "rejected.jsonl", "coverage.json", "findings.json", "refuted.jsonl",
                 "baseline_diff.json")
KEEP_RAW = {"rules.json"}          # rule_checks.py runs before planning; carried.json is rewritten below


def clear_stale_outputs(run: Run) -> None:
    for old in run.path("shards").glob("*.json"):
        old.unlink()
    for old in run.path("findings", "raw").glob("*.json"):
        if old.name not in KEEP_RAW:
            old.unlink()
    for old in run.path("findings", "verdicts").glob("*.json"):
        if not old.name.startswith("carried__"):     # rewritten (or removed) by carry_forward / main
            old.unlink()
    for name in DERIVED_FILES:
        run.path("findings", name).unlink(missing_ok=True)


CONTEXT_FILE_KEYS = ("relatedClasses", "triggerHandlers")


def reviewer_context(run: Run, cfg: dict, shards: list[dict]) -> None:
    """Tell each reviewer what scripts already reported and which patterns consultants keep rejecting."""
    rules = (read_json(run.path("findings", "raw", "rules.json"), {}) or {}).get("findings", [])
    warn = (cfg.get("library") or {}).get("warnReviewers", False)
    fps = {area: library.false_positives(cfg, area) for area in {s["area"] for s in shards}} if warn else {}
    for s in shards:
        mine = set(s["components"]) | set(s.get("context_components", []))
        reported = [{"title": f["title"], "components": sorted(mine & {f["component"], *f["related_components"]})}
                    for f in rules if mine & {f["component"], *f["related_components"]}]
        if reported:
            s["context"]["alreadyReported"] = reported
        if fps.get(s["area"]):
            s["context"]["knownFalsePositives"] = fps[s["area"]]


def main() -> int:
    global MAX_ITEMS, DIRTY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--areas", default=",".join(PLANNERS))
    ap.add_argument("--config")
    ap.add_argument("--baseline", default="none",
                    help="auto = newest earlier run of the same client; or a run folder; none = full review")
    args = ap.parse_args()
    run = Run(Path(args.run))
    cfg = load_run_config(run, args.config)
    budget = cfg["shards"]["tokenBudget"]
    MAX_ITEMS = cfg["shards"].get("maxComponents", MAX_ITEMS)

    with Timer() as t:
        items = read_json(run.path("index", "inventory.json"), [])
        automation = read_json(run.path("index", "automation_map.json"), {})
        ev_by_path = load_evidence(run)
        areas = [a.strip() for a in args.areas.split(",") if a.strip()]
        unknown = [a for a in areas if a not in PLANNERS]
        if unknown:
            info(f"Unknown areas {unknown}; available: {sorted(PLANNERS)}")
            return 2

        clear_stale_outputs(run)
        # Full plan first: it defines which areas this run assesses (incremental or not).
        DIRTY = None
        full = [{**s, "planner": a} for a in areas for s in PLANNERS[a](items, automation, ev_by_path, budget)]

        prev_root = None
        if args.baseline != "none" and (cfg.get("incremental") or {}).get("enabled", True):
            prev_root = earlier_run(run) if args.baseline == "auto" else Path(args.baseline)
        incremental = None
        run.path("findings", "raw").mkdir(parents=True, exist_ok=True)
        if prev_root and (prev_root / "index" / "inventory.json").exists():
            DIRTY = changed_components(items, read_json(prev_root / "index" / "inventory.json", []),
                                       run.root, prev_root)
            if DIRTY is None:
                info(f"INCREMENTAL: {prev_root.name} has no content hashes; doing a full review")
        if DIRTY is not None:
            shards = [{**s, "planner": a} for a in areas for s in PLANNERS[a](items, automation, ev_by_path, budget)]
            planned = {c for s in shards for c in s["components"]}
            incremental = {"baseline": prev_root.name, "changedComponents": len(DIRTY),
                           "fullPlanShards": len(full), **carry_forward(run, prev_root, items, DIRTY, planned)}
        else:
            shards = full
            run.path("findings", "raw", "carried.json").unlink(missing_ok=True)
            for old in run.path("findings", "verdicts").glob("carried__*.json"):
                old.unlink()

        reviewer_context(run, cfg, shards)
        tokens_by_path = {i["path"]: i.get("tokens", 0) for i in items}
        for s in shards:
            # Reviewers also read the context files (related classes, trigger handlers).
            ctx = {c["path"] for key in CONTEXT_FILE_KEYS for c in s.get("context", {}).get(key, [])}
            ctx -= {f["path"] for f in s["files"]} | {f.get("sourcePath") for f in s["files"]}
            s["sourceTokens"] = sum(f["tokens"] for f in s["files"]) + sum(tokens_by_path.get(p, 0) for p in ctx)
            s["evidence"] = [{**e, "message": (e.get("message") or "")[:300]} for e in s["evidence"]]
            write_json(run.path("shards", f"{s['shard_id']}.json"), s)

        agents = len(shards)
        est_input = sum((s["sourceTokens"] + AGENT_OVERHEAD_TOKENS) * TURN_FACTOR for s in shards)
        plan = {
            "areas": areas, "tokenBudget": budget, "shards": agents,
            "byArea": {a: sum(1 for s in shards if s.get("planner") == a) for a in areas},
            # Areas this run assesses: from the full plan, so an unchanged area still counts (its findings carry forward).
            "assessedByArea": {a: sum(1 for s in full if s.get("planner") == a) for a in areas},
            "incremental": incremental,
            "largestShardTokens": max((s["sourceTokens"] for s in shards), default=0),
            "overBudget": [s["shard_id"] for s in shards if s["sourceTokens"] > budget],
            "estimate": {
                "reviewAgents": agents,
                "inputTokens": int(est_input),
                "outputTokens": agents * OUTPUT_TOKENS_PER_AGENT,
                "note": "Review stage only; verify/synthesis add ~10-20%. Most input is cache reads.",
            },
            "withinAgentCap": agents <= cfg["shards"]["maxAgentsPerRun"],
        }
        write_json(run.path("shards", "plan.json"), plan)

    run.log_step("plan-shards", "ok", t.seconds, **{k: plan[k] for k in ("shards", "byArea", "overBudget", "incremental")})
    info(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
