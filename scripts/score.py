"""Stage 5b - Apply verdicts, de-duplicate, assign IDs, score. Deterministic.

Inputs : findings/validated.jsonl, findings/verdicts/*.json (from the verify workflow)
Outputs: findings/findings.json   {findings, scores, counts}
         findings/refuted.jsonl   findings the verifier disproved (kept for audit)

Scores are a fixed formula, never set by the LLM:
  penalty(area) = sum of severity weights of its findings
  score(area)   = round(100 * exp(-penalty / scale))
  overall       = weighted mean of area scores (config: scoring.areaWeights)
  rating        = Good >= 80, Fair >= 60, else Needs attention

Usage:
  python score.py --run <run folder> [--config client.yml]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from common import Run, Timer, info, load_config, read_json, write_json

SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
AREA_LABELS = {
    "apex": "Apex code", "flow": "Flows", "automation": "Automation design", "security": "Security and access",
    "datamodel": "Data model", "crma": "CRM Analytics", "integration": "Integrations", "release": "Release readiness",
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.open(encoding="utf-8")] if path.exists() else []


def fingerprint(f: dict) -> str:
    """Stable across runs: same component + category + gist of the title (line numbers may move)."""
    words = re.findall(r"[a-z0-9_]+", f["title"].lower())
    gist = " ".join(w for w in words if len(w) > 3)[:60]
    return hashlib.sha1(f"{f['component']}|{f['category']}|{gist}".encode()).hexdigest()[:12]


def evidence_key(f: dict) -> frozenset:
    return frozenset((e["path"], e["line_start"]) for e in f["evidence"])


def merge(into: dict, other: dict) -> None:
    if SEV_ORDER[other["severity"]] < SEV_ORDER[into["severity"]]:
        into["severity"] = other["severity"]
    seen = {(e["path"], e["line_start"]) for e in into["evidence"]}
    for e in other["evidence"]:
        if (e["path"], e["line_start"]) not in seen and len(into["evidence"]) < 5:
            into["evidence"].append(e)
    into["evidence_ids"] = sorted(set(into["evidence_ids"]) | set(other["evidence_ids"]))
    into["related_components"] = sorted((set(into["related_components"]) | set(other["related_components"])
                                         | {other["component"]}) - {into["component"]})
    into.setdefault("merged_from", []).append(other["finding_id"])


def dedupe(findings: list[dict]) -> list[dict]:
    """Same component + category with overlapping evidence, or the same tool evidence ids -> one finding."""
    findings = sorted(findings, key=lambda f: (SEV_ORDER[f["severity"]], f["finding_id"]))
    kept: list[dict] = []
    for f in findings:
        for k in kept:
            same_place = k["component"] == f["component"] and k["category"] == f["category"] \
                and (evidence_key(k) & evidence_key(f))
            same_tool = f["evidence_ids"] and set(f["evidence_ids"]) <= set(k["evidence_ids"])
            if same_place or same_tool:
                merge(k, f)
                break
        else:
            kept.append(dict(f))
    return kept


# Which finding areas each shard planner covers.
PLANNER_AREAS = {"apex": {"apex"}, "flow": {"flow"}, "automation": {"automation"}, "security": {"security"},
                 "datamodel": {"datamodel"}, "crma": {"crma"}, "platform": {"release", "integration"}}


INTEGRATION_TYPES = {"NamedCredential", "ExternalCredential", "RemoteSiteSetting", "ConnectedApp",
                     "ExternalServiceRegistration", "CspTrustedSite", "PlatformEventChannel"}


def assessed_areas(run: Run) -> set[str]:
    """Areas that had at least one review shard. Integration counts only if the org has integration metadata."""
    plan = read_json(run.path("shards", "plan.json"), {})
    if not plan:
        return set(AREA_LABELS)
    # assessedByArea: the full plan, so an area skipped by an incremental run (nothing changed) still counts.
    by_area = plan.get("assessedByArea") or plan.get("byArea") or {}
    areas = set().union(*(PLANNER_AREAS.get(a, set()) for a in plan.get("areas", []) if by_area.get(a, 1) > 0))
    inventory = read_json(run.path("index", "inventory.json"), [])
    if "integration" in areas and not any(i.get("type") in INTEGRATION_TYPES for i in inventory):
        areas.discard("integration")
    return areas


def score_areas(findings: list[dict], cfg: dict, assessed: set[str] | None = None) -> dict:
    """Areas not reviewed in this run are reported as 'Not assessed' and left out of the overall score."""
    s_cfg = cfg["scoring"]
    weights, scale = s_cfg["severityWeights"], s_cfg["scale"]
    assessed = assessed if assessed is not None else set(AREA_LABELS)
    penalty, counts = defaultdict(float), defaultdict(Counter)
    for f in findings:
        penalty[f["area"]] += weights[f["severity"]]
        counts[f["area"]][f["severity"]] += 1
    rating = lambda s: "Good" if s >= s_cfg["good"] else ("Fair" if s >= s_cfg["fair"] else "Needs attention")
    areas = {}
    for area in AREA_LABELS:
        if area not in assessed and area not in counts:
            areas[area] = {"label": AREA_LABELS[area], "score": None, "rating": "Not assessed",
                           "penalty": 0, "counts": {}}
            continue
        sc = round(100 * math.exp(-penalty[area] / scale))
        areas[area] = {"label": AREA_LABELS[area], "score": sc, "rating": rating(sc),
                       "penalty": penalty[area], "counts": dict(counts[area])}
    w = s_cfg["areaWeights"]
    scored = {a: v for a, v in areas.items() if v["score"] is not None}
    total_w = sum(w.get(a, 1) for a in scored) or 1
    overall = round(sum(v["score"] * w.get(a, 1) for a, v in scored.items()) / total_w) if scored else None
    overall_rating = rating(overall) if overall is not None else "Not assessed"
    # A Critical finding means the org cannot be rated Good overall, whatever the average says.
    if overall_rating == "Good" and any(f["severity"] == "Critical" for f in findings):
        overall_rating = "Fair"
    return {"overall": overall, "overallRating": overall_rating,
            "areas": areas, "formula": {
        "severityWeights": weights, "scale": scale, "areaWeights": w,
        "text": "area score = 100 x exp(-sum of severity weights / scale); overall = weighted mean of assessed areas; any Critical finding caps the overall rating at Fair"}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    run = Run(Path(args.run))

    with Timer() as t:
        findings = load_jsonl(run.path("findings", "validated.jsonl"))
        verdicts = {v["finding_id"]: v for v in (read_json(p) for p in run.path("findings", "verdicts").glob("*.json")) if v}
        refuted, kept = [], []
        for f in findings:
            v = verdicts.get(f["finding_id"])
            if f.get("basis") == "deterministic":
                f["verification"] = {"verdict": "rule check", "reason": "computed by a script from the metadata; not an AI finding"}
            elif v:
                f["verification"] = {"verdict": v["verdict"], "reason": v["reason"]}
                if v["verdict"] == "refuted":
                    refuted.append(f)
                    continue
                if v["verdict"] == "downgraded":
                    f["original_severity"] = f["severity"]
                    f["severity"] = v["corrected_severity"]
            elif f["severity"] in ("Critical", "High"):
                f["verification"] = {"verdict": "unverified", "reason": "no verifier output"}
            kept.append(f)

        merged = dedupe(kept)
        merged.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["area"], f["component"]))
        for n, f in enumerate(merged, 1):
            f["id"] = f"F-{n:03d}"
            f["fingerprint"] = fingerprint(f)
            f.setdefault("review", {"status": "pending"})

        scores = score_areas(merged, cfg, assessed_areas(run))
        counts = {"bySeverity": dict(Counter(f["severity"] for f in merged)),
                  "byArea": dict(Counter(f["area"] for f in merged)),
                  "validated": len(findings), "refuted": len(refuted), "afterDedupe": len(merged),
                  "unverifiedHigh": sum(1 for f in merged if f.get("verification", {}).get("verdict") == "unverified")}
        write_json(run.path("findings", "findings.json"), {"findings": merged, "scores": scores, "counts": counts})
        with run.path("findings", "refuted.jsonl").open("w", encoding="utf-8") as fh:
            for f in refuted:
                fh.write(json.dumps(f, ensure_ascii=False) + "\n")

    run.log_step("score", "ok", t.seconds, **counts, overall=scores["overall"])
    info(f"SCORE: {len(merged)} findings ({counts['bySeverity']}), {len(refuted)} refuted, overall {scores['overall']} "
         f"({scores['overallRating']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
