# sf-health data handling

> **DRAFT for A360 security and legal review.** Not approved for client use until signed off.
> Sign-off: ______________________ (security)  ______________________ (legal)  Date: __________

This page states what sf-health reads from a client's Salesforce org, what is sent to Claude
(Anthropic), and where everything is stored. It is written so it can be checked against the code;
file references are given for each statement.

## 1. What is read from the org

sf-health uses the Salesforce CLI with the consultant's own login, and only **retrieves and queries**.
It never deploys, writes or deletes data, assigns permissions or runs Apex tests
(`scripts/collect.py`).

| Data policy | What is read |
|---|---|
| `metadata-only` | Org **metadata**: Apex code, flows, triggers, objects and fields, validation rules, layouts, profiles and permission sets, settings. Reports, dashboards, documents, static resources, email templates and translations are excluded. Then a static code scan (Code Analyzer) runs locally. |
| `full` | Everything above, plus read-only **org checks**: Security Health Check, org limits, permission assignments, inactive users and unused licences, connected-app OAuth tokens, release updates, legacy API use, and Apex error messages from the last 30 days. |

**Business records** (accounts, contacts, cases, opportunities, custom-object data, files) are
**never** queried or retrieved under either policy.

## 2. Personal data that can be included

- **Usernames** (`full` only): users who hold dangerous permissions, and users who haven't logged in
  or hold unused licences, with profile and last-login date (`scripts/org_facts.py`). Salesforce
  usernames are usually email addresses.
- **Error messages** (`full` only): record IDs and quoted field values are removed before anything
  else reads them (`sanitize.redactErrorRecordData`, on by default).
- **Whatever the client's own metadata contains**: e.g. names or email addresses hard-coded in Apex,
  flow descriptions or comments. sf-health does not filter these.

Under `metadata-only`, only the last point applies.

## 3. What is sent to Claude

The AI stages (review, follow-up, verify, summary) send the following to Claude through the
consultant's Claude Code session under **A360's Anthropic agreement**:

- the retrieved metadata files the reviewers open, and the summaries the scripts build from them
- the org-check tables above (`full` only)
- the findings, and for the summary only the findings the consultant accepted

The AI agents can only **read** files in the run folder. They cannot run commands, write files or
reach the org (`agents/*.md`, `workflows/*.js`; the workflows refuse to run otherwise).
Org files are treated as untrusted data, never as instructions.

<!-- Legal to confirm: data retention and training terms of A360's Anthropic plan (commercial terms
     / zero data retention), and whether clients must be told Claude is used. -->

## 4. Where data is stored

| Data | Location | Retention |
|---|---|---|
| Run folder: metadata snapshot, tool outputs, findings, reports | Consultant's laptop, `health-runs/<Client>/<date>/` | Until moved to client-approved storage. Never in git (`.gitignore`) |
| Finding library: past decisions, with component, object, client and user names replaced by placeholders | Consultant's laptop, `~/.sf-health/library.jsonl` | Kept. Can be turned off per client (`library.enabled: false`) |
| Claude Code session logs | Consultant's laptop, Claude Code's own folder | Per Claude Code settings |

Nothing is uploaded anywhere else. The HTML dashboard is only published online if the consultant
explicitly chooses to.

## 5. Statement for clients (proposed)

> To assess your Salesforce org, A360 retrieves its configuration and code (metadata) with read-only
> access. [With full scope: we also run Salesforce's read-only security and usage checks, which list
> the usernames of users with powerful permissions or unused licences.] We do not access your
> business records. The metadata is analysed on the consultant's machine with the help of Anthropic's
> Claude AI under A360's commercial agreement, and every finding is reviewed by an A360 consultant
> before it is reported. Assessment files are kept in [agreed location] and deleted [agreed period].

## 6. Consultant obligations

See the [user guide, section 10](USER-GUIDE.md#10-rules-you-must-follow).
