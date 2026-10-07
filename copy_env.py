#!/usr/bin/env python3
"""Copy local env files into a Herdr worktree checkout.

Runs on worktree.created (HERDR_PLUGIN_EVENT_JSON) or as a workspace action
(HERDR_PLUGIN_CONTEXT_JSON). Never overwrites by default. Never reads secrets
from a bare repo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_FILES = [
    ".env",
    ".env.local",
    ".env.development",
    ".env.development.local",
]


def load_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as err:
        print(f"copy-env: invalid JSON: {err}", file=sys.stderr)
        return {}
    return value if isinstance(value, dict) else {}


def load_config() -> dict:
    cfg_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not cfg_dir:
        return {"files": list(DEFAULT_FILES), "overwrite": False}
    path = Path(cfg_dir) / "config.json"
    if not path.is_file():
        return {"files": list(DEFAULT_FILES), "overwrite": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        print(f"copy-env: could not read {path}: {err}", file=sys.stderr)
        return {"files": list(DEFAULT_FILES), "overwrite": False}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        files = list(DEFAULT_FILES)
    overwrite = bool(data.get("overwrite")) if isinstance(data, dict) else False
    return {"files": [str(f) for f in files], "overwrite": overwrite}


def event_dest(event: dict) -> Path | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    worktree = data.get("worktree")
    if isinstance(worktree, dict) and worktree.get("path"):
        return Path(str(worktree["path"]))
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        wt = workspace.get("worktree")
        if isinstance(wt, dict) and wt.get("checkout_path"):
            return Path(str(wt["checkout_path"]))
    return None


def context_dest(context: dict) -> Path | None:
    worktree = context.get("worktree")
    if isinstance(worktree, dict):
        for key in ("path", "checkout_path"):
            if worktree.get(key):
                return Path(str(worktree[key]))
    workspace = context.get("workspace")
    if isinstance(workspace, dict):
        wt = workspace.get("worktree")
        if isinstance(wt, dict) and wt.get("checkout_path"):
            return Path(str(wt["checkout_path"]))
        cwd = workspace.get("cwd")
        if cwd:
            return Path(str(cwd))
    return None


def is_bare_git_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "HEAD").is_file() and (path / "objects").is_dir() and not (path / ".git").exists():
        return True
    return False


def worktree_paths(dest: Path) -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(dest), "worktree", "list", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as err:
        print(f"copy-env: git worktree list failed: {err}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(proc.stderr.strip() or "copy-env: git worktree list failed", file=sys.stderr)
        return []
    paths: list[Path] = []
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree ") :]))
    return paths


def pick_source(dest: Path, names: list[str]) -> Path | None:
    candidates = [p for p in worktree_paths(dest) if p.resolve() != dest.resolve()]
    ranked: list[Path] = []
    for path in candidates:
        if is_bare_git_dir(path):
            continue
        ranked.append(path)
    # Prefer a checkout that already has any of the requested files.
    for path in ranked:
        if any((path / name).is_file() for name in names):
            return path
    return ranked[0] if ranked else None


def copy_files(source: Path, dest: Path, names: list[str], overwrite: bool) -> int:
    copied = 0
    skipped = 0
    missing = 0
    for name in names:
        src = source / name
        dst = dest / name
        if not src.is_file():
            missing += 1
            continue
        if dst.exists() and not overwrite:
            print(f"copy-env: skip {name} (already exists)")
            skipped += 1
            continue
        try:
            shutil.copy2(src, dst)
        except OSError as err:
            print(f"copy-env: failed to copy {name}: {err}", file=sys.stderr)
            return 1
        print(f"copy-env: copied {name} from {source}")
        copied += 1
    if copied == 0 and missing == len(names):
        print(f"copy-env: no env files found in {source}")
    elif copied == 0 and skipped:
        print("copy-env: nothing new to copy")
    return 0


def main() -> int:
    event = load_json(os.environ.get("HERDR_PLUGIN_EVENT_JSON"))
    context = load_json(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON"))
    cfg = load_config()

    dest = event_dest(event) or context_dest(context)
    if dest is None:
        print("copy-env: could not resolve worktree path", file=sys.stderr)
        return 1
    if not dest.is_dir():
        print(f"copy-env: destination does not exist: {dest}", file=sys.stderr)
        return 1
    if is_bare_git_dir(dest):
        print(f"copy-env: refusing bare git dir: {dest}", file=sys.stderr)
        return 1

    source = pick_source(dest, cfg["files"])
    if source is None:
        print(
            "copy-env: no sibling worktree with env files found; "
            "create one worktree that already has .env, then retry",
            file=sys.stderr,
        )
        return 1

    return copy_files(source, dest, cfg["files"], cfg["overwrite"])


if __name__ == "__main__":
    raise SystemExit(main())
