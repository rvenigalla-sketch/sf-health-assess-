"""Stage 1 - Collect. Deterministic, no LLM.

Preflight -> manifest from org -> chunked metadata retrieve -> run analysis tools -> sanitize.
Every step is logged to <run>/run_log.json; a failed chunk or tool is logged and the run continues.

Usage:
  python collect.py --org "FSF Prod" --client FSF [--config client.yml] [--steps preflight,manifest,retrieve,tools]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from common import (
    Run, Timer, info, load_config, load_run_config, read_json, run_cmd, run_sf_json, write_json,
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

        notes["windowsLongPaths"] = long_paths_enabled()
        if not notes["windowsLongPaths"]:
            info("PREFLIGHT warning: Windows long paths are off. Files whose full path passes 260 characters "
                 "(long layout or flow names) will be left out and listed in run_log.json. To avoid it, set "
                 "LongPathsEnabled=1 (admin) or use a shorter workspaceRoot such as C:/hr.")

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
    # Member names can contain XML specials ("Case-Sales & Service Layout"); unescaped, one such name
    # makes the whole chunk manifest unparseable.
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{MD_NS}">']
    for name in sorted(types):
        lines.append("    <types>")
        lines += [f"        <members>{xml_escape(m)}</members>" for m in sorted(types[name])]
        lines.append(f"        <name>{xml_escape(name)}</name>")
        lines.append("    </types>")
    lines += [f"    <version>{xml_escape(api_version)}</version>", "</Package>", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def is_managed(member: str, mtype: str, namespaces: set[str]) -> bool:
    """True if the component itself belongs to an installed package namespace.

    What matters is the component's OWN name, not its parent's:
      CustomField   Account.ns__Field__c   managed       ns__Obj__c.My_Field__c  subscriber's
      Layout        Account-ns__X Layout   managed       ns__Obj__c-My Layout    subscriber's
      CustomMetadata ns__Type.ns__Rec      managed       ns__Type.My_Rec         subscriber's record
      EmailTemplate folder/ns__Tpl         managed
    Layout names are "<Object>-<Layout name>" (object names can't contain '-'; layout names can
    contain '.'), so they are split on the first dash instead of the last dot."""
    if not namespaces:
        return False
    if mtype == "Layout" and "-" in member:
        own = member.split("-", 1)[1]
    else:
        own = member.replace("/", ".").split(".")[-1]
    return any(own.startswith(ns + "__") for ns in namespaces)


# Profiles / permission sets: the Metadata API only returns their access entries (FLS, object, class,
# page, tab, app, layout, record type, flow, custom permission access) for components that are in
# the SAME retrieve. Split alphabetically, they'd come back nearly empty. So they never go in the
# main chunks; they're retrieved in a separate "access" pass, a few at a time, each chunk together
# with the components they grant access to. Custom metadata types are CustomObjects (X__mdt), and a
# CustomObject brings its fields, so CustomField members are not needed for FLS.
ACCESS_TYPES = ("Profile", "PermissionSet", "MutingPermissionSet")
ACCESS_RELATED_TYPES = ("CustomObject", "ApexClass", "ApexPage", "CustomTab", "CustomApplication",
                        "Layout", "RecordType", "Flow", "CustomPermission", "ExternalDataSource")
# Only these folders are kept from an access chunk; the objects / classes it also returns are partial
# copies of what the main chunks retrieved and must not overwrite them.
ACCESS_FOLDERS = ("profiles", "permissionsets", "mutingpermissionsets")


def plan_chunks(types: dict[str, list[str]], size: int, access_size: int = 25,
                related_size: int | None = None) -> tuple[list[dict], list[dict]]:
    """Split the manifest into (main chunks, access chunks).

    Main chunks: every type except Profile/PermissionSet/MutingPermissionSet, <= size members each,
    each type's members contiguous. Access chunks: <= access_size profiles/permission sets each, plus
    the related types. If the related types exceed related_size members, they are split into groups
    and every access batch is retrieved once per group; the profile files are merged afterwards
    (merge_access_file), so each profile ends up with all its access entries."""
    related_size = related_size or size
    chunks, current, count = [], {}, 0
    for name in sorted(types):
        if name in ACCESS_TYPES:
            continue
        for member in types[name]:
            if count >= size:
                chunks.append(current)
                current, count = {}, 0
            current.setdefault(name, []).append(member)
            count += 1
    if current:
        chunks.append(current)

    access_members = [(t, m) for t in ACCESS_TYPES for m in types.get(t, [])]
    if not access_members:
        return chunks, []
    groups, current, count = [], {}, 0
    for name in ACCESS_RELATED_TYPES:
        for member in types.get(name, []):
            if count >= related_size:
                groups.append(current)
                current, count = {}, 0
            current.setdefault(name, []).append(member)
            count += 1
    groups.append(current)        # may be empty: profiles alone still bring their user permissions
    access = []
    for start in range(0, len(access_members), max(1, access_size)):
        batch: dict[str, list[str]] = {}
        for t, m in access_members[start:start + max(1, access_size)]:
            batch.setdefault(t, []).append(m)
        for group in groups:
            access.append({**{k: list(v) for k, v in group.items()}, **batch})
    return chunks, access


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
            for name in list(types):
                kept = [m for m in types[name] if not is_managed(m, name, namespaces)]
                managed_dropped += len(types[name]) - len(kept)
                if kept:
                    types[name] = kept
                else:
                    del types[name]

        size = cfg["retrieve"]["chunkSize"]
        chunks, access = plan_chunks(types, size, cfg["retrieve"].get("accessChunkSize", 25),
                                     cfg["retrieve"].get("accessRelatedChunkSize", size))

        for old in [*out_dir.glob("chunk_*.xml"), *out_dir.glob("access_*.xml")]:
            old.unlink()
        for i, chunk in enumerate(chunks, 1):
            write_package(out_dir / f"chunk_{i:02d}.xml", chunk, api_version)
        for i, chunk in enumerate(access, 1):
            write_package(out_dir / f"access_{i:02d}.xml", chunk, api_version)

        counts = {k: len(v) for k, v in sorted(types.items())}
    run.log_step("manifest", "ok", t.seconds, apiVersion=api_version, chunks=len(chunks),
                 accessChunks=len(access),
                 accessPass="Profile/PermissionSet/MutingPermissionSet retrieved separately with the "
                            "types they grant access to; only their files are kept from those chunks",
                 members=sum(counts.values()), excludedTypes=sorted(excluded),
                 managedNamespaces=sorted(namespaces), managedMembersDropped=managed_dropped, typeCounts=counts)
    if access:
        info(f"MANIFEST: {len(chunks)} chunks + {len(access)} profile/permission-set access chunks")
    return True


# --------------------------------------------------------------------------- retrieve
def ensure_sfdx_project(meta_dir: Path, api_version: str) -> None:
    proj = meta_dir / "sfdx-project.json"
    if not proj.exists():
        write_json(proj, {"packageDirectories": [{"path": "force-app", "default": True}],
                          "sourceApiVersion": api_version})
    (meta_dir / "force-app").mkdir(exist_ok=True)


def component_errors(res: dict, limit: int = 50) -> list[str]:
    """Per-component problems from an `sf project retrieve start --json` payload (success or failure)."""
    out = []
    for key in ("result", "data"):
        body = res.get(key)
        if not isinstance(body, dict):
            continue
        for f in body.get("files") or []:
            if isinstance(f, dict) and (f.get("state") == "Failed" or f.get("error")):
                out.append(f"{f.get('type')}:{f.get('fullName')}: {f.get('error') or f.get('problem') or 'failed'}")
        msgs = body.get("messages") or []
        for m in msgs if isinstance(msgs, list) else [msgs]:
            if isinstance(m, dict):
                out.append(f"{m.get('fileName') or m.get('fullName') or '?'}: {m.get('problem') or m.get('message')}")
    return sorted(set(out))[:limit]


def _element_key(el) -> bytes:
    """Serialized element without its trailing whitespace (tails differ by position)."""
    tail, el.tail = el.tail, None
    try:
        return ET.tostring(el)
    finally:
        el.tail = tail


def merge_access_file(src: Path, dest: Path) -> None:
    """Copy a profile / permission set file from an access chunk. If an earlier access chunk (same
    profile, other group of related types) already wrote it, union the two: every access entry
    appears in exactly one group; shared top-level settings are identical and deduplicated."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
        return
    try:
        ET.register_namespace("", MD_NS)
        base = ET.parse(dest)
        root = base.getroot()
        seen = {_element_key(c) for c in root}
        for child in ET.parse(src).getroot():
            key = _element_key(child)
            if key not in seen:
                seen.add(key)
                root.append(child)
        ET.indent(base, space="    ")
        base.write(dest, encoding="UTF-8", xml_declaration=True)
    except ET.ParseError:
        shutil.copy2(src, dest)


