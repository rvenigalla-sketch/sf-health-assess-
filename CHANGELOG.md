# Changelog

Every release bumps `version` in `.claude-plugin/plugin.json`; installed copies only update when it
changes. Update with `claude plugin update sf-health@a360-salesforce`.

## 0.3.0 (2026-10-29)

- Progress while running. Stages are shown as `[Stage N/8]`. Collection prints a percentage and bar
  as each retrieve chunk and tool finishes. Review, follow-up and verify log one progress line per
  finished agent, visible live in `/workflows`. Collection now runs in the background, so long
  retrieves no longer hit the command timeout.
- Documentation: a new [user guide](docs/USER-GUIDE.md) (setup to report, troubleshooting, rules)
  and a [how it works](docs/HOW-IT-WORKS.md) reference. The README is now a short overview.
- `config/client.example.yml`: a starting point for client configs.
- [Data handling](docs/DATA-HANDLING.md) statement (draft, for security and legal sign-off).
- GitHub Actions run the unit tests on Windows, macOS and Linux for every push and pull request.
- Issue forms for bug reports and pilot feedback.
- Client names removed from code comments and the default config.

## 0.2.2 (2026-10-20)

- `usage_report.py`: the actual Claude tokens each run used, per stage and model, from Claude Code's
  local logs. Run by both commands and saved as `usage.json`.

## 0.2.1 (2026-10-20)

- Removed the personal-data redaction added in 0.2.0. The scope stays metadata and read-only org
  checks; business records are never read.

## 0.2.0 (2026-10-20)

- Review fixes: read-only agent types are required, stale results are cleared on re-runs, profile
  access is retrieved correctly, and the review gate and finding library were hardened.
- Accuracy-first defaults; redesigned A360-branded dashboard.
- Incremental interim re-checks, script-made rule checks, local finding library, faster collection.

## 0.1.0 (2026-10-17)

- First version: collection, index, sharded multi-agent review, follow-up, verification, scoring,
  baseline comparison, consultant review sheet, HTML / Word / Excel outputs.
