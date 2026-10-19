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
- Client config: when the user gave one (e.g. a client file with `dataPolicy: metadata-only` for
  clients promised "metadata only"), pass `--config <file>` at the first step, collect. The scripts
  save it as `RUN/client-config.yml` and reuse it in every later step of that run, so later steps may
  omit `--config`. Passing `--config` again replaces the saved copy; only do that if the user asks.
  On `--from` resumes the saved copy is used automatically.

Before starting, confirm with the user in one message:
- the org alias and client name
- the config file and its `dataPolicy`
- that a read-only retrieve of the org is OK
- whether the org holds health data (PHI) or other regulated personal data; if so, and no client
  config was given, use `--config "$PLUGIN/config/hipaa.yml"`

If they already confirmed these in the conversation, don't ask again.

## Stages

Run in order. After each one, give the user one short progress line. If a script exits non-zero,
stop, show its last lines and say what to do. Never skip a stage silently.

1. **Collect**: `collect.py --org "<alias>" --client "<Client>" [--date D] [--config C]`
   Preflight problems stop the run; show them. Tool failures are logged and the run continues.
   Report which tools failed (from `RUN/run_log.json`).
2. **Index**: `build_index.py --run RUN` (first redacts emails, phone numbers and SSN-shaped numbers in
   the collected files, so every AI stage reads redacted text; health-data clients use `config/hipaa.yml`)
   Then **rule checks**: `rule_checks.py --run RUN` (script-made findings, no Claude usage).
3. **Plan**: `plan_shards.py --run RUN --baseline none [--areas ...]`
   - Planning clears this run's stale raw reviewer findings and verifier verdicts from any earlier
     attempt, so a re-run of the same run folder starts the review, follow-up and verify stages clean.
   - Default is a **full review**. Always use it for any assessment that goes to a client.
   - Only if the user explicitly asks for a quick interim re-check of an org assessed before, use
     `--baseline auto`. It reviews only what changed since the previous run, plus anything that
     calls it or uses a changed object or field, and carries the other findings forward. Say that
     this is an interim check, not a full assessment.
   Show the user the plan: shards per area, estimated review tokens, any over-budget shards and,
   for an incremental run, `incremental` (changed components, findings carried forward, full-plan shard count).
   If it planned 0 shards (nothing changed), skip the review and follow-up workflows, but still run
   `validate_findings.py --run RUN` (it checks the rule-check and carried-forward findings), then go on to stage 6.
   Also report the privacy line from the index step (values redacted, from `RUN/privacy.json`): counts only.
   If `privacy.json` says `"redact": false`, say clearly that AI stages will read unredacted values.
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
   - If the workflow fails with "sf-health requires its read-only agent types", stop: the plugin is not
     installed (or `models.useAgentTypes` is false). Tell the user; never work around it.
   - If the result's `idFixes` is non-empty, an agent returned the wrong id; tell the user which ones.
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
   for every row, then run `/sf-health:finalize <RUN>`. **Stop here.** Do not finalize or accept
   findings on the consultant's behalf.
   Also give a short summary from `score.py`'s output line: counts by severity, refuted count and the provisional overall score.

## Resuming

`--from <stage>` (index, plan, review, followup, verify, score, gate) starts from that stage on an
existing run (pass `--date` to reuse the folder). The run's saved `client-config.yml` is reused,
so `--config` is not needed. Collection is never repeated unless asked: a new retrieve means a new
snapshot. Re-running from `plan` (or earlier) re-plans and clears stale raw findings and verdicts, so
the review, follow-up and verify stages run again in full; the go-ahead before stage 4 still applies.

## Rules

- Never run anything that writes to the org. Collection is retrieve and query only, and never `sf check run-all-tests`.
- Never edit files under `RUN/findings/raw`, `RUN/index` or `RUN/metadata` by hand.
- Do not paste findings into chat. The consultant reviews them in the spreadsheet.
- If the user asks for a draft without consultant review, `review_gate.py accept-all` exists, but the
  report will be marked DRAFT. Say so.
