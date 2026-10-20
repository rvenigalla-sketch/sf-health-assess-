"""Actual Claude token usage of an assessment run, from Claude Code's local session logs.

Claude Code writes every chat to ~/.claude/projects/<project>/<session>.jsonl, sub-agents to
<session>/subagents/agent-*.jsonl and workflow agents to <session>/subagents/workflows/wf_*/agent-*.jsonl,
each assistant reply with its token usage. This script finds the sessions that worked on a run (their
log mentions the run folder), adds the usage up per stage and model, and writes <run>/usage.json.

Stages: review / followup / verify / synthesize (workflow agents, from the workflow journal's labels),
orchestrator (the chat session itself, from its first to its last mention of the run folder: commands,
progress, decisions) and other (any other sub-agent that worked on the run).
Collect, index, rule checks and plan run as scripts and use no tokens.

Only token counts are read; no message content is printed or stored.

Usage:
  python usage_report.py --run <run>                       # sessions found automatically
  python usage_report.py --run <run> --session <id> [...]  # or name the sessions
  [--projects-dir ~/.claude/projects]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Run, info, write_json  # noqa: E402

FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
SHORT = {"input_tokens": "input", "cache_creation_input_tokens": "cacheWrite",
         "cache_read_input_tokens": "cacheRead", "output_tokens": "output"}
STAGE_ORDER = ("review", "followup", "verify", "synthesize", "orchestrator", "other")


def run_markers(run_root: Path) -> list[str]:
    """Spellings of the run folder as they appear inside JSON logs (escaped backslashes, slashes)."""
    p = str(run_root.resolve())
    tail = "/".join(Path(p).parts[-3:])          # <workspace>/<client>/<date>, robust to drive spelling
    forms = {p, p.replace("\\", "/"), p.replace("\\", "\\\\"), tail, tail.replace("/", "\\\\")}
    return sorted({f.lower() for f in forms if f})


def mentions(path: Path, markers: list[str]) -> bool:
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                low = line.lower()
                if any(m in low for m in markers):
                    return True
    except OSError:
        pass
    return False


def run_window(path: Path, markers: list[str]) -> tuple[int, int] | None:
    """Line range of a chat log from the first to the last line that mentions the run folder."""
    first = last = None
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                low = line.lower()
                if any(m in low for m in markers):
                    first = i if first is None else first
                    last = i
    except OSError:
        return None
    return None if first is None else (first, last)


def usage_of(path: Path, window: tuple[int, int] | None = None) -> dict[str, dict[str, int]]:
    """{model: {field: tokens}} for one log, optionally only lines first..last of `window`. A reply is
    logged once per content block with the same usage, so replies are counted once per message id."""
    seen, out = set(), defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    try:
        fh = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    with fh:
        for i, line in enumerate(fh):
            if window and not window[0] <= i <= window[1] + 1:   # +1: the reply after the last mention
                continue
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = e.get("message") if isinstance(e.get("message"), dict) else None
            if e.get("type") != "assistant" or not msg or not isinstance(msg.get("usage"), dict):
                continue
            key = msg.get("id") or e.get("requestId") or e.get("uuid")
            if key in seen:
                continue
            seen.add(key)
            model = msg.get("model") or "unknown"
            if model == "<synthetic>":
                continue
            for f in FIELDS:
                out[model][f] += int(msg["usage"].get(f) or 0)
    return dict(out)


def workflow_labels(wf_dir: Path) -> dict[str, str]:
    """agentId -> label from a workflow journal's 'started' entries."""
    labels = {}
    j = wf_dir / "journal.jsonl"
    if j.exists():
        with j.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == "started" and e.get("agentId"):
                    labels[e["agentId"]] = e.get("label") or ""
    return labels


def stage_of(label: str) -> str:
    if label.startswith("review:"):
        return "followup" if label.split(":", 1)[1].startswith("followup-") else "review"
    if label.startswith("verify:"):
        return "verify"
    if label.startswith("synthesize"):
        return "synthesize"
    return "other"


def add(total: dict, stage: str, usage: dict, agents: int = 0) -> None:
    st = total.setdefault(stage, {"agents": 0, "byModel": {}})
    st["agents"] += agents
    for model, u in usage.items():
        m = st["byModel"].setdefault(model, dict.fromkeys(FIELDS, 0))
        for f in FIELDS:
            m[f] += u[f]