def merge_staged(staged: Path, dest: Path, access: bool) -> tuple[int, list[str]]:
    """Merge a staged chunk's force-app into metadata/force-app. Main chunks: plain copy (objects are
    split across chunks by type, so folders merge rather than replace). Access chunks: only the
    profile / permission set files. Copies file by file: a file that can't be copied (most often a
    path over Windows' 260-character limit) is skipped and returned, never fatal.
    Returns (files merged, skipped paths relative to force-app)."""
    if not staged.exists():
        return 0, []
    n, skipped = 0, []
    # os.walk takes file-or-folder from the directory listing; Path.is_file() would stat the file,
    # which fails (returns False) for a path over the Windows limit and would drop it silently.
    files = [Path(d) / name for d, _, names in os.walk(staged) for name in names]
    for f in files:
        rel = f.relative_to(staged)
        if access and not any(part in ACCESS_FOLDERS for part in rel.parts[:-1]):
            continue
        try:
            if access:
                merge_access_file(f, dest / rel)
            else:
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest / rel)
            n += 1
        except OSError:
            skipped.append(rel.as_posix())
    return n, skipped


def long_paths_enabled() -> bool:
    """Windows only reads paths over 260 characters when LongPathsEnabled is set (needs admin)."""
    if sys.platform != "win32":
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
    except OSError:
        return False


