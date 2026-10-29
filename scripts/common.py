"""Shared helpers for sf-health scripts: config, run workspace, subprocess, logging."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PLUGIN_ROOT / "config" / "default.yml"

# Rough chars-per-token for code and XML; used only for shard sizing.
CHARS_PER_TOKEN = 3.5


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None = None) -> dict:
    cfg = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    if path:
        cfg = deep_merge(cfg, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    if cfg.get("dataPolicy") not in ("full", "metadata-only"):
        raise ValueError(f"dataPolicy must be 'full' or 'metadata-only', got {cfg.get('dataPolicy')!r}")
    return cfg


def load_run_config(run: "Run", path: str | None = None) -> dict:
    """Config for a run. The client config passed to the first step is saved as <run>/client-config.yml;
    later steps reuse it when --config is omitted, so a setting like library.enabled: false can't be
    lost by forgetting the flag. Passing --config again replaces the saved copy."""
    saved = run.path("client-config.yml")
    if path:
        text = Path(path).read_text(encoding="utf-8")
        if not saved.exists() or saved.read_text(encoding="utf-8") != text:
            saved.write_text(text, encoding="utf-8")
        return load_config(path)
    return load_config(str(saved) if saved.exists() else None)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-") or "client"


class Run:
    """A run workspace: <root>/<client>/<date>/{metadata,tools,index,shards,findings,review,report}."""

    SUBDIRS = ("manifest", "metadata", "tools", "index", "shards", "findings", "review", "report")
    # Collection runs some steps in threads; the log file is read-modify-written, so serialize it.
    _log_lock = threading.Lock()

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        for sub in self.SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "run_log.json"

    @classmethod
    def create(cls, workspace_root: str, client: str, date: str | None = None) -> "Run":
        date = date or dt.date.today().isoformat()
        return cls(Path(workspace_root) / slug(client) / date)

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    # --- run log: one entry per step, with status, timing and notes -------------
    def _read_log(self) -> dict:
        if self.log_path.exists():
            return json.loads(self.log_path.read_text(encoding="utf-8"))
        return {"steps": []}

    def log_step(self, name: str, status: str, seconds: float, **details) -> None:
        with self._log_lock:
            log = self._read_log()
            log["steps"].append({
                "step": name,
                "status": status,
                "seconds": round(seconds, 1),
                "at": dt.datetime.now().isoformat(timespec="seconds"),
                **details,
            })
            self.log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")

    def set_meta(self, **meta) -> None:
        with self._log_lock:
            log = self._read_log()
            log.setdefault("meta", {}).update(meta)
            self.log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")


def sf_executable() -> str:
    exe = shutil.which("sf") or shutil.which("sf.cmd")
    if not exe:
        raise RuntimeError("Salesforce CLI 'sf' not found on PATH")
    return exe


TIMEOUT_CODE = 124      # like coreutils `timeout`
NOT_STARTED_CODE = 127  # command not found / could not start


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and its children. On Windows `sf` is sf.cmd -> cmd.exe -> node, and killing
    only cmd.exe leaves node running and holding the output pipes, so communicate() never returns."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        else:
            os.killpg(proc.pid, signal.SIGKILL)   # the child leads its own session (start_new_session)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int = 3600,
            env: dict | None = None) -> tuple[int, str, str]:
    """Run a command, return (code, stdout, stderr). Never raises: a timeout kills the whole process
    tree and returns code 124; a command that can't be started returns 127. (Ctrl+C still propagates.)"""
    if args and args[0] == "sf":
        try:
            args = [sf_executable(), *args[1:]]
        except RuntimeError as e:
            return NOT_STARTED_CODE, "", str(e)
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, **env} if env else None,
            start_new_session=os.name != "nt",
        )
    except OSError as e:
        return NOT_STARTED_CODE, "", f"could not start {args[0] if args else '?'}: {e}"
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out, err = "", ""
        msg = f"TIMEOUT: command did not finish within {timeout}s and was killed: {' '.join(map(str, args))[:300]}"
        return TIMEOUT_CODE, out or "", ((err or "") + "\n" + msg).lstrip("\n")
    except BaseException:
        _kill_tree(proc)    # e.g. KeyboardInterrupt: don't leave an orphaned sf/node process behind
        raise


def run_sf_json(args: list[str], cwd: Path | None = None, timeout: int = 3600,
                env: dict | None = None) -> tuple[bool, dict]:
    """Run an sf command with --json. Returns (ok, parsed_json_or_error_dict)."""
    code, out, err = run_cmd(["sf", *args, "--json"], cwd=cwd, timeout=timeout, env=env)
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else {}
    except json.JSONDecodeError:
        data = {}
    ok = code == 0 and data.get("status", 0) == 0
    if not ok and not data:
        data = {"error": (err or out)[-2000:]}
    return ok, data


class Timer:
    """Elapsed time; `seconds` is readable both inside and after the with-block."""

    def __enter__(self):
        self.start = time.monotonic()
        self.end = None
        return self

    def __exit__(self, *exc):
        self.end = time.monotonic()
        return False

    @property
    def seconds(self) -> float:
        return (self.end or time.monotonic()) - self.start


class Progress:
    """Prints `PROGRESS <stage> done/total pct% [####------] detail` after each unit of a long step,
    so the assess command can relay how far it has got. ASCII only (Windows consoles). Thread-safe."""

    WIDTH = 20

    def __init__(self, stage: str, total: int):
        self.stage, self.total, self.done = stage, max(0, total), 0
        self._lock = threading.Lock()

    def tick(self, detail: str = "") -> None:
        with self._lock:
            self.done += 1
            total = max(self.total, self.done)
            pct = int(100 * self.done / total) if total else 100
            bar = "#" * (pct * self.WIDTH // 100)
            info(f"PROGRESS {self.stage} {self.done}/{total} {pct}% [{bar:-<{self.WIDTH}}] {detail}".rstrip())


def content_hash(text: str) -> str:
    """Short content hash for incremental runs (line endings normalized, so a re-retrieve on
    another OS doesn't look like a change)."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8", "replace")).hexdigest()[:16]


def earlier_run(run: "Run", needs: str = "index/inventory.json") -> Path | None:
    """Newest earlier date folder of the same client that has `needs` (the previous snapshot)."""
    earlier = sorted(p for p in run.root.parent.iterdir()
                     if p.is_dir() and p.name < run.root.name and (p / needs).exists())
    return earlier[-1] if earlier else None


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path, default=None):
    if not Path(path).exists():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def info(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
