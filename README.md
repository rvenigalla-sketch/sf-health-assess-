# sf-health

A360's Salesforce org health assessment, packaged as a Claude Code plugin for internal use.

Point it at a Salesforce org and it produces a scored, evidence-backed health report: a Word report
for the client, an interactive HTML dashboard, and an Excel baseline of the org. It covers Apex,
flows and automation, security and access, the data model, CRM Analytics, integrations and release
readiness.

- **Read-only.** It retrieves metadata and runs read-only checks. It never deploys, writes data or
  reads business records.
- **Evidence-backed.** Every finding quotes the exact lines it is based on, and a script checks each
  quote against the files. High and Critical findings are re-checked by a second, skeptical agent.
- **Consultant in control.** Nothing reaches the client report until an A360 consultant has accepted,
  edited or rejected each finding.

## Quick start

Setup, prerequisites and every step are in the **[user guide](docs/USER-GUIDE.md)**. In short:

1. One-time setup: Salesforce CLI with three plugins, Python 3.10+, Java 11+, and this plugin
   ([guide, section 1](docs/USER-GUIDE.md#1-one-time-setup)):
   ```
   /plugin marketplace add https://github.com/rvenigalla-sketch/sf-health-assess-.git
   /plugin install sf-health@a360-salesforce
   ```
2. Log in to the org (`sf org login web -a <alias>`) and copy
   [config/client.example.yml](config/client.example.yml) to a client config.
3. From an empty working folder, run:
   ```
   /sf-health:assess "<org alias>" "<Client name>" --config <client>.yml
   ```
   It shows `[Stage N/8]` and a percentage as it goes, and asks before it uses any Claude usage.
4. Fill in Accept / Reject / Edit for every row of `health-runs/<Client>/<date>/review/findings_review.xlsx`.
5. Run `/sf-health:finalize health-runs/<Client>/<date>` for the report, dashboard and baseline.

## Documentation

| Document | For |
|---|---|
| [User guide](docs/USER-GUIDE.md) | Consultants: setup, running, reviewing, reporting, troubleshooting |
| [How it works](docs/HOW-IT-WORKS.md) | Maintainers and reviewers: pipeline, accuracy, cost, safety, development, releases |
| [Data handling](docs/DATA-HANDLING.md) | Security, legal and clients: what is read, sent to Claude and stored |
| [Changelog](CHANGELOG.md) | What changed in each version |
| [config/default.yml](config/default.yml) | Every setting, with comments |

## Updating

```
claude plugin update sf-health@a360-salesforce
```

Then `/reload-plugins`. See the [user guide, section 11](docs/USER-GUIDE.md#11-updating-the-plugin).

## Support

Questions, bugs and ideas: open an issue in this repository using the bug or pilot-feedback form.
Attach `run_log.json` only; never a run folder or client files. Owner: Raviteja Venigalla.
