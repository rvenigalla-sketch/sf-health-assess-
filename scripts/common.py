"""Shared helpers for sf-health scripts: config, run workspace, subprocess, logging."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
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


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int = 3600,
            env: dict | None = None) -> tuple[int, str, str]:
    """Run a command, return (code, stdout, stderr). Never raises on non-zero exit."""
    if args and args[0] == "sf":
        args = [sf_executable(), *args[1:]]
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env={**os.environ, **env} if env else None,
    )
    return proc.returncode, proc.stdout, proc.stderr


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
