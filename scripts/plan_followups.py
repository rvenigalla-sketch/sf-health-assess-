"""Stage 4b - Plan the bounded follow-up round. Deterministic.

Reviewers list `follow_up` leads: components outside their shard that look involved
(e.g. "this Bid flow updates Opportunity; Opportunity automation may re-fire"). This script
collects them from findings/raw/*.json, merges leads on the same component, ranks them by how
often and how severely they were raised, and writes at most followUp.maxAgents shards
(shards/followup-NN.json) for the review workflow, reviewed by followup-reviewer.

Usage:
  python plan_followups.py --run <run> [--config client.yml]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from common import Run, Timer, info, load_config, read_json, write_json

SEV_RANK = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    run = Run(Path(args.run))
    cap = cfg["followUp"]["maxAgents"]

    with Timer() as t:
        idx = {i["id"]: i for i in read_json(run.path("index", "inventory.json"), [])}
        leads: dict[str, dict] = defaultdict(lambda: {"reasons": [], "from": set(), "weight": 0})
        for raw in sorted(run.path("findings", "raw").glob("*.json")):
            if raw.stem.startswith("followup-"):
                continue                                  # one round only
            out = read_json(raw) or {}
            worst = max((SEV_RANK[f["severity"]] for f in out.get("findings", [])), default=1)
            for lead in out.get("follow_up", []):
                comp = lead.get("component")
                if comp not in idx:
                    continue                              # invented or unknown component: ignore
                entry = leads[comp]
                entry["reasons"].append(f"[{out.get('shard_id')}] {lead.get('reason', '')[:300]}")
                entry["from"].add(out.get("shard_id"))
                entry["weight"] += worst
        ranked = sorted(leads.items(), key=lambda kv: (-kv[1]["weight"], -len(kv[1]["from"]), kv[0]))
        chosen, dropped = ranked[:cap], ranked[cap:]

        for old in run.path("shards").glob("followup-*.json"):
            old.unlink()
        for n, (comp, lead) in enumerate(chosen, 1):
            item = idx[comp]
            files = [{"id": comp, "path": item.get("digestPath") or item["path"], "sourcePath": item["path"],
                      "lines": item["lines"], "tokens": item.get("digestTokens") or item["tokens"]}]
            obj = item.get("object")
            summary = idx.get(f"AutomationSummary:{obj}") if obj else None
            if summary and summary["id"] != comp:
                files.append({"id": summary["id"], "path": summary["path"], "sourcePath": summary["path"],
                              "lines": summary["lines"], "tokens": summary["tokens"]})
            write_json(run.path("shards", f"followup-{n:02d}.json"), {
                "shard_id": f"followup-{n:02d}", "area": "followup", "reviewer": "followup-reviewer",
                "planner": "followup", "components": [f["id"] for f in files],
                "files": files, "context_components": [], "context": {},
                "questions": lead["reasons"][:6], "raisedBy": sorted(lead["from"]), "evidence": [],
                "sourceTokens": sum(f["tokens"] for f in files),
            })

    run.log_step("plan-followups", "ok", t.seconds, leads=len(leads), shards=len(chosen), dropped=len(dropped))
    info(f"FOLLOW-UP: {len(leads)} lead(s), {len(chosen)} shard(s) planned"
         + (f"; {len(dropped)} lower-ranked lead(s) not followed: {', '.join(c for c, _ in dropped[:10])}" if dropped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
