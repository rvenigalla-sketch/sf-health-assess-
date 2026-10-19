"""Local finding library: what A360 consultants decided about past findings, across all runs.

Stored as JSON lines at config library.path (default ~/.sf-health/library.jsonl), outside the repo.
Never committed, never uploaded, never sent to a model as-is. Off when the run's config says
library.enabled: false (the client config saved in the run folder is reused, so the flag can't be lost).

Before an entry is saved, client-specific text is replaced by placeholders:
  - every Apex class / trigger / flow / custom object / field / ... name in the run's inventory -> <name>
  - the labels of those components (read from their metadata files) -> <name>
  - the client's name and its variants (slug, no-space form, acronym, each longer word) -> <client>
  - user names found in the run (preflight user, org-fact user lists) -> <user>
  - e-mail addresses -> <email>, URLs -> <url>, host names -> <host>, Salesforce record ids -> <id>
  - quoted strings -> "<text>", numbers -> #
so the library keeps only the pattern ("<name> runs SOQL inside a loop"), not the client's org.

Reviewer notes are free text, so they are scrubbed the same way and tagged with a salted hash of the
client (never the name). Hints show a note's text only to the same client; for other clients a hint shows
only counts and the reject-reason category chosen in the review sheet.

Used by:
  review_gate.py import   records each Accept / Edit / Reject decision
  review_gate.py export   "Library hint" column: how similar findings were decided before
  plan_shards.py          known false positives per area, passed to reviewers (library.warnReviewers)

Usage:
  python library.py stats [--run <run>] [--config client.yml]
  python library.py false-positives --area flow [--run <run>] [--config client.yml]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import secrets
import sys
from collections import Counter
from pathlib import Path

from common import Run, info, load_config, load_run_config, read_json

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
URL = re.compile(r"\b(?:https?|ftp)://[^\s\"'<>)\]]+", re.I)
HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|edu|gov|biz|info|app|dev|cloud|site|uk|de|fr|nl|au|ca|eu)\b",
                  re.I)
SF_ID = re.compile(r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5}0[A-Za-z0-9]{9}(?:[A-Za-z0-9]{3})?\b")
QUOTED = re.compile(r"\"[^\"\n]{1,160}\"|“[^”\n]{1,160}”|(?<!\w)'[^'\n]{1,160}'(?!\w)")
NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
WORD = re.compile(r"[a-z<>#]+")
LABEL = re.compile(r"^[ \t]{0,4}<(?:label|pluralLabel|masterLabel)>([^<]{3,120})</", re.M)
STOP = {"the", "and", "with", "from", "that", "this", "into", "have", "has", "are", "for", "not", "its", "all"}
# Standard Salesforce objects are not client-identifying and keep findings readable.
STANDARD = {"Account", "Contact", "Lead", "Opportunity", "Case", "User", "Task", "Event", "Campaign",
            "Product2", "Pricebook2", "Quote", "Order", "Contract", "Asset"}
# Single-word labels this generic say nothing about the client; scrubbing them would only blur patterns.
GENERIC_LABELS = {"name", "status", "type", "amount", "total", "description", "owner", "date", "active", "code",
                  "stage", "number", "email", "phone", "notes", "title", "account", "contact", "record"}
# Corporate suffixes and filler words: not identifying on their own.
CLIENT_FILLER = {"inc", "llc", "ltd", "plc", "corp", "corporation", "company", "group", "holdings", "the", "and",
                 "gmbh", "limited", "services", "international"}
GENERIC_USER_PARTS = {"admin", "integration", "user", "api", "test", "sfdc", "system", "noreply", "support",
                      "sales", "info", "service", "automated", "process", "guest", "site"}
# Reject reasons offered in the review sheet. Only the category is shown across clients.
REJECT_REASONS = ["False positive", "Not relevant for this client", "Accepted risk", "Duplicate",
                  "Already fixed", "Other"]
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


def _salt(cfg: dict) -> str:
    """Per-machine salt kept next to the library, so client tags can't be matched to a list of names."""
    p = lib_path(cfg)
    if not p:
        return ""
    s = p.parent / ".salt"
    if not s.exists():
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(secrets.token_hex(16), encoding="utf-8")
    return s.read_text(encoding="utf-8").strip()


def client_tag(cfg: dict, run: Run) -> str:
    """Salted hash of the client's folder slug: same client -> same tag, name never stored."""
    return hashlib.sha256((_salt(cfg) + ":" + run.root.parent.name.lower()).encode()).hexdigest()[:12]


