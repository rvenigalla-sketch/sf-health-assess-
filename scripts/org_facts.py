"""Condense org-level tool output (Health Check, hardis) into small citable text tables.

Raw outputs can be huge (FSF: 27k permission grants, 9 MB). Reviewers get one line per fact in
index/orgfacts/<topic>.txt. Those files are real paths with line numbers, so findings cite them
exactly like source code and validate_findings.py checks them the same way.

Called by build_index.py; returns a summary dict that goes into metrics.json.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from common import read_json

SEP = " | "


def _result(tools: Path, name: str):
    data = read_json(tools / name)
    if data is None:
        return None
    return data.get("result", data) if isinstance(data, dict) else data


def _write(out: Path, topic: str, title: str, header: list[str], rows: list[list], notes: list[str] = ()) -> str:
    lines = [f"# {title}", *[f"# {n}" for n in notes], SEP.join(header)]
    lines += [SEP.join(str(c).replace("\n", " ").replace("|", "/") for c in r) for r in rows]
    path = out / f"{topic}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path.as_posix()


def build_org_facts(tools: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    summary: dict = {}

    # ---- Security Health Check
    score = _result(tools, "healthcheck-score.json")
    risks = _result(tools, "healthcheck-risks.json")
    if score and score.get("records"):
        summary["healthCheckScore"] = float(score["records"][0].get("Score") or 0)
    if risks and risks.get("records"):
        order = {"HIGH_RISK": 0, "MEDIUM_RISK": 1, "LOW_RISK": 2, "INFORMATIONAL": 3, "MEETS_STANDARD": 4}
        recs = sorted(risks["records"], key=lambda r: (order.get(r.get("RiskType"), 9), r.get("SettingGroup", "")))
        _write(out, "healthcheck", f"Security Health Check (score {summary.get('healthCheckScore', '?')})",
               ["RiskType", "SettingGroup", "Setting", "OrgValue", "StandardValue"],
               [[r.get("RiskType"), r.get("SettingGroup"), r.get("Setting"), r.get("OrgValue"), r.get("StandardValue")]
                for r in recs])
        summary["healthCheckRisks"] = dict(Counter(r.get("RiskType") for r in recs))

    # ---- Dangerous permissions (hardis unsecure-permissions): tier 0 = most dangerous
    perms = _result(tools, "hardis-unsecure-permissions.json")
    if perms and perms.get("profileGrants") is not None:
        grants = (perms.get("profileGrants") or []) + (perms.get("permSetGrants") or [])
        by_container = defaultdict(lambda: {"type": "", "tiers": Counter(), "top": set()})
        for g in grants:
            # hardis uses two row shapes: camelCase (profiles) and display labels (some perm sets).
            name = g.get("containerName") or g.get("Container") or "?"
            tier = g.get("tier")
            if tier is None:
                tier = int(str(g.get("Tier", "9")).replace("Tier", "").strip() or 9)
            label = g.get("permissionLabel") or g.get("Permission") or "?"
            obj = g.get("object") or g.get("Object") or ""
            c = by_container[name]
            c["type"] = g.get("containerType") or g.get("Type") or "PermissionSet"
            c["tiers"][tier] += 1
            if tier <= 1:
                c["top"].add(label + (f" ({obj})" if obj else ""))
        rows = sorted(by_container.items(), key=lambda kv: (-kv[1]["tiers"][0], -kv[1]["tiers"][1], kv[0]))
        _write(out, "permissions", "Dangerous permission grants by profile / permission set (hardis; tier 0 = most dangerous)",
               ["Container", "Type", "Tier0", "Tier1", "Tier2", "Tier3+", "Tier0-1 permissions"],
               [[n, c["type"], c["tiers"][0], c["tiers"][1], c["tiers"][2],
                 sum(v for t, v in c["tiers"].items() if t >= 3), "; ".join(sorted(c["top"]))[:600]]
                for n, c in rows if sum(c["tiers"].values())])
        users = perms.get("assignments") or []
        _write(out, "privileged_users", "Users holding dangerous permissions (hardis)",
               ["User", "Profile", "LastLogin", "MaxTier", "DangerousPermissionCount"],
               [[u.get("Username"), u.get("Profile"), u.get("Last Login"), u.get("Max Tier"),
                 u.get("Dangerous Permissions Count")] for u in users])
        summary["usersWithDangerousPermissions"] = int(perms.get("usersWithDangerousPermissions") or len(users))
        summary["containersWithTier0"] = sum(1 for c in by_container.values() if c["tiers"][0])

    # ---- Access hygiene
    ups = _result(tools, "hardis-underusedpermsets.json")
    if ups:
        rows = [[p.get("Name"), p.get("Type"), p.get("UserCount")]
                for k in ("zeroUserPermSets", "limitedUserPermSets", "zeroUserPermSetGroups", "limitedUserPermSetGroups")
                for p in ups.get(k) or []]
        _write(out, "underused_permsets", "Permission sets / groups with 0 or few users (hardis)",
               ["Name", "Type", "Users"], rows)
        summary["underusedPermissionSets"] = len(rows)
    uu = _result(tools, "hardis-unusedusers.json")
    ul = _result(tools, "hardis-unusedlicenses.json")
    rows = [[u.get("Username"), u.get("Profile.Name"), u.get("LastLoginDate") or "never", "inactive login"]
            for u in (uu or {}).get("users") or []]
    rows += [[l.get("User"), l.get("PermissionsSetLicense"), "", l.get("Reason")]
             for l in (ul or {}).get("unusedPermissionSetLicenseAssignments") or []]
    if uu or ul:
        _write(out, "licenses_users", "Users not logging in and unused license assignments (hardis)",
               ["User", "ProfileOrLicense", "LastLogin", "Issue"], rows)
        summary["unusedUsersOrLicenses"] = len(rows)

    # ---- Connected apps / OAuth
    ca = _result(tools, "hardis-unsecure-connected-apps.json")
    if ca:
        tokens = ca.get("unsecuredOAuthTokens") or []
        by_app = Counter(t.get("AppName") for t in tokens)
        last = {}
        for t in tokens:
            last[t.get("AppName")] = max(last.get(t.get("AppName"), ""), t.get("Last Used Date") or "")
        _write(out, "connected_apps", "OAuth tokens on connected apps that are not admin pre-approved (hardis)",
               ["App", "OpenTokens", "LastUsed"], [[a, n, last.get(a, "")] for a, n in by_app.most_common()])
        summary["unsecuredConnectedApps"] = len(by_app)

    # ---- Release readiness
    ru = _result(tools, "hardis-releaseupdates.json")
    if ru:
        recs = sorted(ru.get("suspectRecords") or [], key=lambda r: r.get("DueDate") or "9999")
        _write(out, "release_updates", "Release updates not yet completed (hardis)",
               ["DueDate", "Status", "Category", "Title", "Release"],
               [[r.get("DueDate"), r.get("Status"), r.get("Category"), r.get("Title"), r.get("ReleaseLabel")] for r in recs])
        summary["pendingReleaseUpdates"] = len(recs)
    la = _result(tools, "hardis-legacyapi.json")
    if la:
        rows = [[",".join(map(str, r.get("apiFamily") or [])), r.get("minApiVersion"), r.get("maxApiVersion"),
                 r.get("severity"), r.get("deprecationRelease"), r.get("totalErrors")]
                for r in la.get("legacyApiResults") or []]
        _write(out, "legacy_api", "Calls to retired / retiring API versions in event logs (hardis)",
               ["ApiFamily", "MinVersion", "MaxVersion", "Severity", "Retirement", "Calls"], rows)
        summary["legacyApiCalls"] = sum(int(r[-1] or 0) for r in rows)
    ua = _result(tools, "hardis-unused-apex-classes.json")
    if ua:
        _write(out, "unused_async_apex", "Async Apex classes with no recent jobs (hardis)",
               ["Class", "AsyncType", "LatestJobRunDays", "NextJob"],
               [[c.get("Name"), c.get("AsyncType"), c.get("latestJobRunDays"), c.get("nextJobDate")]
                for c in ua.get("asyncClassList") or []])

    # ---- Org limits (only those above 50%)
    lim = _result(tools, "hardis-limits.json")
    if lim and lim.get("limitEntries"):
        high = sorted((l for l in lim["limitEntries"] if (l.get("percentUsed") or 0) >= 50), key=lambda l: -l["percentUsed"])
        _write(out, "limits", "Org limits at 50% or more (hardis)", ["Limit", "Used", "Max", "PercentUsed"],
               [[l.get("name"), l.get("used"), l.get("max"), l.get("percentUsed")] for l in high])
        summary["limitsAbove50Pct"] = len(high)

    # ---- Runtime errors (already redacted at collection): grouped, not one row per interview
    errs = _result(tools, "hardis-errors.json")
    if errs:
        groups = Counter()
        first_last = {}
        for e in (errs.get("flowErrors") or []) + (errs.get("apexErrors") or []):
            op = (e.get("Operation") or e.get("ApexClass") or "?")
            op = op.rsplit(" ", 3)[0] if any(ch.isdigit() for ch in op[-8:]) else op   # drop the timestamp suffix
            key = (e.get("Source") or "?", op, e.get("ErrorStep") or "", (e.get("Exception") or "")[:200])
            groups[key] += 1
            t = e.get("StartTime") or ""
            lo, hi = first_last.get(key, (t, t))
            first_last[key] = (min(lo, t), max(hi, t))
        _write(out, "runtime_errors", f"Runtime errors in the last {errs.get('days', '?')} days, grouped (hardis; record IDs and values redacted)",
               ["Count", "Source", "FlowOrClass", "ErrorStep", "First", "Last", "Exception"],
               [[n, *k[:3], first_last[k][0][:10], first_last[k][1][:10], k[3]] for k, n in groups.most_common()])
        summary["runtimeErrors"] = sum(groups.values())
        summary["runtimeErrorGroups"] = len(groups)

    return summary
