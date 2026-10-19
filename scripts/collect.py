"""Stage 1 - Collect. Deterministic, no LLM.

Preflight -> manifest from org -> chunked metadata retrieve -> run analysis tools -> sanitize.
Every step is logged to <run>/run_log.json; a failed chunk or tool is logged and the run continues.

Usage:
  python collect.py --org "FSF Prod" --client FSF [--config client.yml] [--steps preflight,manifest,retrieve,tools]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import (
    Run, Timer, info, load_config, read_json, run_cmd, run_sf_json, write_json,
)

MD_NS = "http://soap.sforce.com/2006/04/metadata"
ALL_STEPS = ("preflight", "manifest", "retrieve", "tools")


# --------------------------------------------------------------------------- preflight
def preflight(run: Run, cfg: dict, org: str) -> bool:
    with Timer() as t:
        problems, notes = [], {}

        code, out, _ = run_cmd([sys.executable, "--version"])
        notes["python"] = out.strip()
        if sys.version_info < (3, 10):
            problems.append("Python 3.10+ required (Code Analyzer flow engine)")

        # `sf plugins --json` returns a bare list (no status wrapper).
        code, out, _ = run_cmd(["sf", "plugins", "--json"], timeout=600)
        try:
            listed = json.loads(out[out.find("["):]) if "[" in out else []
        except json.JSONDecodeError:
            listed = []
        installed = {p.get("name") for p in listed if isinstance(p, dict)}
        notes["plugins"] = sorted(n for n in installed if n and not n.startswith("@oclif"))
        needed = {"@salesforce/plugin-code-analyzer": cfg["tools"]["codeAnalyzer"]["enabled"]}
        if cfg["dataPolicy"] == "full":
            needed["@orgcheck/sfdx-plugin"] = cfg["tools"]["orgCheck"]["enabled"]
            needed["sfdx-hardis"] = cfg["tools"]["hardis"]["enabled"]
        for name, wanted in needed.items():
            if wanted and name not in installed:
                problems.append(f"sf plugin missing: {name} (sf plugins install {name})")

        ok, org_info = run_sf_json(["org", "display", "--target-org", org])
        if not ok:
            problems.append(f"Org '{org}' not authenticated: {org_info.get('message') or org_info.get('error')}")
        else:
            r = org_info["result"]
            notes["org"] = {"username": r.get("username"), "instanceUrl": r.get("instanceUrl"),
                            "orgId": r.get("id"), "apiVersion": r.get("apiVersion")}

        ok, limits = run_sf_json(["org", "list", "limits", "--target-org", org])
        if ok:
            api = next((l for l in limits["result"] if l.get("name") == "DailyApiRequests"), None)
            if api and api.get("max"):
                headroom = api["remaining"] / api["max"]
                notes["dailyApiRequests"] = {"max": api["max"], "remaining": api["remaining"],
                                             "headroom": round(headroom, 3)}
                if headroom < cfg["retrieve"]["minApiHeadroom"]:
                    problems.append(f"Low API headroom: {api['remaining']}/{api['max']} remaining today")

    run.log_step("preflight", "failed" if problems else "ok", t.seconds, problems=problems, **notes)
    for p in problems:
        info(f"PREFLIGHT: {p}")
    return not problems


# --------------------------------------------------------------------------- manifest
def parse_package(path: Path) -> dict[str, list[str]]:
    root = ET.parse(path).getroot()
    types: dict[str, list[str]] = {}
    for t in root.findall(f"{{{MD_NS}}}types"):
        name = t.findtext(f"{{{MD_NS}}}name")
        members = [m.text for m in t.findall(f"{{{MD_NS}}}members") if m.text]
        types.setdefault(name, []).extend(members)
    return types


def write_package(path: Path, types: dict[str, list[str]], api_version: str) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{MD_NS}">']
    for name in sorted(types):
        lines.append("    <types>")
        lines += [f"        <members>{m}</members>" for m in sorted(types[name])]
        lines.append(f"        <name>{name}</name>")
        lines.append("    </types>")
    lines += [f"    <version>{api_version}</version>", "</Package>", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def manifest(run: Run, cfg: dict, org: str) -> bool:
    with Timer() as t:
        out_dir = run.path("manifest")
        # sf manifest/retrieve commands must run inside an SFDX project.
        org_api = next((s.get("org", {}).get("apiVersion") for s in reversed(read_json(run.log_path, {}).get("steps", []))
                        if s.get("step") == "preflight" and s.get("org")), None)
        ensure_sfdx_project(run.path("metadata"), org_api or "62.0")
        ok, res = run_sf_json(["project", "generate", "manifest", "--from-org", org,
                               "--output-dir", str(out_dir), "--name", "full-package"],
                              cwd=run.path("metadata"), timeout=1800)
        full = out_dir / "full-package.xml"
        if not ok or not full.exists():
            run.log_step("manifest", "failed", t.seconds, error=res.get("message") or res.get("error"))
            return False

        api_version = ET.parse(full).getroot().findtext(f"{{{MD_NS}}}version") or "62.0"
        excluded = set(cfg["retrieve"]["excludeTypes"])
        all_types = parse_package(full)
        types = {k: v for k, v in all_types.items() if k not in excluded}

        # Managed-package components can't be changed by the client: drop members whose own
        # name is in an installed package namespace (Account.ns__Field__c, ns__Object__c, ns__Class).
        namespaces = set(all_types.get("InstalledPackage", []))
        managed_dropped = 0
        if cfg["retrieve"].get("excludeManagedPackages", True) and namespaces:
            def is_managed(member: str) -> bool:
                last = member.replace("/", ".").split(".")[-1]
                return any(last.startswith(ns + "__") for ns in namespaces)
            for name in list(types):
                kept = [m for m in types[name] if not is_managed(m)]
                managed_dropped += len(types[name]) - len(kept)
                if kept:
                    types[name] = kept
                else:
                    del types[name]

        # Split into chunks of <= chunkSize members, keeping each type's members contiguous.
        size = cfg["retrieve"]["chunkSize"]
        chunks, current, count = [], {}, 0
        for name in sorted(types):
            for member in types[name]:
                if count >= size:
                    chunks.append(current)
                    current, count = {}, 0
                current.setdefault(name, []).append(member)
                count += 1
        if current:
            chunks.append(current)

        for old in out_dir.glob("chunk_*.xml"):
            old.unlink()
        for i, chunk in enumerate(chunks, 1):
            write_package(out_dir / f"chunk_{i:02d}.xml", chunk, api_version)

        counts = {k: len(v) for k, v in sorted(types.items())}
    run.log_step("manifest", "ok", t.seconds, apiVersion=api_version, chunks=len(chunks),
                 members=sum(counts.values()), excludedTypes=sorted(excluded),
                 managedNamespaces=sorted(namespaces), managedMembersDropped=managed_dropped, typeCounts=counts)
    return True


# --------------------------------------------------------------------------- retrieve
def ensure_sfdx_project(meta_dir: Path, api_version: str) -> None:
    proj = meta_dir / "sfdx-project.json"
    if not proj.exists():
        write_json(proj, {"packageDirectories": [{"path": "force-app", "default": True}],
                          "sourceApiVersion": api_version})
    (meta_dir / "force-app").mkdir(exist_ok=True)


def retrieve(run: Run, cfg: dict, org: str) -> bool:
    chunks = sorted(run.path("manifest").glob("chunk_*.xml"))
    if not chunks:
        info("RETRIEVE: no chunk manifests; run the manifest step first")
        return False
    api_version = ET.parse(chunks[0]).getroot().findtext(f"{{{MD_NS}}}version")
    meta_dir = run.path("metadata")
    ensure_sfdx_project(meta_dir, api_version)
    workers = max(1, int(cfg["retrieve"].get("parallel", 1)))
    staging_root = run.path("_staging")

    def one(chunk: Path) -> bool:
        # In parallel, each chunk gets its own SFDX project so two retrieves never write the same
        # folder at once; files are merged into metadata/force-app afterwards.
        cwd = meta_dir
        if workers > 1:
            cwd = staging_root / chunk.stem
            shutil.rmtree(cwd, ignore_errors=True)
            ensure_sfdx_project(cwd, api_version)
        for attempt in range(1, cfg["retrieve"]["retries"] + 2):
            with Timer() as t:
                ok, res = run_sf_json(
                    ["project", "retrieve", "start", "--manifest", str(chunk), "--target-org", org,
                     "--wait", str(cfg["retrieve"]["waitMinutes"]), "--ignore-conflicts"],
                    cwd=cwd, timeout=cfg["retrieve"]["waitMinutes"] * 60 + 300)
            files = len((res.get("result") or {}).get("files", []) or [])
            run.log_step(f"retrieve:{chunk.stem}", "ok" if ok else "failed", t.seconds,
                         attempt=attempt, files=files, parallel=workers,
                         error=None if ok else (res.get("message") or res.get("error")))
            if ok:
                info(f"RETRIEVE {chunk.stem}: {files} files in {t.seconds:.0f}s")
                return True
        return False

    with Timer() as total:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = dict(zip(chunks, pool.map(one, chunks)))
        if workers > 1:
            # Merge in chunk order. Objects are split across chunks by type (fields, record types...),
            # so folders merge rather than replace.
            for chunk in chunks:
                staged = staging_root / chunk.stem / "force-app"
                if results[chunk] and staged.exists():
                    shutil.copytree(staged, meta_dir / "force-app", dirs_exist_ok=True)
            shutil.rmtree(staging_root, ignore_errors=True)
    failed = [c.stem for c, ok in results.items() if not ok]
    run.log_step("retrieve", "failed" if failed else "ok", total.seconds, chunks=len(chunks),
                 parallel=workers, failedChunks=failed)
    if failed:
        info(f"RETRIEVE: failed chunks {failed} (see run_log.json); continuing with partial metadata")
    return not failed


# --------------------------------------------------------------------------- tools
SF_ID = re.compile(r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5}0[A-Za-z0-9]{9}(?:[A-Za-z0-9]{3})?\b")
QUOTED = re.compile(r"(\"[^\"\n]{1,300}\"|'[^'\n]{1,300}')")


def redact_text(text: str) -> str:
    """Remove record IDs and quoted field values from runtime error text."""
    return QUOTED.sub("<value>", SF_ID.sub("<recordId>", text))


def redact_json(obj):
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    return obj


def tool_step(run: Run, name: str, args: list[str], out_file: str, cwd: Path | None = None,
              redact: bool = False, timeout: int = 3600, org: str | None = None) -> bool:
    with Timer() as t:
        # SF_TARGET_ORG: some plugins (hardis) call `sf` internally without passing -o.
        ok, res = run_sf_json(args, cwd=cwd, timeout=timeout, env={"SF_TARGET_ORG": org} if org else None)
        # hardis exits with status 1 when it finds issues; a result payload means it ran.
        ok = ok or bool(res.get("result"))
        if redact:
            res = redact_json(res)
        write_json(run.path("tools", out_file), res)
    run.log_step(f"tool:{name}", "ok" if ok else "failed", t.seconds, output=f"tools/{out_file}",
                 error=None if ok else (res.get("message") or res.get("error")))
    info(f"TOOL {name}: {'ok' if ok else 'FAILED'} ({t.seconds:.0f}s)")
    return ok


def code_analyzer(run: Run, cfg: dict) -> None:
    meta = run.path("metadata")
    with Timer() as t:
        code, out, err = run_cmd(
            ["sf", "code-analyzer", "run", "--workspace", "force-app",
             "--rule-selector", cfg["tools"]["codeAnalyzer"]["ruleSelector"],
             "--output-file", str(run.path("tools", "code-analyzer.json")),
             "--output-file", str(run.path("tools", "code-analyzer.sarif")),
             "--view", "table"],
            cwd=meta, timeout=7200)
    ok = run.path("tools", "code-analyzer.json").exists()
    run.log_step("tool:code-analyzer", "ok" if ok else "failed", t.seconds,
                 output="tools/code-analyzer.json", error=None if ok else (err or out)[-1500:])
    info(f"TOOL code-analyzer: {'ok' if ok else 'FAILED'} ({t.seconds:.0f}s)")


def org_check(run: Run, cfg: dict, org: str) -> None:
    # Only read-only views. Never `sf check run-all-tests` (runs Apex tests in the org).
    out_dir = run.path("tools", "orgcheck")
    out_dir.mkdir(exist_ok=True)
    terms = ["--accept-the-terms"] if cfg["tools"]["orgCheck"].get("acceptTerms") else []
    for view in cfg["tools"]["orgCheck"]["views"]:
        tool_step(run, f"orgcheck:{view}",
                  ["check", view, "--target-org", org, *terms,
                   "--json-file", str(out_dir / f"{view}.json"),
                   "--xlsx-file", str(out_dir / f"{view}.xlsx")],
                  f"orgcheck/{view}-run.json", timeout=7200)


def hardis(run: Run, cfg: dict, org: str) -> None:
    redact = cfg["sanitize"]["redactErrorRecordData"]
    commands = list(cfg["tools"]["hardis"]["commands"])
    if cfg["tools"]["hardis"].get("auditTrail"):
        commands.append("hardis:org:diagnose:audittrail")
    for command in commands:
        short = command.split(":")[-1]
        # --agent: hardis non-interactive mode (no prompts).
        tool_step(run, f"hardis:{short}", [*command.split(" "), "--target-org", org, "--agent"],
                  f"hardis-{short.split(' ')[0]}.json", cwd=run.path("metadata"),
                  redact=redact and "monitor" in command, timeout=3600, org=org)


def health_check(run: Run, cfg: dict, org: str) -> None:
    tool_step(run, "healthcheck-score",
              ["data", "query", "--use-tooling-api", "--target-org", org,
               "--query", "SELECT Score, CustomBaselineId FROM SecurityHealthCheck"],
              "healthcheck-score.json")
    tool_step(run, "healthcheck-risks",
              ["data", "query", "--use-tooling-api", "--target-org", org,
               "--query", "SELECT RiskType, Setting, SettingGroup, OrgValue, StandardValue "
                          "FROM SecurityHealthCheckRisks"],
              "healthcheck-risks.json")


def tools(run: Run, cfg: dict, org: str) -> None:
    t = cfg["tools"]
    with Timer() as total, ThreadPoolExecutor(max_workers=2) as pool:
        # Code Analyzer only reads local files, so it runs alongside the org queries below.
        ca = pool.submit(code_analyzer, run, cfg) if t["codeAnalyzer"]["enabled"] else None
        if cfg["dataPolicy"] == "metadata-only":
            info("TOOLS: dataPolicy=metadata-only, skipping org-querying tools")
        else:
            # Org-querying tools stay one at a time: they share the org's API limits.
            if t["healthCheck"]["enabled"]:
                health_check(run, cfg, org)
            if t["orgCheck"]["enabled"]:
                org_check(run, cfg, org)
            if t["hardis"]["enabled"]:
                hardis(run, cfg, org)
        if ca:
            ca.result()
    run.log_step("tools", "ok", total.seconds, dataPolicy=cfg["dataPolicy"])


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", required=True, help="sf org alias or username")
    ap.add_argument("--client", required=True)
    ap.add_argument("--config")
    ap.add_argument("--date", help="run date folder (default today); reuse to resume a run")
    ap.add_argument("--steps", default=",".join(ALL_STEPS))
    ap.add_argument("--force", action="store_true", help="continue even if preflight fails")
    args = ap.parse_args()

    cfg = load_config(args.config)
    run = Run.create(cfg["workspaceRoot"], args.client, args.date)
    run.set_meta(client=args.client, org=args.org, dataPolicy=cfg["dataPolicy"])
    info(f"Run folder: {run.root}")
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]

    if "preflight" in steps and not preflight(run, cfg, args.org) and not args.force:
        info("Stopping: fix preflight problems or pass --force")
        return 2
    if "manifest" in steps and not manifest(run, cfg, args.org):
        return 3
    if "retrieve" in steps:
        retrieve(run, cfg, args.org)
    if "tools" in steps:
        tools(run, cfg, args.org)
    print(json.dumps({"run": str(run.root)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
