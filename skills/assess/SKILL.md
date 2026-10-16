---
name: assess
description: Run an sf-health Salesforce org health assessment end to end (collect, index, plan, multi-agent review, follow-up, verify, score, consultant review sheet). Use when the user asks to run or resume a health assessment / health check of a Salesforce org.
disable-model-invocation: true
argument-hint: "<org alias> <client name> [--config file.yml] [--date YYYY-MM-DD] [--areas a,b] [--from stage]"
---

# sf-health: assess

You orchestrate the assessment. Scripts do every count, parse and check; reviewer agents run inside
workflows. **You never read org source files or finding bodies yourself.** Keep your context small:
run commands, read their one-line summaries, and report progress.

## Paths and setup

- `PLUGIN` = the plugin root: two folders above this skill's base directory (the folder that has
  `scripts/`, `workflows/` and `agents/`). Use forward slashes.
- Run every script as `python "$PLUGIN/scripts/<script>.py" ...` from the user's working directory.
  Runs are written to `./health-runs/<Client>/<date>/` (config `workspaceRoot`).
- `RUN` = the run folder printed by collect.py (`{"run": "..."}`).
- Pass `--config <file>` to every script when the user gave one, e.g. a client file with
  `dataPolicy: metadata-only` for clients promised "metadata only".

Before starting, confirm with the user in one message:
- the org alias and client name
- the config file and its `dataPolicy`
- that a read-only retrieve of the org is OK

If they already confirmed these in the conversation, don't ask again.

## Stages

Run in order. After each one, give the user one short progress line. If a script exits non-zero,
stop, show its last lines and say what to do. Never skip a stage silently.

1. **Collect**: `collect.py --org "<alias>" --client "<Client>" [--date D] [--config C]`
   Preflight problems stop the run; show them. Tool failures are logged and the run continues.
   Report which tools failed (from `RUN/run_log.json`).
2. **Index**: `build_index.py --run RUN [--config C]`
3. **Plan**: `plan_shards.py --run RUN [--areas ...] [--config C]`
   Show the user the plan: shards per area, estimated review tokens, and any over-budget shards.
   **Ask for a go-ahead before stage 4.** This is the first step that uses Claude usage
   (roughly 2–3× the review estimate for the whole run).
4. **Review**:
   - `ARGS = workflow_args.py review --run RUN` (prints JSON).
   - Read `$PLUGIN/workflows/review.js` and call the Workflow tool with its full text as `script`
     (inline; `scriptPath` only accepts files inside the working folder) and `args` = that JSON,
     passed as an object, not a string.
   - When it completes, the journal is `<Transcript dir from the launch result>/journal.jsonl`.
     If you don't have it: `workflow_args.py journal --run-id <runId>`.
   - `extract_results.py --journal <journal> --run RUN --kind review`
   - `validate_findings.py --run RUN`
   - Report: findings passed / rejected, and any shard without output. If shards have no output,
     resume the same workflow (`resumeFromRunId`) once, extract again and re-validate.
5. **Follow-up** (one round only):
   - `plan_followups.py --run RUN`. If it planned 0 shards, skip to stage 6.
   - Otherwise `workflow_args.py followup --run RUN`, run `review.js` (inline, as above) with those args, extract with
     `--kind followup`, then `validate_findings.py --run RUN` again.
6. **Verify**:
   - `workflow_args.py verify --run RUN`. If it lists no findings, skip.
   - Otherwise run `$PLUGIN/workflows/verify.js` (inline, as above) with those args and extract with `--kind verify`.
7. **Score and compare**: `score.py --run RUN`, then `diff_baseline.py --run RUN --auto`.
8. **Consultant review sheet**: `review_gate.py export --run RUN`
   Tell the user where `RUN/review/findings_review.xlsx` is. Ask them to set Accept / Reject / Edit
   for every row, then run `/sf-health:finalize <RUN>`.
   Also give a short summary from `score.py`'s output line: counts by severity, refuted count and the provisional overall score.

## Resuming

`--from <stage>` (index, plan, review, followup, verify, score, gate) starts from that stage on an
existing run (pass `--date` to reuse the folder). Collection is never repeated unless asked: a new
retrieve means a new snapshot.

## Rules

- Never run anything that writes to the org. Collection is retrieve and query only, and never `sf check run-all-tests`.
- Never edit files under `RUN/findings/raw`, `RUN/index` or `RUN/metadata` by hand.
- Do not paste findings into chat. The consultant reviews them in the spreadsheet.
- If the user asks for a draft without consultant review, `review_gate.py accept-all` exists, but the
  report will be marked DRAFT. Say so.
