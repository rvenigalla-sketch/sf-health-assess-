"""Local finding library: what A360 consultants decided about past findings, across all runs.

Stored as JSON lines at config library.path (default ~/.sf-health/library.jsonl), outside the repo.
Never committed, never uploaded, never sent to a model as-is.

Before an entry is saved, client-specific names are replaced by placeholders:
  - every Apex class / trigger / flow / custom object / field / ... name in the run's inventory -> <name>
  - e-mail addresses -> <email>, Salesforce record ids -> <id>, the client's name -> <client>
  - numbers -> #
so the library keeps only the pattern ("<name> runs SOQL inside a loop"), not the client's org.

Used by:
  review_gate.py import   records each Accept / Edit / Reject decision
  review_gate.py export   "Library hint" column: how similar findings were decided before
  plan_shards.py          known false positives per area, passed to reviewers

Usage:
  python library.py stats [--config client.yml]
  python library.py false-positives --area flow [--config client.yml]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from common import Run, info, load_config, read_json

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
SF_ID = re.compile(r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5}0[A-Za-z0-9]{9}(?:[A-Za-z0-9]{3})?\b")
NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
WORD = re.compile(r"[a-z<>#]+")
STOP = {"the", "and", "with", "from", "that", "this", "into", "have", "has", "are", "for", "not", "its", "all"}
# Standard Salesforce objects are not client-identifying and keep findings readable.
STANDARD = {"Account", "Contact", "Lead", "Opportunity", "Case", "User", "Task", "Event", "Campaign",
            "Product2", "Pricebook2", "Quote", "Order", "Contract", "Asset"}
SIMILAR = 0.6


def lib_path(cfg: dict) -> Path | None:
    lib = cfg.get("library") or {}
    if not lib.get("enabled", True):
        return None
    return Path(str(lib.get("path", "~/.sf-health/library.jsonl"))).expanduser()


def load(cfg: dict) -> list[dict]:
    p = lib_path(cfg)
    if not p or not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class Generalizer:
    """Replaces this run's component names (and client name) with placeholders."""

    def __init__(self, run: Run):
        names: set[str] = set()
        for i in read_json(run.path("index", "inventory.json"), []):
            if i["type"] in ("OrgFact", "FieldUsage", "AutomationSummary"):
                continue
            for part in [i["name"], *re.split(r"[.:/]", i["name"]), i.get("object") or ""]:
                if len(part) >= 3 and part not in STANDARD:
                    names.add(part)
        meta = (read_json(run.log_path, {}) or {}).get("meta", {})
        self.client = [c for c in {meta.get("client"), run.root.parent.name} if c and len(c) >= 2]
        ordered = sorted(names, key=len, reverse=True)
        self.names = re.compile(r"(?<![\w])(?:" + "|".join(map(re.escape, ordered)) + r")(?![\w])") if ordered else None

    def __call__(self, text: str) -> str:
        text = EMAIL.sub("<email>", text or "")
        text = SF_ID.sub("<id>", text)
        for c in self.client:
            text = re.sub(re.escape(c), "<client>", text, flags=re.I)
        if self.names:
            text = self.names.sub("<name>", text)
        return NUMBER.sub("#", text)


def tokens(text: str) -> frozenset:
    return frozenset(w for w in WORD.findall(text.lower()) if len(w) > 3 and w not in STOP and w not in ("<name>",))


def similar(a: dict, b_area: str, b_category: str, b_tokens: frozenset) -> bool:
    if a["area"] != b_area or a["category"] != b_category:
        return False
    ta = frozenset(a["tokens"])
    if ta == b_tokens:
        return True
    union = ta | b_tokens
    return bool(union) and len(ta & b_tokens) / len(union) >= SIMILAR


def record(run: Run, cfg: dict, decided: list[tuple[dict, str, str]]) -> int:
    """decided: (finding as exported, decision accepted|edited|rejected, reviewer notes).
    Re-importing the same run replaces that run's earlier entries."""
    p = lib_path(cfg)
    if not p:
        return 0
    gen = Generalizer(run)
    src = hashlib.sha1(str(run.root).lower().encode()).hexdigest()[:12]
    kept = [e for e in load(cfg) if e.get("src") != src]
    today = dt.date.today().isoformat()
    for f, decision, notes in decided:
        title = gen(f["title"])
        kept.append({
            "src": src, "date": today, "area": f["area"], "category": f["category"],
            "rule": f.get("rule") or "", "basis": f.get("basis", ""),
            "severity": f.get("original_severity", f["severity"]), "finalSeverity": f["severity"],
            "decision": decision, "title": title, "tokens": sorted(tokens(title)),
            "recommendation": gen(f.get("recommendation", ""))[:500], "reason": gen(notes or "")[:300],
        })
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept), encoding="utf-8")
    return len(decided)


class Hints:
    def __init__(self, cfg: dict, run: Run):
        self.entries = load(cfg)
        self.gen = Generalizer(run) if self.entries else None

    def __call__(self, f: dict) -> str:
        if not self.entries:
            return ""
        t = tokens(self.gen(f["title"]))
        hits = [e for e in self.entries if similar(e, f["area"], f["category"], t)]
        if not hits:
            return "New pattern"
        c = Counter(e["decision"] for e in hits)
        parts = [f"{c[k]} {k}" for k in ("accepted", "edited", "rejected") if c[k]]
        text = f"Seen {len(hits)}x before: {', '.join(parts)}"
        reasons = [e["reason"] for e in hits if e["decision"] == "rejected" and e["reason"]]
        if reasons:
            text += f'. Last reject reason: "{reasons[-1][:150]}"'
        return text


def false_positives(cfg: dict, area: str, limit: int = 15) -> list[str]:
    """Patterns rejected at least library.falsePositiveAfter times and never accepted or edited."""
    after = int((cfg.get("library") or {}).get("falsePositiveAfter", 2))
    groups: dict[tuple, list[dict]] = {}
    for e in load(cfg):
        if e["area"] != area:
            continue
        key = next((k for k in groups if similar(groups[k][0], e["area"], e["category"], frozenset(e["tokens"]))),
                   (e["category"], e["title"]))
        groups.setdefault(key, []).append(e)
    out = []
    for es in groups.values():
        c = Counter(e["decision"] for e in es)
        if c["rejected"] >= after and not (c["accepted"] or c["edited"]):
            reason = next((e["reason"] for e in reversed(es) if e["reason"]), "")
            out.append(f"{es[-1]['title']} (rejected {c['rejected']}x" + (f': "{reason}")' if reason else ")"))
    return out[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["stats", "false-positives"])
    ap.add_argument("--area")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.action == "stats":
        entries = load(cfg)
        print(json.dumps({"path": str(lib_path(cfg)), "entries": len(entries),
                          "runs": len({e["src"] for e in entries}),
                          "byDecision": dict(Counter(e["decision"] for e in entries)),
                          "byArea": dict(Counter(e["area"] for e in entries))}, indent=2))
        return 0
    if not args.area:
        info("--area is required")
        return 2
    print("\n".join(false_positives(cfg, args.area)) or "(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
