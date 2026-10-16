"""Print the small JSON `args` object for an sf-health workflow, and find workflow journals.

The orchestrator passes this verbatim as the Workflow tool's `args`, so it never has to read shard
or finding contents itself.

Usage:
  python workflow_args.py review    --run <run> [--config c.yml]   # first-round shards
  python workflow_args.py followup  --run <run> [--config c.yml]   # follow-up shards
  python workflow_args.py verify    --run <run> [--config c.yml]   # validated High/Critical findings
  python workflow_args.py synthesize --run <run> [--config c.yml]
  python workflow_args.py journal   --run-id wf_xxx                # path of that workflow's journal.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import PLUGIN_ROOT, Run, load_config, read_json

MODEL_ALIASES = {"sonnet", "opus", "haiku", "fable"}


def model_arg(cfg: dict, role: str) -> dict:
    m = (cfg.get("models") or {}).get(role)
    return {"model": m} if m in MODEL_ALIASES else {}


def shard_list(run: Run, followup: bool) -> list[dict]:
    out = []
    for p in sorted(run.path("shards").glob("*.json")):
        s = read_json(p)
        if not s or "shard_id" not in s:
            continue
        if followup != s["shard_id"].startswith("followup-"):
            continue
        out.append({"shard_id": s["shard_id"], "area": s.get("area"), "reviewer": s["reviewer"]})
    return out


def find_journal(run_id: str) -> Path | None:
    root = Path.home() / ".claude"
    hits = [p for p in root.rglob("journal.jsonl") if run_id in str(p)]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["review", "followup", "verify", "synthesize", "journal"])
    ap.add_argument("--run")
    ap.add_argument("--config")
    ap.add_argument("--run-id")
    args = ap.parse_args()

    if args.kind == "journal":
        j = find_journal(args.run_id or "")
        print(json.dumps({"journal": str(j) if j else None}))
        return 0 if j else 1

    cfg = load_config(args.config)
    run = Run(Path(args.run))
    base = {"run": str(run.root).replace("\\", "/"), "pluginRoot": str(PLUGIN_ROOT).replace("\\", "/"),
            "useAgentTypes": bool((cfg.get("models") or {}).get("useAgentTypes"))}
    if args.kind in ("review", "followup"):
        out = {**base, "shards": shard_list(run, args.kind == "followup"), **model_arg(cfg, "reviewer")}
    elif args.kind == "verify":
        p = run.path("findings", "validated.jsonl")
        rows = [json.loads(l) for l in p.open(encoding="utf-8")] if p.exists() else []
        # Critical first, then High; within a severity, model-only observations before tool-confirmed ones.
        # Anything past verify.maxFindings stays "unverified" and is flagged in the consultant sheet.
        rank = lambda f: (f["severity"] != "Critical", f.get("basis") == "tool-confirmed")
        serious = sorted((f for f in rows if f["severity"] in ("Critical", "High")), key=rank)
        cap = int((cfg.get("verify") or {}).get("maxFindings", 60))
        if len(serious) > cap:
            print(f"VERIFY: {len(serious)} High/Critical findings, verifying the first {cap}; "
                  f"{len(serious) - cap} stay unverified (raise verify.maxFindings to cover them)", file=sys.stderr)
        out = {**base, "findings": [{"finding_id": f["finding_id"]} for f in serious[:cap]], **model_arg(cfg, "verifier")}
    else:
        out = {**base, **model_arg(cfg, "synthesizer")}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