def merge_all(run: Run, jobs: list[tuple[Path, bool]], access: list[Path]) -> tuple[dict, list[str]]:
    """Merge every staged chunk into metadata/force-app, main chunks first, then access chunks."""
    merged, skipped = {}, []
    for chunk, staged in jobs:
        if staged:
            n, sk = merge_staged(run.path("_staging", chunk.stem, "force-app"),
                                 run.path("metadata", "force-app"), chunk in access)
            merged[chunk.stem] = n
            skipped += sk
    if skipped:
        info(f"RETRIEVE: {len(skipped)} file(s) could not be copied and are left out of the snapshot "
             f"(see run_log.json skippedFiles){'' if long_paths_enabled() else '; Windows long paths are off'}")
    return merged, sorted(set(skipped))


def retrieve(run: Run, cfg: dict, org: str) -> bool:
    manifests = run.path("manifest")
    chunks = sorted(manifests.glob("chunk_*.xml"))
    access = sorted(manifests.glob("access_*.xml"))
    if not chunks and not access:
        info("RETRIEVE: no chunk manifests; run the manifest step first")
        return False
    api_version = ET.parse((chunks or access)[0]).getroot().findtext(f"{{{MD_NS}}}version")
    meta_dir = run.path("metadata")
    # A fresh retrieve replaces the snapshot: components deleted in the org since an earlier
    # attempt must not linger in force-app.
    shutil.rmtree(meta_dir / "force-app", ignore_errors=True)
    ensure_sfdx_project(meta_dir, api_version)
    workers = max(1, int(cfg["retrieve"].get("parallel", 1)))
    staging_root = run.path("_staging")
    shutil.rmtree(staging_root, ignore_errors=True)

    def one(job: tuple[Path, bool]) -> bool:
        chunk, staged = job
        # Staged chunks get their own SFDX project, merged into metadata/force-app afterwards: in
        # parallel so two retrieves never write the same folder at once, and always for access
        # chunks so their partial objects / classes never overwrite the main retrieve.
        cwd = meta_dir
        if staged:
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
            errors = component_errors(res)
            run.log_step(f"retrieve:{chunk.stem}", "ok" if ok else "failed", t.seconds,
                         attempt=attempt, files=files, parallel=workers,
                         error=None if ok else (res.get("message") or res.get("error")),
                         componentErrors=errors or None)
            if ok:
                info(f"RETRIEVE {chunk.stem}: {files} files in {t.seconds:.0f}s"
                     + (f" ({len(errors)} component errors)" if errors else ""))
                return True
        return False

    jobs = [(c, workers > 1) for c in chunks] + [(a, True) for a in access]
    with Timer() as total:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = dict(zip((c for c, _ in jobs), pool.map(one, jobs)))
        # Merge in order (main chunks first, then access chunks). A chunk that failed after retries
        # is still merged: whatever it staged is real metadata (serial mode keeps it in place too).
        merged, skipped = merge_all(run, jobs, access)
        shutil.rmtree(staging_root, ignore_errors=True)
    failed = [c.stem for c, ok in results.items() if not ok]
    partial = {k: n for k, n in merged.items() if k in failed and n}
    run.log_step("retrieve", "failed" if failed else "ok", total.seconds, chunks=len(chunks),
                 accessChunks=len(access), parallel=workers, failedChunks=failed,
                 partialFilesKeptFromFailedChunks=partial or None, skippedFiles=skipped or None)
    if failed:
        info(f"RETRIEVE: failed chunks {failed} (see run_log.json); continuing with partial metadata")
    return not failed