def collect(projects: Path, run_root: Path, sessions: list[str] | None) -> tuple[dict, list[str]]:
    markers = run_markers(run_root)
    totals: dict = {}
    used: list[str] = []
    per_session: dict[str, int] = {}
    for proj in sorted(p for p in projects.iterdir() if p.is_dir()) if projects.is_dir() else []:
        for log in sorted(proj.glob("*.jsonl")):
            sid = log.stem
            if sessions is not None:
                if sid not in sessions:
                    continue
            elif not mentions(log, markers):
                continue
            used.append(sid)
            # The chat itself: only the stretch that worked on this run, so a long chat that also did
            # other work isn't counted whole. With --session, the whole session is counted.
            chat = usage_of(log, None if sessions is not None else run_window(log, markers))
            add(totals, "orchestrator", chat)
            per_session[sid] = sum(sum(u.values()) for u in chat.values())
            sub = proj / sid / "subagents"
            if not sub.is_dir():
                continue
            # Workflow agents: count those that worked on this run (their prompt names the run folder).
            for wf in sorted((sub / "workflows").glob("wf_*")):
                labels = workflow_labels(wf)
                for a in sorted(wf.glob("agent-*.jsonl")):
                    if sessions is None and not mentions(a, markers):
                        continue
                    stage = stage_of(labels.get(a.stem.removeprefix("agent-"), ""))
                    add(totals, stage, usage_of(a), agents=1)
            # Other sub-agents launched from the chat (not workflow agents).
            for a in sorted(sub.glob("agent-*.jsonl")):
                if sessions is None and not mentions(a, markers):
                    continue
                add(totals, "other", usage_of(a), agents=1)
    return totals, per_session


def summarize(totals: dict) -> dict:
    stages, grand = {}, dict.fromkeys(FIELDS, 0)
    by_model: dict = {}
    for stage in [s for s in STAGE_ORDER if s in totals] + [s for s in totals if s not in STAGE_ORDER]:
        st = totals[stage]
        s_tot = dict.fromkeys(FIELDS, 0)
        for model, u in st["byModel"].items():
            bm = by_model.setdefault(model, dict.fromkeys(FIELDS, 0))
            for f in FIELDS:
                s_tot[f] += u[f]
                grand[f] += u[f]
                bm[f] += u[f]
        if sum(s_tot.values()) == 0 and not st["agents"]:
            continue
        stages[stage] = {"agents": st["agents"], "total": sum(s_tot.values()),
                         **{SHORT[f]: s_tot[f] for f in FIELDS},
                         "byModel": {m: {SHORT[f]: u[f] for f in FIELDS} for m, u in st["byModel"].items()}}
    return {"total": sum(grand.values()), **{SHORT[f]: grand[f] for f in FIELDS},
            "byModel": {m: {"total": sum(u.values()), **{SHORT[f]: u[f] for f in FIELDS}} for m, u in by_model.items()},
            "byStage": stages}


def fmt(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--session", nargs="*", help="session ids to count (default: every session that mentions the run)")
    ap.add_argument("--projects-dir", default=os.path.join(Path.home(), ".claude", "projects"))
    args = ap.parse_args()
    run = Run(Path(args.run))
    totals, per_session = collect(Path(args.projects_dir).expanduser(), run.root, args.session)
    used = list(per_session)
    if not used:
        info(f"USAGE: no Claude Code session in {args.projects_dir} mentions {run.root}; pass --session <id>")
        return 1
    report = {"sessions": used, **summarize(totals), "orchestratorBySession": per_session}
    write_json(run.path("usage.json"), report)
    run.log_step("usage", "ok", 0, total=report["total"], output=report["output"], sessions=len(used))

    est = (json.loads(run.path("shards", "plan.json").read_text(encoding="utf-8")).get("estimate") or {}) \
        if run.path("shards", "plan.json").exists() else {}
    print(f"{'stage':<13}{'agents':>7}{'total':>10}{'cacheRead':>11}{'cacheWrite':>11}{'input':>9}{'output':>9}")
    for stage, s in report["byStage"].items():
        print(f"{stage:<13}{s['agents']:>7}{fmt(s['total']):>10}{fmt(s['cacheRead']):>11}"
              f"{fmt(s['cacheWrite']):>11}{fmt(s['input']):>9}{fmt(s['output']):>9}")
    print(f"{'TOTAL':<13}{'':>7}{fmt(report['total']):>10}{fmt(report['cacheRead']):>11}"
          f"{fmt(report['cacheWrite']):>11}{fmt(report['input']):>9}{fmt(report['output']):>9}")
    for model, u in sorted(report["byModel"].items(), key=lambda x: -x[1]["total"]):
        print(f"  {model}: {fmt(u['total'])} (output {fmt(u['output'])})")
    print("Chat sessions counted (orchestrator tokens; drop one by passing --session with the others):")
    for sid, n in sorted(per_session.items(), key=lambda x: -x[1]):
        print(f"  {sid}: {fmt(n)}")
    if est:
        print(f"Plan estimate for the review stage: {fmt(int(est.get('inputTokens', 0)))} input, "
              f"{est.get('reviewAgents')} agents")
    info(f"USAGE: {fmt(report['total'])} tokens over {len(used)} session(s) -> usage.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