def _words(text: str) -> list[str]:
    """Split on punctuation, spaces, underscores and camelCase."""
    out = []
    for w in re.split(r"[\W_]+", text):
        out += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", w) or ([w] if w else [])
    return [w for w in out if w]


def client_variants(names: list[str]) -> tuple[list[str], list[str]]:
    """(case-insensitive variants, case-sensitive acronyms) of the client's name."""
    loose, acronyms = set(), set()
    for c in names:
        if not c or len(c) < 2:
            continue
        loose.add(c)
        loose.add(re.sub(r"[^A-Za-z0-9_-]+", "-", c).strip("-"))           # slug
        squashed = re.sub(r"[\W_]+", "", c)
        if len(squashed) >= 3:
            loose.add(squashed)
        words = _words(c)
        core = "".join(w for w in words if w.lower() not in CLIENT_FILLER)
        if len(core) >= 3:
            loose.add(core)                                                  # "AcmeWidgets" for "Acme Widgets Inc"
        for w in words:
            if len(w) >= 4 and w.lower() not in CLIENT_FILLER:
                loose.add(w)
        if len(words) >= 2:
            for ws in (words, [w for w in words if w.lower() not in CLIENT_FILLER]):
                acr = "".join(w[0] for w in ws).upper()
                if len(acr) >= 2:
                    acronyms.add(acr)
    loose = {v for v in loose if len(v) >= 3}
    return sorted(loose, key=len, reverse=True), sorted(acronyms, key=len, reverse=True)


def _alternation(items, flags=0, edge=r"[A-Za-z0-9]"):
    items = [i for i in items if i]
    if not items:
        return None
    return re.compile(rf"(?<!{edge})(?:" + "|".join(map(re.escape, sorted(set(items), key=len, reverse=True)))
                      + rf")(?!{edge})", flags)


def _org_users(run: Run) -> set[str]:
    """User names the run knows about: the preflight user and the user columns of org-fact tables."""
    users: set[str] = set()
    log = read_json(run.log_path, {}) or {}
    for s in log.get("steps", []):
        u = ((s.get("org") or {}) if isinstance(s.get("org"), dict) else {}).get("username")
        if u:
            users.add(str(u))
    for topic in ("privileged_users", "licenses_users"):
        p = run.path("index", "orgfacts", f"{topic}.txt")
        if not p.exists():
            continue
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
        for l in lines[1:]:
            v = l.split(" | ")[0].strip()
            if v and v.lower() not in ("none", "?"):
                users.add(v)
    out = set()
    for u in users:
        out.add(u)
        local = u.split("@")[0]
        for part in re.split(r"[\s._+-]+", local):
            if len(part) >= 3 and part.lower() not in GENERIC_USER_PARTS and not part.isdigit():
                out.add(part)
    return out


def _labels(run: Run, inventory: list[dict]) -> set[str]:
    """Top-level labels of every component the run retrieved (object, field, flow, ... labels)."""
    labels: set[str] = set()
    for i in inventory:
        path = i.get("path") or ""
        if not path.endswith("-meta.xml"):
            continue
        p = run.path(*path.split("/"))
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        for m in LABEL.finditer(text):
            lab = m.group(1).strip()
            if len(lab) < 4 or lab in STANDARD or lab.lower() in GENERIC_LABELS:
                continue
            labels.add(lab)
    return labels


class Generalizer:
    """Replaces this run's client-specific text with placeholders (see the module docstring)."""

    def __init__(self, run: Run):
        names: set[str] = set()
        inventory = read_json(run.path("index", "inventory.json"), []) or []
        for i in inventory:
            if i["type"] in ("OrgFact", "FieldUsage", "AutomationSummary"):
                continue
            for part in [i["name"], *re.split(r"[.:/]", i["name"]), i.get("object") or ""]:
                if len(part) >= 3 and part not in STANDARD:
                    names.add(part)
        meta = (read_json(run.log_path, {}) or {}).get("meta", {})
        loose, acronyms = client_variants([c for c in {meta.get("client"), run.root.parent.name} if c])
        self.client = _alternation(loose, re.I)
        self.acronyms = _alternation(acronyms)
        self.users = _alternation(_org_users(run), re.I)
        self.labels = _alternation(_labels(run, inventory), re.I, edge=r"\w")
        self.names = _alternation(names, edge=r"\w")

    def __call__(self, text: str) -> str:
        text = URL.sub("<url>", str(text or ""))
        text = EMAIL.sub("<email>", text)
        text = HOST.sub("<host>", text)
        text = SF_ID.sub("<id>", text)
        text = QUOTED.sub('"<text>"', text)
        for rx, ph in ((self.users, "<user>"), (self.names, "<name>"), (self.labels, "<name>"),
                       (self.client, "<client>"), (self.acronyms, "<client>")):
            if rx:
                text = rx.sub(ph, text)
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