def merge_existing(run: Run) -> bool:
    staging = run.path("_staging")
    manifests = run.path("manifest")
    access = sorted(manifests.glob("access_*.xml"))
    jobs = [(c, True) for c in sorted(manifests.glob("chunk_*.xml")) + access if (staging / c.stem).is_dir()]
    if not jobs:
        info("MERGE: no staged chunks under _staging; run the retrieve step instead")
        return False
    with Timer() as t:
        shutil.rmtree(run.path("metadata", "force-app"), ignore_errors=True)
        ensure_sfdx_project(run.path("metadata"), ET.parse(jobs[0][0]).getroot().findtext(f"{{{MD_NS}}}version"))
        merged, skipped = merge_all(run, jobs, access)
        shutil.rmtree(staging, ignore_errors=True)
    run.log_step("retrieve", "ok", t.seconds, mergedFromStaging=merged, skippedFiles=skipped or None)
    info(f"MERGE: {sum(merged.values())} files from {len(jobs)} staged chunk(s)")
    return True


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
    # Success is judged by the output file existing, so a file from an earlier attempt must not count.
    for suffix in ("json", "sarif"):
        run.path("tools", f"code-analyzer.{suffix}").unlink(missing_ok=True)
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
    ap.add_argument("--steps", default=",".join(ALL_STEPS),
                    help=f"comma list of {', '.join(ALL_STEPS)}; 'merge' re-merges a staged retrieve without the org")
    ap.add_argument("--force", action="store_true", help="continue even if preflight fails")
    args = ap.parse_args()

    # The first load only finds the workspace; load_run_config then saves the client config in the run
    # folder so every later step (index, plan, review gate, render) uses it without --config.
    run = Run.create(load_config(args.config)["workspaceRoot"], args.client, args.date)
    cfg = load_run_config(run, args.config)
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
    elif "merge" in steps:
        # Recovery only: re-merge an earlier attempt's staged retrieve (<run>/_staging) without
        # contacting the org, e.g. after a crash between retrieving and merging.
        if not merge_existing(run):
            return 3
    if "tools" in steps:
        tools(run, cfg, args.org)
    print(json.dumps({"run": str(run.root)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
