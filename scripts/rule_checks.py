"""Stage 3a - Rule checks. Deterministic, no LLM.

Facts the index already computes exactly become findings directly, in the same shape reviewers
return, so they cost no tokens, read the same every run, and skip the verifier. They go through
validate_findings.py like every other finding (quotes are checked against the files).

Checks:
  flow-no-fault-path   active flows whose data / action steps have no fault path (flow digests)
  old-api-version      Apex classes, triggers and flows below ruleChecks.minApiVersion (api_versions table)
  public-read-write    custom objects with a Public Read/Write org-wide default (sharing table)

Writes findings/raw/rules.json (shard_id "rules"). plan_shards.py passes these to reviewers as
"already reported" so they don't raise them again.

Usage:
  python rule_checks.py --run <run folder> [--config client.yml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import Run, Timer, info, load_config, read_json, write_json

MAX_EVIDENCE = 5


def _lines(run: Run, rel: str) -> list[str]:
    p = run.path(*rel.split("/"))
    return p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []


def _ev(path: str, n: int, line: str) -> dict:
    return {"path": path, "line_start": n, "line_end": n, "quote": line.strip()[:300]}


def _finding(**kw) -> dict:
    return {"evidence_ids": [], "basis": "deterministic", "confidence": "high", "rule": kw.pop("rule"), **kw}


def flow_no_fault_path(run: Run, items: list[dict], cfg: dict) -> list[dict]:
    flows = sorted((i for i in items if i["type"] == "Flow" and i.get("status") == "Active" and i.get("digestPath")),
                   key=lambda i: i["name"])
    hits = []           # (flow item, [(line no, line)])
    for f in flows:
        before_save = f.get("triggerType") == "RecordBeforeSave"
        rows = [(n, line) for n, line in enumerate(_lines(run, f["digestPath"]), 1)
                if "!NO-FAULT-PATH" in line
                # "Update triggering record" in a before-save flow is not DML and can't have a fault path.
                and not (before_save and "{!$Record}" in line)]
        if rows:
            hits.append((f, rows))
    if not hits:
        return []
    hits.sort(key=lambda h: (-len(h[1]), h[0]["name"]))
    elements = sum(len(r) for _, r in hits)
    record_triggered = sum(1 for f, _ in hits if f.get("kind") in ("record-triggered", "process-builder"))
    return [_finding(
        rule="flow-no-fault-path",
        title=f"{len(hits)} active flow(s) have {elements} data or action step(s) with no fault path",
        area="flow", category="error-handling", severity="Medium",
        component=hits[0][0]["id"],
        evidence=[_ev(f["digestPath"], n, line) for f, rows in hits for n, line in rows[:1]][:MAX_EVIDENCE],
        impact=(f"When one of these steps fails (validation rule, locked record, limit), the flow stops with an "
                f"unhandled error. {record_triggered} of these flows run on record save, so the user's save fails "
                f"with a generic error and nobody is told what broke."),
        recommendation=("Add a fault path to each Get/Create/Update/Delete, action and subflow step: log the error "
                        "(e.g. to a custom error object or platform event) and show a clear message on screen flows."),
        effort="M" if len(hits) > 3 else "S",
        related_components=[f["id"] for f, _ in hits[1:]],
    )]


def old_api_versions(run: Run, items: list[dict], cfg: dict) -> list[dict]:
    min_api = float((cfg.get("ruleChecks") or {}).get("minApiVersion", 51.0))
    rel = "index/orgfacts/api_versions.txt"
    ids = {(i["type"], i["name"]): i["id"] for i in items}
    rows = []
    for n, line in enumerate(_lines(run, rel), 1):
        parts = [p.strip() for p in line.split("|")]
        try:
            version = float(parts[0])
        except (ValueError, IndexError):
            continue
        if version < min_api and len(parts) == 3:
            rows.append((n, line, version, ids.get((parts[1], parts[2]))))
    if not rows or "OrgFact:api_versions" not in ids.values():
        return []
    oldest = min(r[2] for r in rows)
    return [_finding(
        rule="old-api-version",
        title=f"{len(rows)} Apex class(es), trigger(s) or flow(s) on API versions below {min_api:g} (oldest {oldest:g})",
        area="release", category="release-readiness", severity="Low",
        component="OrgFact:api_versions",
        evidence=[_ev(rel, n, line) for n, line, _, _ in rows[:MAX_EVIDENCE]],
        impact=("Old API versions keep legacy behaviour, miss newer platform fixes and features, and are the "
                "first to be affected when Salesforce retires API versions."),
        recommendation=("Raise these components to a current API version in a sandbox, re-run their tests and "
                        "check behaviour changes listed in the release notes before deploying."),
        effort="M" if len(rows) > 10 else "S",
        related_components=sorted({cid for _, _, _, cid in rows if cid}),
    )]


def public_read_write(run: Run, items: list[dict], cfg: dict) -> list[dict]:
    rel = "index/orgfacts/sharing.txt"
    ids = {i["id"] for i in items}
    rows = []
    for n, line in enumerate(_lines(run, rel), 1):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[0].endswith("__c") and "ReadWrite" in (parts[1], parts[2]):
            rows.append((n, line, parts[0], parts[2] == "ReadWrite"))
    if not rows or "OrgFact:sharing" not in ids:
        return []
    external = [o for _, _, o, ext in rows if ext]
    return [_finding(
        rule="public-read-write",
        title=f"{len(rows)} custom object(s) have a Public Read/Write org-wide default"
              + (f" ({len(external)} also for external users)" if external else ""),
        area="security", category="security", severity="Medium",
        component="OrgFact:sharing",
        evidence=[_ev(rel, n, line) for n, line, _, _ in rows[:MAX_EVIDENCE]],
        impact=("Every user who can edit the object can see and edit every record, whoever owns it. Roles, "
                "sharing rules and record ownership give no protection on these objects."
                + (" External (community / portal) users get the same access." if external else "")),
        recommendation=("Confirm with the business whether all users really need to edit all records. If not, set "
                        "the default to Private or Public Read Only and open access with sharing rules."),
        effort="M",
        related_components=sorted(f"CustomObject:{o}" for _, _, o, _ in rows if f"CustomObject:{o}" in ids),
    )]


CHECKS = [flow_no_fault_path, old_api_versions, public_read_write]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    run = Run(Path(args.run))
    out = run.path("findings", "raw", "rules.json")
    if not (cfg.get("ruleChecks") or {}).get("enabled", True):
        out.unlink(missing_ok=True)
        info("RULES: disabled in config")
        return 0

    with Timer() as t:
        items = read_json(run.path("index", "inventory.json"), [])
        findings = [f for check in CHECKS for f in check(run, items, cfg)]
        write_json(out, {"shard_id": "rules", "findings": findings,
                         "coverage": {"components_reviewed": [], "skipped": []}, "follow_up": []})
    run.log_step("rule-checks", "ok", t.seconds, findings=len(findings), rules=[f["rule"] for f in findings])
    info(f"RULES: {len(findings)} script-made finding(s): {', '.join(f['rule'] for f in findings) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
