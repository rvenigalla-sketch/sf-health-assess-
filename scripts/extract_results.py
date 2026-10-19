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

from common import Run, info, read_json, write_json

KINDS = {
    # kind: (required keys identifying the object, key used for the file name, output folder)
    "review": ({"shard_id", "findings", "coverage", "follow_up"}, "shard_id", ("findings", "raw")),
    "verify": ({"finding_id", "verdict", "reason"}, "finding_id", ("findings", "verdicts")),
    "synthesis": ({"kind", "executive_summary", "roadmap"}, "kind", ("findings",)),
    "followup": ({"shard_id", "findings", "coverage", "follow_up"}, "shard_id", ("findings", "raw")),
}


# Agent labels set by the workflows (workflows/review.js, verify.js); follow-ups run through review.js.
LABEL_PREFIX = {"review": "review:", "followup": "review:", "verify": "verify:"}


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


def allowed_ids(run: Run, kind: str) -> tuple[set[str] | None, dict[str, str] | None]:
    """(ids this kind may write, finding_id -> fingerprint for verdicts). None = no restriction."""
    if kind in ("review", "followup"):
        ids = {s["shard_id"] for s in (read_json(p) for p in run.path("shards").glob("*.json"))
               if isinstance(s, dict) and "shard_id" in s}
        return {i for i in ids if i.startswith("followup-") == (kind == "followup")}, None
    if kind == "verify":
        expected = read_json(run.path("findings", "verdicts", "_expected.json"), None)
        if not isinstance(expected, dict):
            # workflow_args.py verify writes the map; fall back to the current validated findings.
            from score import content_fingerprint, load_jsonl
            expected = {f["finding_id"]: content_fingerprint(f)
                        for f in load_jsonl(run.path("findings", "validated.jsonl"))}
        return set(expected), expected
    return None, None


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

    # Defence in depth: only accept results for ids this run asked for. An agent that returns a
    # made-up or mistyped id must not create a file that looks like a real shard's / finding's output.
    allowed, expected = allowed_ids(run, args.kind)

    entries = []
    for line in Path(args.journal).open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # The journal records each agent's RAW return value; the id the workflow asked for is in the
    # label of the matching "started" entry (review:<shard_id> / verify:<finding_id>). That label is
    # authoritative: an agent that echoes a wrong id must not land on another shard's / finding's file.
    prefix = LABEL_PREFIX.get(args.kind)
    labels: dict[str, str] = {}
    for e in entries:
        if isinstance(e, dict) and e.get("type") == "started" and isinstance(e.get("label"), str):
            for k in (e.get("key"), e.get("agentId")):
                if k:
                    labels[f"{'key' if k == e.get('key') else 'agent'}:{k}"] = e["label"]

    def label_id(e: dict) -> str | None:
        label = labels.get(f"key:{e.get('key')}") or labels.get(f"agent:{e.get('agentId')}")
        if prefix and label and label.startswith(prefix):
            return label[len(prefix):]
        return None

    found: dict[str, dict] = {}
    by_label: set[str] = set()
    rejected: set[str] = set()

    def accept(key: str, d: dict, labelled: bool) -> None:
        if allowed is not None and key not in allowed:
            rejected.add(key)
            return
        if not labelled and key in by_label:
            return                              # a label-keyed result beats an unlabelled copy
        found[key] = d                          # last write wins (a resumed agent's newest result)
        if labelled:
            by_label.add(key)

    for e in entries:
        lid = label_id(e) if isinstance(e, dict) and e.get("type") == "result" else None
        if lid is not None:
            d = next((d for d in walk(e.get("result")) if keys <= d.keys()), None)
            if d is None:
                continue
            returned = str(d[name_key])
            if returned != lid:
                print(f"EXTRACT WARNING: agent for {lid!r} returned {name_key} {returned!r}; "
                      f"using {lid!r} from its label", file=sys.stderr)
                d = {**d, name_key: lid}
            accept(lid, d, True)
            continue
        for d in walk(e):                       # no label to go by: fall back to the returned id
            if keys <= d.keys():
                accept(str(d[name_key]), d, False)

    for key in sorted(rejected):
        print(f"EXTRACT WARNING: ignored {args.kind} result for unknown {name_key} {key!r} "
              "(not planned for this run)", file=sys.stderr)
    if expected is not None:
        # Bind each verdict to the finding content the verifier was shown (see score.py).
        found = {k: {**v, "fingerprint": expected[k]} for k, v in found.items()}

    for name, obj in found.items():
        safe = name.replace("#", "__").replace("/", "_").replace(":", "_")
        write_json(out_dir / f"{safe}.json", obj)
    info(f"EXTRACT {args.kind}: {len(found)} result(s) -> {out_dir}")
    print(json.dumps({"extracted": len(found)}))
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
