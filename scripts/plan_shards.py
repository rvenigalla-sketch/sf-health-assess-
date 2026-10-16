"""Stage 3 - Shard planner. Deterministic, no LLM.

Splits the org into review shards that each fit a token budget, so no reviewer sees more than
it can read carefully. Each shard file lists the components to review (with paths), the tool
evidence for exactly those files, and small context (related components, automation summary).

Writes <run>/shards/<shard_id>.json and <run>/shards/plan.json (counts + token/agent estimate).

Usage:
  python plan_shards.py --run <run folder> [--areas apex,flow,automation] [--config client.yml]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from common import Run, Timer, info, load_config, read_json, write_json

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


def pack(groups: list[list[dict]], budget: int, cost) -> list[list[dict]]:
    """First-fit decreasing: keep each group together; split a group only if it alone exceeds budget."""
    units: list[list[dict]] = []
    for g in groups:
        if sum(cost(i) for i in g) <= budget:
            units.append(g)
            continue
        chunk, size = [], 0
        for i in g:                      # g is in BFS order, so neighbours stay together
            if chunk and size + cost(i) > budget:
                units.append(chunk)
                chunk, size = [], 0
            chunk.append(i)
            size += cost(i)
        if chunk:
            units.append(chunk)
    units.sort(key=lambda u: -sum(cost(i) for i in u))
    bins: list[tuple[int, list[dict]]] = []
    for u in units:
        c = sum(cost(i) for i in u)
        for n, (size, content) in enumerate(bins):
            if size + c <= budget:
                bins[n] = (size + c, content + u)
                break
        else:
            bins.append((c, list(u)))
    return [content for _, content in bins]


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
    code = [i for i in apex if not i.get("isTest")]
    tests = [i for i in apex if i.get("isTest")]
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


PLANNERS = {"apex": plan_apex}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--areas", default=",".join(PLANNERS))
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    run = Run(Path(args.run))
    budget = cfg["shards"]["tokenBudget"]

    with Timer() as t:
        items = read_json(run.path("index", "inventory.json"), [])
        automation = read_json(run.path("index", "automation_map.json"), {})
        ev_by_path = load_evidence(run)
        areas = [a.strip() for a in args.areas.split(",") if a.strip()]
        unknown = [a for a in areas if a not in PLANNERS]
        if unknown:
            info(f"Unknown areas {unknown}; available: {sorted(PLANNERS)}")
            return 2

        for old in run.path("shards").glob("*.json"):
            old.unlink()
        shards = [s for a in areas for s in PLANNERS[a](items, automation, ev_by_path, budget)]
        for s in shards:
            s["sourceTokens"] = sum(f["tokens"] for f in s["files"])
            write_json(run.path("shards", f"{s['shard_id']}.json"), s)

        agents = len(shards)
        est_input = sum((s["sourceTokens"] + AGENT_OVERHEAD_TOKENS) * TURN_FACTOR for s in shards)
        plan = {
            "areas": areas, "tokenBudget": budget, "shards": agents,
            "byArea": {a: sum(1 for s in shards if s["area"] == a) for a in areas},
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

    run.log_step("plan-shards", "ok", t.seconds, **{k: plan[k] for k in ("shards", "byArea", "overBudget")})
    info(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
