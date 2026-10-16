# sf-health

A360's Salesforce org health assessment, packaged as a Claude Code plugin.

Scripts do everything that can be counted or parsed. Claude subagents review small, bounded shards of the org.
Every finding must cite a file, a line range and an exact quote, and a script checks each one before it reaches the report.

## Stages

| # | Stage | Script / agent | LLM? |
|---|---|---|---|
| 1 | Collect: preflight, manifest, chunked retrieve, tools | `scripts/collect.py` | No |
| 2 | Index: inventory, automation map, flow digests, evidence, metrics | `scripts/build_index.py` | No |
| 3 | Shard plan | `scripts/plan_shards.py` | No |
| 4 | Review (+ one bounded follow-up round) | `workflows/assess.js` + `agents/*-reviewer.md` | Yes |
| 5 | Validate, verify, score, synthesize | `scripts/validate_findings.py`, `scripts/score.py`, verifier + synthesizer agents | Partly |
| 6 | Consultant review gate | `review/findings_review.xlsx` | No |
| 7 | Render: HTML dashboard, Word, Excel baseline | `scripts/render_*.py` | No |

## Requirements

- Salesforce CLI (`sf`), with plugins: `code-analyzer`, `@orgcheck/sfdx-plugin`, `sfdx-hardis`
  - Org Check and hardis are unsigned. Add them to `%LOCALAPPDATA%\sf\unsignedPluginAllowList.json`
    (Windows) or `~/.config/sf/unsignedPluginAllowList.json` before `sf plugins install`.
- Python 3.10+ with `pyyaml openpyxl python-docx`
- Java 11+ (Code Analyzer PMD engine)

## Run stage 1-2 by hand

```bash
cd scripts
python collect.py --org "<org alias>" --client "<Client>"      # writes ./health-runs/<Client>/<date>/
python build_index.py --run ./health-runs/<Client>/<date>
```

To resume a run, pass `--date <YYYY-MM-DD>` with `--steps` for the steps still to do.

## Data policy

`config/default.yml` -> `dataPolicy`:
- `full`: every tool, including org-querying tools (Org Check, hardis, Security Health Check).
- `metadata-only`: retrieved metadata + Code Analyzer only. Use this when the client was promised
  "metadata only".

Setup Audit Trail is off by default. Runtime error text has record IDs and quoted values removed
before any agent sees it.

## Never run

- `sf check run-all-tests` (runs Apex tests in the org)
- any deploy, data write, or permission-assign command. Collection is retrieve and query only.
