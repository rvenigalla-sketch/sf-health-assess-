# How sf-health works

Technical reference for people maintaining sf-health or answering questions about its accuracy, cost
and safety. To run an assessment, use the [user guide](USER-GUIDE.md).

Scripts do everything that can be counted or parsed. Claude reviewer agents each review a small,
bounded part of the org. Every finding must quote the exact lines it is based on, and a script checks
each quote against the files before the finding goes any further. An A360 consultant accepts,
rejects or edits every finding before the client report is produced.

## Pipeline

| # | Stage | What runs | Uses Claude? |
|---|---|---|---|
| 1 | Collect | preflight, manifest, read-only retrieve (2 chunks at a time), Code Analyzer alongside Health Check / hardis | No |
| 2 | Index, rule checks | inventory with content hashes, automation map per object, flow / workflow / CRM Analytics digests, field where-used, org facts; then script-made findings (flows without fault paths, old API versions, Public Read/Write defaults) | No |
| 3 | Plan | shards sized to a token budget (45k) and ≤ 30 components each; on a repeat run only what changed; cost estimate | No |
| 4 | Review, validate | one read-only reviewer per shard, in parallel, structured JSON output; quotes checked against the files | Yes |
| 5 | Follow-up | ≤ 20 targeted cross-component checks raised by reviewers | Yes |
| 6 | Verify | High/Critical re-checked by a skeptical verifier | Yes |
| 7 | Score, compare | fixed formula; diff against the previous run of the same org | No |
| 8 | Consultant review sheet | `review/findings_review.xlsx`: the consultant sets Accept / Reject / Edit on each finding | No |
| – | Finalize | `/sf-health:finalize <run>`: summary and roadmap, HTML dashboard, Word report, Excel baseline | Summary only |

Progress: scripts print `PROGRESS <stage> done/total pct% [####------] detail` lines
(`common.Progress`); the review and verify workflows log the same line as each agent finishes, visible
in `/workflows`. The assess command relays them with a `[Stage N/8]` header per stage.

## Usage and cost

Usage scales with the amount of code and automation, not the component count: most components are
fields. **Get the real figure for an org before spending anything:** collect, index and plan use no
Claude usage, and the plan prints the reviewer count and review-token estimate.

Rough guide (the review is about 68k tokens per reviewer agent; follow-up adds up to ~1M; verify adds up
to ~2.4M, capped by `verify.maxFindings`):

| Org | Review agents | Total tokens | Collect | AI stages |
|---|---|---|---|---|
| ~65 classes, 200 flows (a UAT sandbox with 5,100 components, as planned) | 25 | 4–5M | ~25 min | 30–45 min |
| ~300 classes, 400 flows | 60–80 | 8–9M | 40–60 min | 1–1.5 h |
| 1,000+ classes, 1,000+ flows | 150–200 | 14–18M | 1.5–2.5 h | 2–4 h |

The rows below the first are extrapolations; recalibrate them from real runs.
`python scripts/usage_report.py --run <run>` reports a run's **actual** tokens per stage and model, from
Claude Code's local session logs (also run by the assess and finalize commands; saved as `<run>/usage.json`).
Most tokens are cache reads, which count for less than fresh input. On a
subscription a large run may pause at usage limits. Split it with `--areas` if needed.

**Accuracy comes first in the defaults.** Every setting below that could trade accuracy for speed is
off by default:
- client assessments are always full reviews
- every verifier is Opus
- reviewers are never told to skip "known false positives"
- every agent runs as a read-only plugin subagent type

## Repeat runs, rule checks and the finding library

- **Repeat runs (incremental, interim checks only).** `plan_shards.py --baseline auto` compares content
  hashes with the client's previous run. It reviews only what changed, plus:
  - anything that calls the changed code, or that it calls
  - everything that uses a changed object or field (a field, validation rule, record type or
    deleted component marks its whole object as changed)

  Findings on untouched components are carried forward with their verifier verdict. Their quotes
  are checked again against the new files, so a stale finding is rejected rather than copied. A
  client deliverable always uses `--baseline none` (the default), which is a full review.