def record(run: Run, cfg: dict, decided: list[tuple]) -> int:
    """decided: (finding as exported, decision accepted|edited|rejected, reviewer notes[, reject reason]).
    Re-importing the same run replaces that run's earlier entries."""
    p = lib_path(cfg)
    if not p:
        return 0
    gen = Generalizer(run)
    src = hashlib.sha1(str(run.root).lower().encode()).hexdigest()[:12]
    tag = client_tag(cfg, run)
    kept = [e for e in load(cfg) if e.get("src") != src]
    today = dt.date.today().isoformat()
    for f, decision, notes, *rest in decided:
        reason = (rest[0] if rest else "") or ""
        title = gen(f["title"])
        kept.append({
            "src": src, "client": tag, "date": today, "area": f["area"], "category": f["category"],
            "rule": f.get("rule") or "", "basis": f.get("basis", ""),
            "severity": f.get("original_severity", f["severity"]), "finalSeverity": f["severity"],
            "decision": decision, "title": title, "tokens": sorted(tokens(title)),
            "recommendation": gen(f.get("recommendation", ""))[:500],
            "reasonCategory": reason if reason in REJECT_REASONS else ("Other" if reason else ""),
            # Scrubbed, and shown only to the same client (see Hints).
            "note": gen(notes or "")[:300],
        })
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept), encoding="utf-8")
    return len(decided)


def _reason_counts(entries: list[dict]) -> str:
    c = Counter(e.get("reasonCategory") for e in entries if e["decision"] == "rejected" and e.get("reasonCategory"))
    return ", ".join(f"{n} {k.lower()}" for k, n in c.most_common())


class Hints:
    def __init__(self, cfg: dict, run: Run):
        self.entries = load(cfg)
        self.gen = Generalizer(run) if self.entries else None
        self.tag = client_tag(cfg, run) if self.entries else None

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
        reasons = _reason_counts(hits)
        if reasons:
            text += f" (reject reasons: {reasons})"
        # Free-text notes only from this client's own earlier reviews; never another client's words.
        own = [e.get("note") for e in hits if e["decision"] == "rejected" and e.get("note") and e.get("client") == self.tag]
        if own:
            text += f'. This client\'s last reject note: "{own[-1][:150]}"'
        return text


def false_positives(cfg: dict, area: str, limit: int = 15, client: str | None = None) -> list[str]:
    """Patterns rejected at least library.falsePositiveAfter times and never accepted or edited.
    Notes are included only for entries of `client` (a client_tag); otherwise just the reason categories."""
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
            note = next((e["note"] for e in reversed(es) if e.get("note") and client and e.get("client") == client), "")
            why = f': "{note}"' if note else (f"; {_reason_counts(es)}" if _reason_counts(es) else "")
            out.append(f"{es[-1]['title']} (rejected {c['rejected']}x{why})")
    return out[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["stats", "false-positives"])
    ap.add_argument("--area")
    ap.add_argument("--run", help="use this run's saved client config (and client for notes)")
    ap.add_argument("--config")
    args = ap.parse_args()
    run = Run(Path(args.run)) if args.run else None
    cfg = load_run_config(run, args.config) if run else load_config(args.config)
    if not lib_path(cfg):
        info("LIBRARY: disabled by config (library.enabled: false)")
        return 0
    if args.action == "stats":
        entries = load(cfg)
        print(json.dumps({"path": str(lib_path(cfg)), "entries": len(entries),
                          "runs": len({e["src"] for e in entries}),
                          "clients": len({e.get("client") for e in entries}),
                          "byDecision": dict(Counter(e["decision"] for e in entries)),
                          "byArea": dict(Counter(e["area"] for e in entries))}, indent=2))
        return 0
    if not args.area:
        info("--area is required")
        return 2
    print("\n".join(false_positives(cfg, args.area, client=client_tag(cfg, run) if run else None)) or "(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
