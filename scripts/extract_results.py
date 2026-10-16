"""Copy reviewer / verifier outputs from a workflow run's journal into the run folder.

Workflow scripts cannot write files, and returning every finding to the orchestrator would
flood its context. Each agent's schema-validated return value is recorded in the workflow's
journal.jsonl; this script finds those objects and writes them out verbatim.

Usage:
  python extract_results.py --journal <transcriptDir>/journal.jsonl --run <run folder> --kind review
  python extract_results.py --journal ... --run ... --kind verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import Run, info, write_json

KINDS = {
    # kind: (required keys identifying the object, key used for the file name, output folder)
    "review": ({"shard_id", "findings", "coverage", "follow_up"}, "shard_id", ("findings", "raw")),
    "verify": ({"finding_id", "verdict", "reason"}, "finding_id", ("findings", "verdicts")),
}


def walk(obj):
    """Yield every dict nested anywhere in obj (also inside JSON-encoded strings)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    elif isinstance(obj, str) and obj[:1] in "{[" and len(obj) > 20:
        try:
            yield from walk(json.loads(obj))
        except json.JSONDecodeError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--kind", choices=sorted(KINDS), required=True)
    args = ap.parse_args()
    run = Run(Path(args.run))
    keys, name_key, folder = KINDS[args.kind]
    out_dir = run.path(*folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    found: dict[str, dict] = {}
    for line in Path(args.journal).open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        for d in walk(entry):
            if keys <= d.keys():
                found[str(d[name_key])] = d     # last write wins (a resumed agent's newest result)

    for name, obj in found.items():
        safe = name.replace("#", "__").replace("/", "_").replace(":", "_")
        write_json(out_dir / f"{safe}.json", obj)
    info(f"EXTRACT {args.kind}: {len(found)} result(s) -> {out_dir}")
    print(json.dumps({"extracted": len(found)}))
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
