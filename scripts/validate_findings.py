"""Stage 5a - Validate reviewer output against the source. Deterministic.

For every finding in <run>/findings/raw/*.json:
  - component exists in the inventory
  - every evidence path exists, belongs to the finding's component / its shard (org fact files are
    shared), the line range is inside the file, and the quote appears in those lines
    (whitespace- and entity-normalized, `...` elisions allowed, 2-line tolerance either side)
  - quotes are distinctive: >= 12 non-blank characters or a non-trivial identifier
  - the finding's shard was planned for this run (rules / carried outputs have no shard)
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
import html
import json
import re
import sys
from pathlib import Path

from common import Run, Timer, info, read_json, write_json

LINE_TOLERANCE = 2
WS = re.compile(r"\s+")
ELISION = re.compile(r"\.\.\.|…")
IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
MIN_QUOTE_CHARS = 12      # non-whitespace characters, unless the quote has a distinctive identifier
MIN_IDENT_CHARS = 4
# Words too common to make a short quote distinctive on their own.
COMMON_WORDS = {
    "public", "private", "protected", "global", "static", "final", "void", "class", "return", "else", "this",
    "null", "true", "false", "with", "without", "sharing", "inherited", "override", "virtual", "abstract",
    "string", "integer", "boolean", "decimal", "list", "from", "where", "select", "insert", "update", "delete",
    "upsert", "then", "when", "break", "continue", "while", "trigger", "before", "after", "super", "catch",
    "finally", "throw", "name", "value", "label", "type", "true", "elements", "targetreference",
}
# Org-wide fact files (index/orgfacts/*) can back a finding on any component.
SHARED_EVIDENCE_TYPES = {"OrgFact"}
# Files a finding may cite as supporting evidence outside its own component / shard. Index digests live in
# sub-folders of index/; the top-level index/*.json files are bookkeeping and are not evidence.
ORG_EVIDENCE_PREFIXES = ("metadata/", "index/flows/", "index/workflows/", "index/crma/", "index/datamodel/",
                         "index/orgfacts/", "index/automation/")
# Script-made outputs with no shard: rule checks and findings carried forward from the previous run.
UNSHARDED = {"rules", "carried"}


def norm(text: str) -> str:
    """Whitespace runs collapsed; XML/HTML entities decoded (sources keep &lt; where agents quote <)."""
    return WS.sub(" ", html.unescape(text)).strip()


def distinctive(fragments: list[str]) -> bool:
    """A quote must be long enough, or contain a non-trivial identifier, to prove it came from the file:
    `}` or `return x;` match almost anywhere."""
    text = "".join(fragments)
    if len(WS.sub("", text)) >= MIN_QUOTE_CHARS:
        return True
    return any(len(t) >= MIN_IDENT_CHARS and t.lower() not in COMMON_WORDS for t in IDENT.findall(text))


def quote_in(quote: str, window: str) -> bool:
    """Quote found in the window; `...` / `…` elisions match any text, fragments must appear in order."""
    pos = 0
    for frag in (norm(p) for p in ELISION.split(quote)):
        if not frag:
            continue
        hit = window.find(frag, pos)
        if hit < 0:
            return False
        pos = hit + len(frag)
    return True


class SourceCache:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._lines: dict[str, list[str] | None] = {}

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self._lines:
            p = (self.root / rel).resolve()
            # Evidence must stay inside the run folder (a string prefix would accept <run>-other/...).
            if not p.is_relative_to(self.root) or not p.is_file():
                self._lines[rel] = None
            else:
                self._lines[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return self._lines[rel]


def item_paths(item: dict | None) -> set[str]:
    if not item:
        return set()
    return {p for p in (item.get("path"), item.get("digestPath"), item.get("sourcePath")) if p}


def allowed_paths(f: dict, items: dict[str, dict], shard: dict | None) -> tuple[set[str], set[str]]:
    """(exact paths, folder prefixes) a finding's evidence may cite: its own component's files, its shard's
    files and context files, files of its related components, and org-wide fact files."""
    ids = {f.get("component"), *(f.get("related_components") or [])}
    if shard:
        ids |= set(shard.get("components", [])) | set(shard.get("context_components", []))
    exact: set[str] = set()
    for cid in ids:
        exact |= item_paths(items.get(cid))
    if shard:
        for fe in shard.get("files", []):
            exact |= {p for p in (fe.get("path"), fe.get("sourcePath")) if p}
        for key in ("relatedClasses", "triggerHandlers"):
            exact |= {c["path"] for c in (shard.get("context") or {}).get(key, []) if c.get("path")}
    exact |= {p for i in items.values() if i.get("type") in SHARED_EVIDENCE_TYPES for p in item_paths(i)}
    # Bundles (LWC / Aura) are folders: any file inside belongs to the component.
    prefixes = {p.rstrip("/") + "/" for cid in ids for p in item_paths(items.get(cid))
                if items.get(cid, {}).get("type") in ("LightningComponentBundle", "AuraDefinitionBundle")}
    exact |= {p + "-meta.xml" for p in list(exact)}
    return exact, prefixes


def check_finding(f: dict, items: dict[str, dict], evidence_ids: set[str], src: SourceCache,
                  shard: dict | None) -> list[str]:
    reasons = []
    if f.get("component") not in items:
        reasons.append(f"unknown component {f.get('component')!r}")
    exact, prefixes = allowed_paths(f, items, shard)
    # Carried findings passed this check last run, and carry_forward only keeps unchanged components.
    carried = shard is None and f.get("shard_id") == "carried"
    in_scope = 0
    for i, ev in enumerate(f.get("evidence") or []):
        path = (ev.get("path") or "").replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        lines = src.lines(path)
        if lines is None:
            reasons.append(f"evidence[{i}]: file not found {path!r}")
            continue
        if path in exact or any(path.startswith(p) for p in prefixes):
            in_scope += 1
        elif not path.startswith(ORG_EVIDENCE_PREFIXES):
            # Supporting evidence may come from any retrieved org file or index digest (a field's metadata,
            # a profile, another class), never from run bookkeeping (inventory.json, findings/, shards/).
            reasons.append(f"evidence[{i}]: {path} is not an org metadata file or index digest")
            continue
        start, end = ev.get("line_start", 0), ev.get("line_end", 0)
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or start > len(lines):
            reasons.append(f"evidence[{i}]: lines {start}-{end} outside file ({len(lines)} lines)")
            continue
        lo = max(0, start - 1 - LINE_TOLERANCE)
        hi = min(len(lines), end + LINE_TOLERANCE)
        window = norm("\n".join(lines[lo:hi]))
        raw_quote = ev.get("quote") or ""
        fragments = [x for x in (norm(p) for p in ELISION.split(raw_quote)) if x]
        if not fragments or not quote_in(raw_quote, window):
            reasons.append(f"evidence[{i}]: quote not found at {path}:{start}-{end}")
        elif not distinctive(fragments):
            reasons.append(f"evidence[{i}]: quote too short to identify the line ({raw_quote!r}); "
                           f"quote at least {MIN_QUOTE_CHARS} characters or a distinctive identifier")
    if not f.get("evidence"):
        reasons.append("no evidence")
    elif not in_scope and not carried and not reasons:
        reasons.append(f"no evidence from {f.get('component')}, its related components or its shard; "
                       "cite at least one line of the component the finding is about")
    for eid in f.get("evidence_ids") or []:
        if eid not in evidence_ids:
            reasons.append(f"unknown evidence_id {eid}")
    if f.get("basis") == "tool-confirmed" and not f.get("evidence_ids"):
        reasons.append("basis tool-confirmed without evidence_ids")
    if shard is None:
        if f.get("shard_id") not in UNSHARDED:
            reasons.append(f"shard {f.get('shard_id')!r} was not planned for this run")
    elif f.get("component") not in set(shard.get("components", [])) \
            and f.get("component") not in set(shard.get("context_components", [])):
        reasons.append(f"component {f.get('component')} is outside shard {shard['shard_id']}")
    return reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run = Run(Path(args.run))

    with Timer() as t:
        inventory = {i["id"]: i for i in read_json(run.path("index", "inventory.json"), [])}
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