- **Rule checks.** `rule_checks.py` reports facts the index already knows exactly. They cost no tokens,
  are not verified by AI, and show "rule check" in the review sheet. Reviewers don't repeat the
  generic fact. They still report a specific, worse consequence at its own severity.
- **Finding library** (`~/.sf-health/library.jsonl`, local only, never committed). `review_gate.py import`
  saves every Accept / Edit / Reject decision with component, object, client and user names replaced
  by placeholders. The review sheet's **Library hint** column shows how similar findings were decided
  before, and your decision on the same finding last run. It is a hint for the consultant only.
  Free-text consultant notes are kept only for the same client; they are not shown on other
  clients' runs. Passing known false positives to reviewers (`library.warnReviewers`) is off by default.
  `python scripts/library.py stats` shows what it holds.
- **Why no vector database.** An audit must look at every component, and vector search only returns the
  "most similar" pieces. The index is an exact map of what calls what, which is more reliable for
  this. Matching in the library is by keywords; a local embedding model can be added later if it
  grows past a few hundred entries.

## Pilot results (synthetic fixture, 2026-10-17)

| Step | Agents | Tokens | Time |
|---|---|---|---|
| Review | 7 | 293k | 33 s |
| Follow-up | 3 | 144k | 40 s |
| Verify (opus) | 18 | 746k | 59 s |
| Summary (opus) | 1 | 71k | 81 s |

- **Quotes:** 31 of 31 findings passed the quote check; none were rejected.
- **Accuracy:** recall 100% (15 of 15 planted problems found). The clean class got no High or Critical findings.
  Precision was reported as 100%, but that was measured after three AI-found issues (P13–P15) were added
  to the answer key. `tests/eval.py` now matches findings to key items one to one, including Low findings;
  re-run it for a current precision figure.
- **Verifier:** confirmed 11 findings, downgraded 6 and refuted 1. The refuted one was a recursion claim that couldn't happen, because the code crashes first. A follow-up finding had already reported that crash.
- **Answer-key update:** P13–P15 were real fixture issues that the AI found first; they were added to the key afterwards.

## Configuration

All settings, with comments, are in [config/default.yml](../config/default.yml). A client config only
lists what it changes (start from [config/client.example.yml](../config/client.example.yml)); it is
deep-merged over the defaults and saved in the run folder as `client-config.yml`.

## Safety rules

- Collection only retrieves and queries. Never: deploy, data writes, permission assignment, or
  `sf check run-all-tests` (that runs Apex tests in the org).
- Every agent (reviewers, verifier, summary) runs as a plugin subagent type limited to Read/Grep/Glob
  (the summary agent: Read only), and is told org files are untrusted data, not instructions. The
  workflows refuse to run with `models.useAgentTypes: false`. `models.allowUnrestrictedAgents: true` overrides
  that for development only, with a warning; never use it on a client org.
- Client data stays in `health-runs/` (git-ignored). Never commit a run folder.
- The finding library stores patterns only (names replaced), on the consultant's own machine.
  Set `library.enabled: false` in a client config if a client contract rules out even that.

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

## Releasing a new version

1. Run the tests (above). They must pass.
2. Bump `version` in [.claude-plugin/plugin.json](../.claude-plugin/plugin.json). Installed copies only
   update when this string changes.
3. Add an entry to [CHANGELOG.md](../CHANGELOG.md).
4. Commit, push to `main`, and tell users to run `claude plugin update sf-health@a360-salesforce`.

## Rolling out to the company

By default each person installs the plugin (user guide, section 1.5) and updates it by hand: auto-update
is off for company marketplaces. A Claude Code admin can instead add the marketplace and turn on
auto-update for everyone in **managed settings** (claude.ai admin console, or `managed-settings.json`):

```json
{
  "extraKnownMarketplaces": {
    "a360-salesforce": {
      "source": { "source": "github", "repo": "<company-org>/<repo>" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": {
    "sf-health@a360-salesforce": true
  }
}
```

For a private repository, each user needs read access and a stored git login (`gh auth login`, then
`gh auth setup-git`); Claude Code downloads the plugin with the user's own git credentials.
