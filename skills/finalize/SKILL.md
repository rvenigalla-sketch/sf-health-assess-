---
name: finalize
description: Finish an sf-health assessment after the consultant review sheet is filled in - import decisions, write the executive summary and roadmap, and render the HTML dashboard, Word report and Excel baseline. Use when the user says the review sheet is done or asks to produce the report.
disable-model-invocation: true
argument-hint: "<run folder> [--client \"Display Name\"] [--allow-pending]"
---

# sf-health: finalize

`PLUGIN` = two folders above this skill's base directory. `RUN` = the run folder the user gives (or the
newest folder under `./health-runs/<Client>/`).

**Config:** the client config given to `/sf-health:assess` was saved as `RUN/client-config.yml`, and every
script below reuses it automatically (library opt-out, scoring thresholds, models). Don't pass `--config`
again. Only if the user explicitly wants to change a setting now, pass `--config <file>` to the first
script; that replaces the saved copy for the rest of the run.

1. **Import decisions**: `python "$PLUGIN/scripts/review_gate.py" import --run RUN`
   - Exit 3 means some findings have no decision, or their row was deleted from the sheet. Tell the
     user how many, and stop. Continue only if they explicitly accept leaving those out (then re-run
     with `--allow-pending`).
   - Exit 2 means the sheet has a problem: an ID on more than one row, an unknown decision or an
     invalid severity. Show the message; the user fixes the sheet and you import again.
   - Show any `REVIEW NOTE` lines (e.g. an Accept row with edits imported as Edit, edits on a Reject
     row ignored).
2. **Summary and roadmap** (always re-run after an import that changed decisions):
   - `python "$PLUGIN/scripts/workflow_args.py" synthesize --run RUN`
   - Read `$PLUGIN/workflows/synthesize.js` and run the Workflow tool with its full text as `script`
     (inline; `scriptPath` only accepts files inside the working folder) and those args as an object.
   - The journal is `<Transcript dir from the launch result>/journal.jsonl` (fallback:
     `workflow_args.py journal --run-id <runId>`), then `extract_results.py --journal <path> --run RUN --kind synthesis`
3. **Render**: `python "$PLUGIN/scripts/render.py" --run RUN [--client "<Display Name>"]`
   - Exit 2 (`RENDER: stale summary`) means the summary cites findings that aren't accepted, or was
     written before the latest import. Re-run step 2, then render again. Only if the user explicitly
     wants a report without a fresh summary, add `--allow-stale-synthesis` (citations of findings that
     aren't accepted are dropped) and tell them the summary may not match the findings.
   - Report any `RENDER WARNING` lines (e.g. accepted findings the summary left out of the roadmap).
4. **Usage**: `python "$PLUGIN/scripts/usage_report.py" --run RUN`. It adds up the Claude tokens every
   session spent on this run (review, follow-up, verify, summary, and the chat itself) from Claude Code's
   local logs and writes `RUN/usage.json`. Show its table and the plan's estimate. If a chat that did
   unrelated work is listed with a large total, offer to re-run with `--session` naming only the
   sessions that ran this assessment.
5. Give the user the paths:
   - `RUN/report/report.html`: interactive dashboard. Opens in any browser; can be attached.
   - `RUN/report/report.docx`: client report
   - `RUN/report/baseline.xlsx`: baseline workbook
   Offer once to publish the dashboard as a private claude.ai artifact from `RUN/report/report.artifact.html`.
   Only publish if they say yes: it contains the client's org details.
