"""Compare this run with an earlier run of the same org (the baseline). Deterministic.

Findings match one-to-one within the same component + category: first by a shared evidence quote,
then by title similarity (token Jaccard >= 0.5), so moved line numbers and reworded titles don't
count as change. Writes findings/baseline_diff.json and tags each finding in findings.json with
baseline_status: new | unchanged | (fixed findings are listed separately).

Usage:
  python diff_baseline.py --run <this run> --previous <earlier run folder>
  python diff_baseline.py --run <this run> --auto      # newest earlier date folder for the same client
"""

from __future__ import annotations

import argparse
import html
import re
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


TITLE_JACCARD = 0.5
WORD = re.compile(r"[a-z0-9_]+")


def _quotes(f: dict) -> set[str]:
    return {" ".join(html.unescape(e.get("quote") or "").split()).lower() for e in f.get("evidence") or []} - {""}


def _title_tokens(f: dict) -> set[str]:
    return {w for w in WORD.findall((f.get("title") or "").lower()) if len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _quote_overlap(a: set[str], b: set[str]) -> bool:
    """Same evidence line, allowing one quote to be a longer / shorter cut of the other."""
    return any(x == y or (min(len(x), len(y)) >= 12 and (x in y or y in x)) for x in a for y in b)


def match_findings(old: list[dict], cur: list[dict]) -> dict[int, int]:
    """current index -> old index, one-to-one. Candidates share component + category; a pair citing
    the same evidence quote matches first, then the most similar title (token Jaccard >= 0.5).
    A reworded title on the same evidence is the same finding; two old findings on one component are
    two findings (each can be matched or fixed independently)."""
    pairs: dict[int, int] = {}
    used: set[int] = set()
    key = lambda f: (f.get("component"), f.get("category"))
    for pass_ in ("quote", "title"):
        for n, f in enumerate(cur):
            if n in pairs:
                continue
            best, best_score = None, 0.0
            for m, o in enumerate(old):
                if m in used or key(o) != key(f):
                    continue
                sim = _jaccard(_title_tokens(f), _title_tokens(o))
                if pass_ == "quote":
                    if not _quote_overlap(_quotes(f), _quotes(o)):
                        continue
                    score = 1.0 + sim
                else:
                    if sim < TITLE_JACCARD:
                        continue
                    score = sim
                if score > best_score:
                    best, best_score = m, score
            if best is not None:
                pairs[n] = best
                used.add(best)
    return pairs


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
        pairs = match_findings(old["findings"], cur["findings"])
        for n, f in enumerate(cur["findings"]):
            prev = old["findings"][pairs[n]] if n in pairs else None
            f["baseline_status"] = "unchanged" if prev else "new"
            if prev and prev["severity"] != f["severity"]:
                f["baseline_status"] = f"severity {prev['severity']} -> {f['severity']}"
        matched_old = set(pairs.values())
        fixed = [{"id": f.get("id"), "title": f["title"], "severity": f["severity"], "component": f["component"]}
                 for m, f in enumerate(old["findings"]) if m not in matched_old]
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
