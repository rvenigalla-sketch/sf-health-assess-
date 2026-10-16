"""Score an AI review of the seeded fixture against the planted problems.

After running the review workflow on a copy of tests/fixtures/seeded-org (as a run folder),
compare its findings with tests/fixtures/expected_findings.json:

  recall    = planted problems found / planted problems
  precision = findings that match a planted problem / findings at Medium or above
  clean     = High/Critical findings on components listed as clean (should be 0)

A planted problem counts as found when a finding names its component (or one of `anyOf`), has
its category (or one of `altCategories`), and is at least `minSeverity` minus one level.

Usage:
  python tests/eval.py --run <run folder> [--source validated|findings|accepted] [--min-recall 0.8 --min-precision 0.9]
"""

import argparse
import json
import sys
from pathlib import Path

SEV = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
EXPECTED = Path(__file__).resolve().parent / "fixtures" / "expected_findings.json"


def load(run: Path, source: str) -> list[dict]:
    if source == "validated":
        p = run / "findings" / "validated.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    name = "findings.json" if source == "findings" else "accepted.json"
    return json.loads((run / "findings" / name).read_text(encoding="utf-8"))["findings"]


def matches(f: dict, p: dict) -> bool:
    comps = set(p.get("anyOf", [])) | ({p["component"]} if "component" in p else set())
    involved = {f["component"], *f.get("related_components", [])}
    cats = {p["category"], *p.get("altCategories", [])}
    return bool(comps & involved) and f["category"] in cats and SEV[f["severity"]] <= SEV[p["minSeverity"]] + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--source", default="validated", choices=["validated", "findings", "accepted"])
    ap.add_argument("--min-recall", type=float, default=0.8)
    ap.add_argument("--min-precision", type=float, default=0.9)
    args = ap.parse_args()
    exp = json.loads(EXPECTED.read_text(encoding="utf-8"))
    findings = load(Path(args.run), args.source)

    found = {p["id"]: [f.get("finding_id") or f.get("id") for f in findings if matches(f, p)] for p in exp["planted"]}
    recall = sum(bool(v) for v in found.values()) / len(exp["planted"])
    serious = [f for f in findings if SEV[f["severity"]] <= SEV["Medium"]]
    matched = [f for f in serious if any(matches(f, p) for p in exp["planted"])]
    precision = len(matched) / len(serious) if serious else 1.0
    clean_hits = [f for f in findings if f["component"] in exp["clean"] and SEV[f["severity"]] <= SEV["High"]]

    print(f"recall    {recall:.0%}  ({sum(bool(v) for v in found.values())}/{len(exp['planted'])} planted problems found)")
    print(f"precision {precision:.0%}  ({len(matched)}/{len(serious)} Medium+ findings match a planted problem)")
    print(f"clean components with High/Critical findings: {len(clean_hits)}")
    for p in exp["planted"]:
        print(f"  {'FOUND' if found[p['id']] else 'MISSED':6} {p['id']} {p['what']}  {found[p['id']] or ''}")
    unmatched = [f for f in serious if f not in matched]
    if unmatched:
        print("Medium+ findings not matching a planted problem (review: real extra issue or false positive?):")
        for f in unmatched:
            print(f"  {f['severity']:8} {f['component']}  {f['title']}")
    ok = recall >= args.min_recall and precision >= args.min_precision and not clean_hits
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
