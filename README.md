# sf-health

A360's Salesforce org health assessment, packaged as a Claude Code plugin for internal use.

Scripts do everything that can be counted or parsed. Claude reviewer agents each review a small,
bounded part of the org. Every finding must quote the exact lines it is based on, and a script checks
each quote against the files before the finding goes any further. An A360 consultant accepts,
rejects or edits every finding before the client report is produced.

## Install (once per person)

1. **Salesforce CLI** (`sf`). Authenticate the org you will assess (`sf org login web -a <alias>`).
2. **sf plugins**. Org Check and hardis are unsigned, so allowlist them first. On Windows, put this in
   `%LOCALAPPDATA%\sf\unsignedPluginAllowList.json`; on macOS or Linux, in `~/.config/sf/unsignedPluginAllowList.json`:
   ```json
   ["@orgcheck/sfdx-plugin", "sfdx-hardis"]
   ```
   Then run:
   ```
   sf plugins install code-analyzer
   sf plugins install @orgcheck/sfdx-plugin
   sf plugins install sfdx-hardis
   ```
3. **Python 3.10+** with `pip install pyyaml openpyxl python-docx`, and **Java 11+** (Code Analyzer).
4. **The plugin** (in Claude Code):
   ```
   /plugin marketplace add https://github.com/rvenigalla-sketch/sf-health-assess-.git
   /plugin install sf-health@a360-salesforce
   ```
   Restart Claude Code. Then set `models.useAgentTypes: true` in your client config, so reviewers run
   as the plugin's read-only subagents.

## Run an assessment

From an empty working folder (runs are written to `./health-runs/<Client>/<date>/`):

```
/sf-health:assess "<org alias>" "<Client name>" --config client.yml
```

The command runs the stages below. It stops for your go-ahead before the first AI step, and stops
again when the findings are ready for consultant review.

| # | Stage | What runs | Uses Claude? |
|---|---|---|---|
| 1 | Collect | preflight, manifest, chunked read-only retrieve, Code Analyzer, Health Check, hardis | No |
| 2 | Index | inventory, automation map per object, flow / workflow / CRM Analytics digests, field where-used, org facts | No |
| 3 | Plan | shards sized to a token budget (45k) and ≤ 30 components each; cost estimate | No |
| 4 | Review | one read-only reviewer per shard, in parallel, structured JSON output | Yes |
| 4b | Follow-up | ≤ 20 targeted cross-component checks raised by reviewers | Yes |
| 5 | Validate, verify | quotes checked against the files; High/Critical re-checked by a skeptical verifier | Partly |
| 6 | Score, compare | fixed formula; diff against the previous run of the same org | No |
| 7 | Consultant review | `review/findings_review.xlsx`: Accept / Reject / Edit each finding | No (you) |
| 8 | Finalize | `/sf-health:finalize <run>`: summary and roadmap, HTML dashboard, Word report, Excel baseline | Summary only |

Outputs in `<run>/report/`:
- `report.html`: interactive dashboard
- `report.docx`: client report
- `baseline.xlsx`: findings, data model, security and access, user access, automation map

Rough usage per run (calibrate from each run's `run_log.json`): small org (~500 components) about 4–6M
tokens; large org (5,000+) about 30–40M tokens, possibly spread over usage-limit resets.
The verifier (opus, ~40k tokens per finding) is usually the largest step; `verify.maxFindings` caps it.

### Pilot results (synthetic fixture, 2026-10-17)

| Step | Agents | Tokens | Time |
|---|---|---|---|
| Review | 7 | 293k | 33 s |
| Follow-up | 3 | 144k | 40 s |
| Verify (opus) | 18 | 746k | 59 s |
| Summary (opus) | 1 | 71k | 81 s |

- **Quotes:** 31 of 31 findings passed the quote check; none were rejected.
- **Accuracy:** recall 100% (15 of 15 planted problems found) and precision 100%. The clean class got no High or Critical findings.
- **Verifier:** confirmed 11 findings, downgraded 6 and refuted 1. The refuted one was a recursion claim that couldn't happen, because the code crashes first. A follow-up finding had already reported that crash.
- **Answer-key update:** three problems in the key (P13–P15) were real fixture issues that the AI found first; they were added to the key afterwards.

## Client config

Copy and edit, then pass with `--config`:

```yaml
dataPolicy: metadata-only     # or full
tools:
  orgCheck:
    acceptTerms: true         # Org Check's production-org acknowledgement; needed for production orgs
models:
  useAgentTypes: true         # once the plugin is installed
```

- `full`: every tool, including the org-querying ones (Org Check, hardis, Security Health Check).
- `metadata-only`: retrieved metadata and Code Analyzer only. **Use this whenever the client was told
  "metadata only".**

Other defaults (see `config/default.yml`):
- Setup Audit Trail is off.
- Runtime error text has record IDs and quoted values removed.
- Managed-package components are skipped.

## Safety rules

- Collection only retrieves and queries. Never: deploy, data writes, permission assignment, or
  `sf check run-all-tests` (that runs Apex tests in the org).
- Reviewer agents are read-only, and are told to treat org files as data, not instructions.
- Client data stays in `health-runs/` (git-ignored). Never commit a run folder.

## Development

```
python tests/make_fixture.py                   # synthetic seeded org (no client data)
python -m unittest discover -s tests -v        # deterministic pipeline tests
python tests/eval.py --run <fixture run>       # recall/precision of an AI review of the fixture
```

Layout:
- `scripts/`: every deterministic stage
- `workflows/`: review, verify and synthesize fan-outs
- `agents/`: reviewer instructions and checklists
- `rubrics/common.md`: rules shared by all reviewers
- `skills/`: the `/sf-health:assess` and `/sf-health:finalize` commands
- `templates/report.html`: the dashboard
