"""Stage 5a - Validate reviewer output against the source. Deterministic.

For every finding in <run>/findings/raw/*.json:
  - component exists in the inventory
  - every evidence path exists, the line range is inside the file, and the quote appears in
    those lines (whitespace-normalized, with a 2-line tolerance either side)
  - every evidence_id exists in index/evidence.jsonl
  - basis "tool-confirmed" has at least one evidence_id
Findings that fail any check go to findings/rejected.jsonl with the reasons; the rest go to
findings/validated.jsonl. Coverage: components a shard was given but neither reviewed nor
skipped-with-reason are reported in findings/coverage.json.

Usage:
  python validate_findings.py --run <run folder>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common import Run, Timer, info, read_json, write_json

LINE_TOLERANCE = 2
WS = re.compile(r"\s+")


def norm(text: str) -> str:
    return WS.sub(" ", text).strip()


class SourceCache:
    def __init__(self, root: Path):
        self.root = root
        self._lines: dict[str, list[str] | None] = {}

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self._lines:
            p = (self.root / rel).resolve()
            # Evidence must stay inside the run folder.
            if not str(p).startswith(str(self.root)) or not p.is_file():
                self._lines[rel] = None
            else:
                self._lines[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return self._lines[rel]


def check_finding(f: dict, inventory: set[str], evidence_ids: set[str], src: SourceCache,
                  shard: dict | None) -> list[str]:
    reasons = []
    if f.get("component") not in inventory:
        reasons.append(f"unknown component {f.get('component')!r}")
    for i, ev in enumerate(f.get("evidence") or []):
        path = (ev.get("path") or "").replace("\\", "/")
        lines = src.lines(path)
        if lines is None:
            reasons.append(f"evidence[{i}]: file not found {path!r}")
            continue
        start, end = ev.get("line_start", 0), ev.get("line_end", 0)
        if start < 1 or end < start or start > len(lines):
            reasons.append(f"evidence[{i}]: lines {start}-{end} outside file ({len(lines)} lines)")
            continue
        lo = max(0, start - 1 - LINE_TOLERANCE)
        hi = min(len(lines), end + LINE_TOLERANCE)
        window = norm("\n".join(lines[lo:hi]))
        quote = norm(ev.get("quote") or "")
        if not quote or quote not in window:
            reasons.append(f"evidence[{i}]: quote not found at {path}:{start}-{end}")
    if not f.get("evidence"):
        reasons.append("no evidence")
    for eid in f.get("evidence_ids") or []:
        if eid not in evidence_ids:
            reasons.append(f"unknown evidence_id {eid}")
    if f.get("basis") == "tool-confirmed" and not f.get("evidence_ids"):
        reasons.append("basis tool-confirmed without evidence_ids")
    if shard and f.get("component") not in set(shard.get("components", [])) \
            and f.get("component") not in set(shard.get("context_components", [])):
        reasons.append(f"component {f.get('component')} is outside shard {shard['shard_id']}")
    return reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run = Run(Path(args.run))

    with Timer() as t:
        inventory = {i["id"] for i in read_json(run.path("index", "inventory.json"), [])}
        ev_path = run.path("index", "evidence.jsonl")
        evidence_ids = {json.loads(l)["evidence_id"] for l in ev_path.open(encoding="utf-8")} if ev_path.exists() else set()
        shards = {s["shard_id"]: s for s in (read_json(p) for p in run.path("shards").glob("*.json")) if s and "shard_id" in s}
        src = SourceCache(run.root)

        validated, rejected, coverage = [], [], {}
        for raw in sorted(run.path("findings", "raw").glob("*.json")):
            out = read_json(raw)
            if not out:
                continue
            shard_id = out.get("shard_id") or raw.stem
            shard = shards.get(shard_id)
            for n, f in enumerate(out.get("findings", []), 1):
                f = {**f, "shard_id": shard_id, "finding_id": f"{shard_id}#{n}"}
                reasons = check_finding(f, inventory, evidence_ids, src, shard)
                (rejected if reasons else validated).append({**f, "rejected_reasons": reasons} if reasons else f)
            if shard:
                given = set(shard.get("components", []))
                cov = out.get("coverage", {})
                done = set(cov.get("components_reviewed", [])) | {s["component"] for s in cov.get("skipped", [])}
                coverage[shard_id] = {"given": len(given), "missing": sorted(given - done),
                                      "skipped": cov.get("skipped", [])}
        missing_shards = sorted(set(shards) - {p.stem for p in run.path("findings", "raw").glob("*.json")})

        with run.path("findings", "validated.jsonl").open("w", encoding="utf-8") as fh:
            for f in validated:
                fh.write(json.dumps(f, ensure_ascii=False) + "\n")
        with run.path("findings", "rejected.jsonl").open("w", encoding="utf-8") as fh:
            for f in rejected:
                fh.write(json.dumps(f, ensure_ascii=False) + "\n")
        write_json(run.path("findings", "coverage.json"),
                   {"shardsWithoutOutput": missing_shards, "shards": coverage})

    total = len(validated) + len(rejected)
    run.log_step("validate", "ok", t.seconds, findings=total, validated=len(validated),
                 rejected=len(rejected), shardsWithoutOutput=len(missing_shards))
    info(f"VALIDATE: {len(validated)}/{total} findings passed, {len(rejected)} rejected, "
         f"{len(missing_shards)} shard(s) without output")
    return 0


if __name__ == "__main__":
    sys.exit(main())
