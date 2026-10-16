"""Compare this run with an earlier run of the same org (the baseline). Deterministic.

Findings match by fingerprint (component + category + title gist), so moved line numbers don't
count as change. Writes findings/baseline_diff.json and tags each finding in findings.json with
baseline_status: new | unchanged | (fixed findings are listed separately).

Usage:
  python diff_baseline.py --run <this run> --previous <earlier run folder>
  python diff_baseline.py --run <this run> --auto      # newest earlier date folder for the same client
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import Run, Timer, info, read_json, write_json

METRIC_PATHS = [
    ("Apex classes", ("apex", "classes")), ("Active flows", ("flows", "byStatus", "Active")),
    ("Active Process Builders", ("flows", "activeProcessBuilders")), ("Active workflow rules", ("workflowRules", "active")),
    ("Objects with automation", ("automation", "objectsWithAutomation")), ("Custom fields", ("digests", "fieldUsage", "customFields")),
    ("Fields not referenced in metadata", ("digests", "fieldUsage", "unreferenced")),
    ("Health Check score", ("org", "healthCheckScore")), ("Users with dangerous permissions", ("org", "usersWithDangerousPermissions")),
    ("Runtime errors (30 days)", ("org", "runtimeErrors")),
]


def dig(d: dict, path: tuple):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--previous")
    ap.add_argument("--auto", action="store_true")
    args = ap.parse_args()
    run = Run(Path(args.run))

    prev_root = Path(args.previous) if args.previous else None
    if args.auto and not prev_root:
        earlier = sorted(p for p in run.root.parent.iterdir()
                         if p.is_dir() and p.name < run.root.name and (p / "findings" / "findings.json").exists())
        prev_root = earlier[-1] if earlier else None
    if not prev_root:
        info("BASELINE: no earlier run to compare with; this run becomes the baseline")
        write_json(run.path("findings", "baseline_diff.json"), {"previous": None})
        return 0

    with Timer() as t:
        cur = read_json(run.path("findings", "findings.json"))
        old = read_json(prev_root / "findings" / "findings.json")
        cur_m = read_json(run.path("index", "metrics.json"), {})
        old_m = read_json(prev_root / "index" / "metrics.json", {})
        old_by_fp = {f["fingerprint"]: f for f in old["findings"]}
        cur_fps = {f["fingerprint"] for f in cur["findings"]}
        for f in cur["findings"]:
            prev = old_by_fp.get(f["fingerprint"])
            f["baseline_status"] = "unchanged" if prev else "new"
            if prev and prev["severity"] != f["severity"]:
                f["baseline_status"] = f"severity {prev['severity']} -> {f['severity']}"
        fixed = [{"id": f["id"], "title": f["title"], "severity": f["severity"], "component": f["component"]}
                 for fp, f in old_by_fp.items() if fp not in cur_fps]
        diff = {
            "previous": prev_root.name,
            "findings": {"new": sum(f["baseline_status"] == "new" for f in cur["findings"]),
                         "unchanged": sum(f["baseline_status"] != "new" for f in cur["findings"]),
                         "fixed": len(fixed), "fixedList": fixed},
            "scores": {"overall": [old["scores"]["overall"], cur["scores"]["overall"]],
                       **{a: [old["scores"]["areas"].get(a, {}).get("score"), v["score"]]
                          for a, v in cur["scores"]["areas"].items()}},
            "metrics": {label: [dig(old_m, p), dig(cur_m, p)] for label, p in METRIC_PATHS},
        }
        cur["baseline"] = {"previous": prev_root.name}
        write_json(run.path("findings", "findings.json"), cur)
        write_json(run.path("findings", "baseline_diff.json"), diff)

    run.log_step("baseline-diff", "ok", t.seconds, previous=prev_root.name, **{k: v for k, v in diff["findings"].items() if k != "fixedList"})
    info(f"BASELINE vs {prev_root.name}: {diff['findings']['new']} new, {diff['findings']['fixed']} fixed, "
         f"score {diff['scores']['overall'][0]} -> {diff['scores']['overall'][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
