"""Score an AI review of the seeded fixture against the planted problems.

After running the review workflow on a copy of tests/fixtures/seeded-org (as a run folder),
compare its findings with tests/fixtures/expected_findings.json.

A finding can match a planted problem when it names its component (or one of `anyOf`), has its
category (or one of `altCategories`), and is at least `minSeverity` minus one level. Matching is
one-to-one: each planted problem is credited to at most one finding (a maximum matching, preferring
the more severe finding). Other findings that also fit an already-credited problem are duplicates.

  recall             = planted problems matched / planted problems
  precision (all)    = findings matched / all findings (Low included; duplicates count against)
  precision (Medium+)= Medium+ findings matched / Medium+ findings
  clean              = High/Critical findings on components listed as clean (should be 0)

Sources: validated = findings/validated.jsonl (after the quote check, before the verifier);
findings = findings/findings.json (after verify and scoring); accepted = findings/accepted.json.
The default `auto` reports every stage that exists and gates on the latest of validated/findings.

Usage:
  python tests/eval.py --run <run folder> [--source auto|validated|findings|accepted]
                       [--min-recall 0.8 --min-precision 0.9 --precision-scope all|medium]
"""

import argparse
import json
import sys
from pathlib import Path

SEV = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
EXPECTED = Path(__file__).resolve().parent / "fixtures" / "expected_findings.json"
SOURCES = {"validated": "validated.jsonl", "findings": "findings.json", "accepted": "accepted.json"}


def load(run: Path, source: str) -> list[dict]:
    p = run / "findings" / SOURCES[source]
    if source == "validated":
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return json.loads(p.read_text(encoding="utf-8"))["findings"]


def matches(f: dict, p: dict) -> bool:
    comps = set(p.get("anyOf", [])) | ({p["component"]} if "component" in p else set())
    involved = {f["component"], *f.get("related_components", [])}
    cats = {p["category"], *p.get("altCategories", [])}
    return bool(comps & involved) and f["category"] in cats and SEV[f["severity"]] <= SEV[p["minSeverity"]] + 1


def match_one_to_one(findings: list[dict], planted: list[dict]) -> dict[int, int]:
    """Maximum bipartite matching (Kuhn's algorithm): planted index -> finding index.
    Planted problems are tried in order; candidate findings most severe first."""
    cand = [sorted((i for i, f in enumerate(findings) if matches(f, p)), key=lambda i: SEV[findings[i]["severity"]])
            for p in planted]
    owner: dict[int, int] = {}            # finding index -> planted index

    def augment(pi: int, seen: set) -> bool:
        for fi in cand[pi]:
            if fi in seen:
                continue
            seen.add(fi)
            if fi not in owner or augment(owner[fi], seen):
                owner[fi] = pi
                return True
        return False

    for pi in range(len(planted)):
        augment(pi, set())
    return {pi: fi for fi, pi in owner.items()}


def evaluate(findings: list[dict], exp: dict) -> dict:
    planted = exp["planted"]
    pairs = match_one_to_one(findings, planted)
    matched = set(pairs.values())
    dupes = [i for i, f in enumerate(findings) if i not in matched and any(matches(f, p) for p in planted)]
    serious = [i for i, f in enumerate(findings) if SEV[f["severity"]] <= SEV["Medium"]]
    ratio = lambda num, den: num / den if den else 1.0
    return {
        "pairs": pairs, "dupes": dupes, "serious": serious,
        "recall": ratio(len(pairs), len(planted)),
        "precision_all": ratio(len(matched), len(findings)),
        "precision_medium": ratio(len(matched & set(serious)), len(serious)),
        "matched": matched,
        "clean_hits": [f for f in findings if f["component"] in exp["clean"] and SEV[f["severity"]] <= SEV["High"]],
    }


def report(name: str, findings: list[dict], exp: dict) -> dict:
    r = evaluate(findings, exp)
    planted = exp["planted"]
    fid = lambda i: findings[i].get("finding_id") or findings[i].get("id")
    print(f"== {name}: {len(findings)} findings")
    print(f"recall              {r['recall']:.0%}  ({len(r['pairs'])}/{len(planted)} planted problems matched one-to-one)")
    print(f"precision (all)     {r['precision_all']:.0%}  ({len(r['matched'])}/{len(findings)} findings, Low included)")
    print(f"precision (Medium+) {r['precision_medium']:.0%}  "
          f"({len(r['matched'] & set(r['serious']))}/{len(r['serious'])} Medium+ findings)")
    print(f"duplicates          {len(r['dupes'])}  (fit a planted problem already credited to another finding)")
    print(f"clean components with High/Critical findings: {len(r['clean_hits'])}")
    for pi, p in enumerate(planted):
        hit = r["pairs"].get(pi)
        print(f"  {'FOUND' if hit is not None else 'MISSED':6} {p['id']} {p['what']}  {fid(hit) if hit is not None else ''}")
    if r["dupes"]:
        print("Duplicates (count against precision):")
        for i in r["dupes"]:
            print(f"  {findings[i]['severity']:8} {findings[i]['component']}  {findings[i]['title']}")
    extra = [i for i in range(len(findings)) if i not in r["matched"] and i not in r["dupes"]]
    if extra:
        print("Findings not matching any planted problem (review: real extra issue or false positive?):")
        for i in extra:
            print(f"  {findings[i]['severity']:8} {findings[i]['component']}  {findings[i]['title']}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--source", default="auto", choices=["auto", *SOURCES])
    ap.add_argument("--min-recall", type=float, default=0.8)
    ap.add_argument("--min-precision", type=float, default=0.9)
    ap.add_argument("--precision-scope", default="all", choices=["all", "medium"],
                    help="which precision the gate uses (default all: Low findings included)")
    args = ap.parse_args()
    exp = json.loads(EXPECTED.read_text(encoding="utf-8"))
    run = Path(args.run)
    if args.source == "auto":
        sources = [s for s in SOURCES if (run / "findings" / SOURCES[s]).exists()]
        if not sources:
            print("no findings files in the run folder")
            return 1
        gate = next((s for s in ("findings", "validated") if s in sources), sources[-1])
    else:
        sources, gate = [args.source], args.source
    results = {}
    for s in sources:
        label = {"validated": "validated (before verify)", "findings": "scored (after verify)",
                 "accepted": "accepted (after consultant review)"}[s]
        results[s] = report(label, load(run, s), exp)
        print()
    r = results[gate]
    precision = r["precision_all"] if args.precision_scope == "all" else r["precision_medium"]
    ok = r["recall"] >= args.min_recall and precision >= args.min_precision and not r["clean_hits"]
    print(f"gate on {gate}, precision scope {args.precision_scope}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
