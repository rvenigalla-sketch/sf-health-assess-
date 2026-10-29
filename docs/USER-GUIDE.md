# sf-health user guide

Step-by-step instructions for running a Salesforce org health assessment with sf-health, from a
fresh laptop to the client report. For how the tool works inside, see [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

**Who this is for:** A360 consultants who know Salesforce. You don't need to know Python or Claude
Code internals; every command you type is listed here.

**Time:** about 30 minutes to set up (once), then per assessment roughly 1–5 hours of mostly
unattended running, plus your own time reviewing findings in a spreadsheet.

---

## Contents

1. [One-time setup](#1-one-time-setup)
2. [Before each assessment](#2-before-each-assessment)
3. [Run the assessment](#3-run-the-assessment)
4. [Review the findings](#4-review-the-findings)
5. [Produce the report](#5-produce-the-report)
6. [After the assessment](#6-after-the-assessment)
7. [Following progress](#7-following-progress)
8. [Resuming and re-running](#8-resuming-and-re-running)
9. [Troubleshooting](#9-troubleshooting)
10. [Rules you must follow](#10-rules-you-must-follow)
11. [Updating the plugin](#11-updating-the-plugin)

---

## 1. One-time setup

Do this once per laptop. Each step ends with a check command; don't move on until it works.

### 1.1 Salesforce CLI

Install the Salesforce CLI (`sf`) from https://developer.salesforce.com/tools/salesforcecli.

```
sf --version
```

### 1.2 sf plugins

Org Check and hardis are unsigned, so allowlist them first. Create this file:

- Windows: `%LOCALAPPDATA%\sf\unsignedPluginAllowList.json`
- macOS / Linux: `~/.config/sf/unsignedPluginAllowList.json`

```json
["@orgcheck/sfdx-plugin", "sfdx-hardis"]
```

Then install the three plugins:

```
sf plugins install code-analyzer
sf plugins install @orgcheck/sfdx-plugin
sf plugins install sfdx-hardis
sf plugins
```

The last command should list all three.

### 1.3 Python and Java

- **Python 3.10 or newer**: https://www.python.org/downloads/ (on Windows, tick "Add python.exe to PATH").
- **Java 11 or newer** (Code Analyzer needs it): e.g. https://adoptium.net/.

```
python --version
java -version
pip install pyyaml openpyxl python-docx
```

### 1.4 Windows only: long file paths

Salesforce file names can be long. If Windows long paths are off, some files are skipped (the run
still works, and lists them). To turn long paths on, run PowerShell **as administrator**:

```
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
```

If you can't get admin rights, use a short run folder instead (`workspaceRoot: C:/hr` in the client config).

### 1.5 Claude Code and the plugin

1. Install Claude Code (VS Code extension or CLI) and sign in with your A360 Claude account.
2. Install the plugin. In Claude Code, type:
   ```
   /plugin marketplace add https://github.com/rvenigalla-sketch/sf-health-assess-.git
   /plugin install sf-health@a360-salesforce
   ```
3. Run `/reload-plugins` (or open a new session).
4. Check: type `/sf-health:` and you should see `assess` and `finalize`.

If the repository is private, Claude Code uses your own git login to download it. Run
`gh auth login` and then `gh auth setup-git` once, or ask the plugin owner for read access.

> The plugin **must** be installed this way. The AI reviewers run as the plugin's own read-only
> agents. Without the plugin installed, the review refuses to start, on purpose.

---

## 2. Before each assessment

### 2.1 Agree the data scope with the client

sf-health never reads business records (accounts, contacts, cases and so on). It reads:

- **metadata**: code, flows, objects, fields, permissions, settings
- with `dataPolicy: full`, also **org checks**: Security Health Check, licence and user usage, limits,
  and recent Apex error messages (record IDs and quoted values are removed from these). These include
  **usernames** of users with powerful permissions or unused licences.

The full statement, including what is sent to Claude, is in [DATA-HANDLING.md](DATA-HANDLING.md).

If the client was promised "metadata only", you **must** use `dataPolicy: metadata-only`.

### 2.2 Log in to the org

```
sf org login web -a <alias>
```

Use a read-only or System Administrator user that the client approved. For a sandbox, add
`--instance-url https://test.salesforce.com`. Check it with `sf org display -o <alias>`.

### 2.3 Make a client config

Copy [config/client.example.yml](../config/client.example.yml) to e.g. `acme.yml`, then set:

| Setting | When |
|---|---|
| `dataPolicy: metadata-only` | The client was told "metadata only" |
| `dataPolicy: full` | Full assessment agreed |
| `tools.orgCheck.acceptTerms: true` | Production org with `dataPolicy: full` |
| `library.enabled: false` | The contract rules out keeping even anonymised decisions locally |

### 2.4 Make a working folder

Use an empty folder **outside** any git repository, for example `C:\Assessments`. Runs are written
to `health-runs/<Client>/<date>/` inside it. Open that folder in VS Code / Claude Code.

---

## 3. Run the assessment

In Claude Code, from your working folder:

```
/sf-health:assess "<org alias>" "<Client name>" --config acme.yml
```

Example: `/sf-health:assess "acme-prod" "Acme" --config acme.yml`

What happens:

| Stage | What it does | Uses Claude | Rough time |
|---|---|---|---|
| Confirm | Claude repeats the org, client, config and data policy and asks you to confirm | – | – |
| 1/8 Collect | Checks your setup, then a **read-only** retrieve of the org's metadata, then the analysis tools | No | 20 min – 2.5 h |
| 2/8 Index | Builds the inventory, automation map and rule checks | No | Minutes |
| 3/8 Plan | Splits the org into review batches and **estimates the Claude usage** | No | < 1 min |
| **Go-ahead** | **Claude stops and asks before spending any Claude usage.** Check the estimate first | – | – |
| 4/8 Review | One read-only AI reviewer per batch, in parallel | Yes | Stages 4–6 together: 30 min – 4 h, depending on org size |
| 5/8 Follow-up | Up to 20 targeted cross-component checks | Yes | (included above) |
| 6/8 Verify | Each High/Critical finding re-checked by a skeptical second agent | Yes | (included above) |
| 7/8 Score | Scores and a comparison with the last run of the same org | No | < 1 min |
| 8/8 Review sheet | Writes the spreadsheet for you to review. **The run stops here** | No | < 1 min |

You can leave it running. Keep the laptop awake and plugged in, and don't close Claude Code.

At the end Claude tells you where the review sheet is and shows the tokens used so far.

---

## 4. Review the findings

Open `health-runs/<Client>/<date>/review/findings_review.xlsx`. One row per finding.

**Nothing reaches the client report without your decision.** For **every** row, fill in **Decision**:

| Decision | Meaning | Also fill in |
|---|---|---|
| `Accept` | Correct as written | – |
| `Edit` | Real, but needs changes | Any of **New severity**, **Edited title**, **Edited recommendation** |
| `Reject` | Wrong or not relevant | **Reject reason** (helps the next consultant) |

Columns that help you decide:

- **Evidence**: the exact file and line the finding is based on. Every quote has already been checked
  against the files by a script. Open the file under `metadata/force-app/...` if you need context.
- **Verification**: for High/Critical, what the second agent concluded (confirmed, downgraded,
  refuted). A refuted finding is shown so you can see it, not because it should go in the report.
- **Baseline**: whether the finding is new or was there last time.
- **Library hint**: how similar findings were decided on earlier assessments. It is a hint only.
- **rule check** rows were made by a script from exact facts, not by AI.

Don't delete rows, change IDs or add sheets. Save the file (keep it `.xlsx`).

---

## 5. Produce the report

```
/sf-health:finalize health-runs/<Client>/<date>
```

Optional: `--client "Acme Corporation"` for the display name on the report.

Claude imports your decisions, writes the executive summary and phased roadmap (Claude, a few
minutes), and renders the outputs into `health-runs/<Client>/<date>/report/`:

| File | What it is | Who it's for |
|---|---|---|
| `report.docx` | The client report | Client (review and brand-check before sending) |
| `report.html` | Interactive dashboard; opens in any browser, can be attached | Client / internal |
| `baseline.xlsx` | The org as it was on this date: findings, data model, security and access, users, automation map, metrics | Client and the team doing the fixes |

If it says some rows have no decision, either finish the sheet and run finalize again, or confirm
you want them left out.

Changed your mind about a decision? Edit the sheet and run `/sf-health:finalize` again; the summary
and reports are rebuilt.

**Always read the Word report before it goes to the client.** The summary is AI-written from your
accepted findings; check the wording.

---

## 6. After the assessment

- **Keep the run folder.** The next assessment of this org compares against it (new / fixed /
  unchanged findings). Without it, the next run has nothing to compare with. The `baseline.xlsx`
  alone is not enough.
- **Store it where client data belongs** (the client's SharePoint / Drive folder), not in git, not on
  a personal drive. A run folder can be several hundred MB; zip it.
- **Never commit a run folder** to any repository.

---

## 7. Following progress

You always see which stage it's on, as **[Stage N/8]**, with a result line when each stage finishes.

- **Collect** shows a percentage as each retrieve batch and tool finishes:
  ```
  PROGRESS collect 7/22 31% [######--------------] retrieved chunk_05
  ```
- **Review, follow-up, verify** run in the background. Type `/workflows` to watch them live; each
  agent that finishes adds a line like `PROGRESS review 12/25 48% [#########-----------] review:apex-03`.
- Ask Claude "where is it?" at any time.

---

## 8. Resuming and re-running

If the run stopped (laptop slept, usage limit, error), resume from a stage on the **same** run folder:

```
/sf-health:assess "<alias>" "<Client>" --date 2026-10-21 --from review
```

Stages you can resume from: `index`, `plan`, `review`, `followup`, `verify`, `score`, `gate`.
Collection is never repeated unless you ask; a new retrieve is a new snapshot, so start a new date.

**Interim re-check** (not a client deliverable): ask Claude for an "incremental" run. It reviews
only what changed since the last run and carries the rest forward. A client assessment is always a
full review.

**Large orgs**: on a Claude subscription a big run can pause at usage limits. Split it by area, e.g.
`--areas apex,flow` then `--areas security,datamodel`.

---

## 9. Troubleshooting

| Message | What to do |
|---|---|
| `PREFLIGHT: sf plugin missing: ...` | Install the plugin it names (section 1.2) |
| `PREFLIGHT: Org '...' not authenticated` | `sf org login web -a <alias>` |
| `PREFLIGHT: Low API headroom` | The org's daily API calls are low. Run tomorrow, or after hours |
| `PREFLIGHT warning: Windows long paths are off` | Section 1.4, or `workspaceRoot: C:/hr` |
| `TOOL orgcheck:...: FAILED` on production | Set `tools.orgCheck.acceptTerms: true` in the client config |
| `RETRIEVE: failed chunks [...]` | The run continues with what it got. Re-run on a quieter time if many chunks failed |
| `sf-health requires its read-only agent types` | The plugin isn't installed (section 1.5). Don't work around this |
| Review stage: shards with no output | Claude retries once automatically. If it persists, resume with `--from review` |
| `REVIEW: N finding(s) still have no decision` | Fill in Decision for those rows, or confirm leaving them out |
| `REVIEW: these IDs appear on more than one row` | Remove the duplicate row in the sheet |
| `RENDER: stale summary` | Claude re-runs the summary automatically; if asked, say yes |
| Run paused at a usage limit | Wait for the limit to reset, then resume with `--from <stage>` |
| `/sf-health:assess` not found | `/reload-plugins`, or reinstall (section 1.5) |

Every step is logged in `health-runs/<Client>/<date>/run_log.json`. Send that file (not the
whole run folder) when asking for help.

---

## 10. Rules you must follow

- Only run sf-health on orgs the client has approved, with the data policy agreed with them.
- sf-health only retrieves and queries. It never deploys, writes data, assigns permissions or runs
  tests in the org. Don't modify it to do so.
- Don't paste org files or findings into a normal Claude chat. The tool controls what the AI reads.
- Don't edit files under `metadata/`, `index/` or `findings/raw/` by hand.
- Don't send a report without consultant review. (`accept-all` exists for testing; such reports are
  marked DRAFT.)
- Keep run folders out of git and on client-approved storage.

---

## 11. Updating the plugin

Updates are not automatic. When a new version is announced:

```
claude plugin update sf-health@a360-salesforce
```

(or in Claude Code: `/plugin` → **Installed** → sf-health → **Update now**), then `/reload-plugins`.

To get updates automatically: `/plugin` → **Marketplaces** → `a360-salesforce` → **Enable auto-update**.

Check your version in `/plugin` → **Installed**. Changes are listed in [CHANGELOG.md](../CHANGELOG.md).
