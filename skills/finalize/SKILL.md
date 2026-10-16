---
name: finalize
description: Finish an sf-health assessment after the consultant review sheet is filled in - import decisions, write the executive summary and roadmap, and render the HTML dashboard, Word report and Excel baseline. Use when the user says the review sheet is done or asks to produce the report.
disable-model-invocation: true
argument-hint: "<run folder> [--client \"Display Name\"] [--allow-pending] [--config file.yml]"
---

# sf-health: finalize

`PLUGIN` = two folders above this skill's base directory. `RUN` = the run folder the user gives (or the
newest folder under `./health-runs/<Client>/`).

1. **Import decisions**: `python "$PLUGIN/scripts/review_gate.py" import --run RUN [--config C]`
   - Exit 3 means some rows have no decision. Tell the user how many, and stop. Continue only if
     they explicitly accept leaving those out (then re-run with `--allow-pending`).
   - Exit 2 means a row has an invalid edit. Show the message.
2. **Summary and roadmap**:
   - `python "$PLUGIN/scripts/workflow_args.py" synthesize --run RUN`
   - Read `$PLUGIN/workflows/synthesize.js` and run the Workflow tool with its full text as `script`
     (inline; `scriptPath` only accepts files inside the working folder) and those args as an object.
   - The journal is `<Transcript dir from the launch result>/journal.jsonl` (fallback:
     `workflow_args.py journal --run-id <runId>`), then `extract_results.py --journal <path> --run RUN --kind synthesis`
3. **Render**: `python "$PLUGIN/scripts/render.py" --run RUN [--client "<Display Name>"]`
   Report any `RENDER WARNING` lines. They mean the summary cited a finding that doesn't exist, or
   left one out of the roadmap.
4. Give the user the paths:
   - `RUN/report/report.html`: interactive dashboard. Opens in any browser; can be attached.
   - `RUN/report/report.docx`: client report
   - `RUN/report/baseline.xlsx`: baseline workbook
   Offer once to publish the dashboard as a private claude.ai artifact from `RUN/report/report.artifact.html`.
   Only publish if they say yes: it contains the client's org details.
