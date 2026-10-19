"""Redact personal data from a run before any AI stage can read it.

The assessment never reads org records, but people type real values into configuration: alert
recipients in workflows and flows, phone numbers in flow constants, names and numbers in Apex test
data, filter values in CRM Analytics. This step replaces them, in place, in everything a reviewer agent
could open (metadata/, tools/, manifest/), on the consultant's machine:

  emails                        -> [redacted-email]
  phone numbers (NANP formats)  -> [redacted-phone]
  SSN-shaped numbers            -> [redacted-ssn]
  privacy.redactTestData: true  -> every string literal in Apex test classes becomes '[redacted-text]'

Placeholders contain no < > & or quotes, so XML, HTML, JSON and Apex files stay valid. They keep the
structure visible ("a hard-coded email address"), so reviewers can still report
the problem without seeing the value. It is idempotent; build_index.py runs it before indexing, so every
AI stage reads redacted files, and the quote check validates against the same redacted text.
Counts only (never values) are written to <run>/privacy.json.

Usage:
  python privacy.py --run <run>            # redact (also done automatically by build_index.py)
  python privacy.py --run <run> --check    # count what is left, change nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Run, info, load_run_config, read_json, write_json  # noqa: E402

# Folders a reviewer could open. index/ is rebuilt from these, so it inherits the redaction.
REDACT_DIRS = ("metadata", "tools", "manifest")

PATTERNS = {
    "email": (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), "[redacted-email]"),
    "ssn": (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[redacted-ssn]"),
    "phone": (re.compile(r"(?<![\w.])(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?![\w.])"), "[redacted-phone]"),
}
APEX_LITERAL = re.compile(r"'(?:\\.|[^'\\\n])*'")
IS_TEST = re.compile(r"@istest", re.I)


def keep_literal(lit: str) -> bool:
    """Apex test literals that can't be personal data: empty / 1-char, API names, already redacted."""
    body = lit[1:-1]
    return len(body) <= 1 or "__" in body or body.startswith("[redacted-")


def redact_text(text: str, path: str, test_data: bool, counts: Counter) -> str:
    for kind, (rx, repl) in PATTERNS.items():
        text, n = rx.subn(repl, text)
        if n:
            counts[kind] += n
    if test_data and path.endswith(".cls") and IS_TEST.search(text):
        def sub(m: re.Match) -> str:
            if keep_literal(m.group(0)):
                return m.group(0)
            counts["apexTestLiteral"] += 1
            return "'[redacted-text]'"
        text = APEX_LITERAL.sub(sub, text)
    return text


def walk_files(root: Path):
    # os.walk: works for paths over the Windows limit, where Path.is_file() would fail silently.
    for d, _, names in os.walk(root):
        for name in names:
            yield Path(d) / name


def process(run: Run, cfg: dict, write: bool) -> dict:
    pcfg = cfg.get("privacy") or {}
    test_data = bool(pcfg.get("redactTestData"))
    totals: Counter = Counter()
    by_folder: dict[str, Counter] = {}
    changed, unreadable = 0, 0
    for top in REDACT_DIRS:
        for f in walk_files(run.path(top)):
            try:
                raw = f.read_bytes()
            except OSError:
                unreadable += 1
                continue
            if b"\0" in raw[:4096]:
                continue                      # binary
            text = raw.decode("utf-8", errors="surrogateescape")
            counts: Counter = Counter()
            new = redact_text(text, f.name, test_data, counts)
            if not counts:
                continue
            totals.update(counts)
            rel = f.relative_to(run.root).parts
            folder = "/".join(rel[:5] if rel[0] == "metadata" else rel[:2])
            by_folder.setdefault(folder, Counter()).update(counts)
            if write and new != text:
                try:
                    f.write_bytes(new.encode("utf-8", errors="surrogateescape"))
                    changed += 1
                except OSError:
                    unreadable += 1
    return {"counts": dict(totals), "byFolder": {k: dict(v) for k, v in sorted(by_folder.items())},
            "filesChanged": changed, "unreadable": unreadable, "redactTestData": test_data}


def redact_run(run: Run, cfg: dict) -> dict:
    """Redact in place and record counts (accumulated across calls) in <run>/privacy.json."""
    if not (cfg.get("privacy") or {}).get("redact", True):
        info("PRIVACY: redaction is OFF (privacy.redact: false); AI stages will read unredacted values")
        report = {"redact": False, "at": dt.datetime.now().isoformat(timespec="seconds")}
        write_json(run.path("privacy.json"), report)
        return report
    res = process(run, cfg, write=True)
    prev = read_json(run.path("privacy.json"), {}) or {}
    total = Counter(prev.get("redactedTotal") or {})
    total.update(res["counts"])
    report = {"redact": True, "at": dt.datetime.now().isoformat(timespec="seconds"),
              "dataPolicy": cfg.get("dataPolicy"), "redactTestData": res["redactTestData"],
              "redactedTotal": dict(total), "lastPass": res}
    write_json(run.path("privacy.json"), report)
    run.log_step("privacy", "ok", 0, redacted=res["counts"], filesChanged=res["filesChanged"],
                 unreadable=res["unreadable"])
    info(f"PRIVACY: redacted {sum(res['counts'].values())} value(s) in {res['filesChanged']} file(s) "
         f"{res['counts'] or ''}; totals for this run: {dict(total) or 'none'}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--config")
    ap.add_argument("--check", action="store_true", help="count remaining values; change nothing")
    args = ap.parse_args()
    run = Run(Path(args.run))
    cfg = load_run_config(run, args.config)
    if args.check:
        res = process(run, cfg, write=False)
        print(json.dumps({"remaining": res["counts"], "byFolder": res["byFolder"]}, indent=1))
        return 1 if res["counts"] else 0
    redact_run(run, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
