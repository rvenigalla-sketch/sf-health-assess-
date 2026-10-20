"""Token usage report from Claude Code session logs (scripts/usage_report.py)."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import usage_report as ur  # noqa: E402
from common import Run  # noqa: E402


def reply(msg_id, model, inp, out, cread=0, cwrite=0, text=""):
    return {"type": "assistant", "message": {"id": msg_id, "model": model, "content": text, "usage": {
        "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cread,
        "cache_creation_input_tokens": cwrite}}}


def write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


class UsageReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run = Run(self.tmp / "health-runs" / "Acme" / "2026-10-20")
        self.projects = self.tmp / "projects"
        rp = str(self.run.root)          # json.dumps escapes it the way Claude Code's logs do
        proj = self.projects / "proj"
        write_jsonl(proj / "s1.jsonl", [
            reply("m0", "opus", 1, 1, cread=1000),                      # before the run: not counted
            {"type": "user", "message": {"content": f"run {rp}"}},
            reply("m1", "opus", 10, 5, cread=100),
            reply("m1", "opus", 10, 5, cread=100),                     # same reply, second content block
            {"type": "user", "message": {"content": f"done {rp}"}},
            reply("m2", "opus", 1, 2),                                 # reply right after the last mention
            reply("m3", "opus", 7, 7),                                 # later unrelated work: not counted
        ])
        wf = proj / "s1" / "subagents" / "workflows" / "wf_1"
        write_jsonl(wf / "journal.jsonl", [
            {"type": "started", "agentId": "a1", "label": "review:apex-01"},
            {"type": "started", "agentId": "a2", "label": "review:followup-01"},
            {"type": "started", "agentId": "a3", "label": "verify:apex-01#1"},
            {"type": "started", "agentId": "a4", "label": "synthesize"}])
        prompt = {"type": "user", "message": {"content": f"Read {rp}/shards/x.json"}}
        write_jsonl(wf / "agent-a1.jsonl", [prompt, reply("r1", "sonnet", 100, 10, cread=900)])
        write_jsonl(wf / "agent-a2.jsonl", [prompt, reply("r2", "sonnet", 50, 5)])
        write_jsonl(wf / "agent-a3.jsonl", [prompt, reply("r3", "opus", 40, 4)])
        write_jsonl(wf / "agent-a4.jsonl", [prompt, reply("r4", "opus", 30, 3)])
        write_jsonl(wf / "agent-a9.jsonl", [reply("r9", "opus", 999, 9)])  # another run's agent
        write_jsonl(self.projects / "other" / "s2.jsonl", [reply("x", "opus", 5, 5)])  # unrelated chat

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stages_window_and_dedupe(self):
        totals, per_session = ur.collect(self.projects, self.run.root, None)
        rep = ur.summarize(totals)
        self.assertEqual(list(per_session), ["s1"])
        st = rep["byStage"]
        self.assertEqual({k: v["agents"] for k, v in st.items()},
                         {"review": 1, "followup": 1, "verify": 1, "synthesize": 1, "orchestrator": 0})
        self.assertEqual(st["review"]["total"], 1010)
        self.assertEqual(st["orchestrator"]["total"], 115 + 3)    # m1 once + m2; not m0, m3
        self.assertEqual(rep["total"], 1010 + 55 + 44 + 33 + 118)
        self.assertEqual(rep["byModel"]["sonnet"]["total"], 1065)

    def test_named_session_counts_whole_chat(self):
        totals, _ = ur.collect(self.projects, self.run.root, ["s1"])
        self.assertEqual(ur.summarize(totals)["byStage"]["orchestrator"]["total"], 2 + 1000 + 115 + 3 + 14)


if __name__ == "__main__":
    unittest.main()
