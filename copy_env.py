#!/usr/bin/env python3
"""Set up a new Herdr worktree: copy local files, pick a port, start the server.

Runs on worktree.created (HERDR_PLUGIN_EVENT_JSON) or as a workspace action
(HERDR_PLUGIN_CONTEXT_JSON).

1. Copies gitignored local files and directories (.env*, context/, ...) from
   the main checkout (or another non-bare sibling worktree). Never overwrites
   by default. Never reads from a bare repo.
2. If the project config sets `dev`, picks a free TCP port starting at
   `port_base`, opens a new Herdr tab named `server :<port>` in the worktree
   with PORT=<port>, and runs `<install> && <dev>` in it.

Config, lowest to highest priority:
  built-in defaults
  $HERDR_PLUGIN_CONFIG_DIR/config.json        (global defaults)
  <main checkout>/.herdr-worktree.json        (per project)
  <new worktree>/.herdr-worktree.json         (per project, if committed)

Python 3 standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_CONFIG = ".herdr-worktree.json"

DEFAULT_COPY = [
    ".env",
    ".env.local",
    ".env.development",
    ".env.development.local",
]

DEFAULTS: dict = {
    "copy": list(DEFAULT_COPY),
    "install": None,
    "dev": None,
    "port_base": 3000,
    "overwrite": False,
    "tab_label": "server :{port}",
}

PORT_SCAN_LIMIT = 200


def log(msg: str) -> None:
    print(f"copy-env: {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"copy-env: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- input -----


def load_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as err:
        warn(f"invalid JSON: {err}")
        return {}
    return value if isinstance(value, dict) else {}


def read_json_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        warn(f"could not read {path}: {err}")
        return None
    if not isinstance(data, dict):
        warn(f"ignoring {path}: expected a JSON object")
        return None
    return data


def normalize_config(data: dict) -> dict:
    """Keep only known keys with sane types. `files` is the 0.1.x name of `copy`."""
    out: dict = {}
    copy = data.get("copy", data.get("files"))
    if isinstance(copy, list) and copy:
        out["copy"] = [str(item) for item in copy if str(item).strip()]
    for key in ("install", "dev", "tab_label"):
        if key in data:
            value = data[key]
            out[key] = str(value).strip() if value else None
    if "port_base" in data:
        try:
            port = int(data["port_base"])
            if 1 <= port <= 65535:
                out["port_base"] = port
            else:
                warn(f"port_base out of range: {port}")
        except (TypeError, ValueError):
            warn(f"invalid port_base: {data['port_base']!r}")
    if "overwrite" in data:
        out["overwrite"] = bool(data["overwrite"])
    return out


def global_config() -> dict:
    cfg_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not cfg_dir:
        return {}
    data = read_json_file(Path(cfg_dir) / "config.json")
    return normalize_config(data) if data else {}


def build_config(source: Path | None, dest: Path) -> tuple[dict, list[Path]]:
    cfg = dict(DEFAULTS)
    cfg.update(global_config())
    used: list[Path] = []
    for root in (source, dest):
        if root is None:
            continue
        path = root / PROJECT_CONFIG
        data = read_json_file(path)
        if data is not None:
            cfg.update(normalize_config(data))
            used.append(path)
    return cfg, used


# ------------------------------------------------- resolve paths / ids -----


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def event_dest(event: dict) -> Path | None:
    data = as_dict(event.get("data"))
    worktree = as_dict(data.get("worktree"))
    if worktree.get("path"):
        return Path(str(worktree["path"]))
    wt = as_dict(as_dict(data.get("workspace")).get("worktree"))
    if wt.get("checkout_path"):
        return Path(str(wt["checkout_path"]))
    return None


def context_dest(context: dict) -> Path | None:
    wt = as_dict(context.get("worktree"))
    for key in ("checkout_path", "path"):
        if wt.get(key):
            return Path(str(wt[key]))
    for key in ("workspace_cwd", "focused_pane_cwd"):
        if context.get(key):
            return Path(str(context[key]))
    return None


def repo_root_hint(event: dict, context: dict) -> Path | None:
    """Main checkout path that Herdr reports for the worktree group."""
    data = as_dict(event.get("data"))
    for wt in (
        as_dict(as_dict(data.get("workspace")).get("worktree")),
        as_dict(context.get("worktree")),
    ):
        if wt.get("repo_root"):
            return Path(str(wt["repo_root"]))
    return None


def workspace_id(event: dict, context: dict, is_event: bool) -> str | None:
    data = as_dict(event.get("data"))
    candidates = [
        as_dict(data.get("workspace")).get("workspace_id"),
        as_dict(data.get("worktree")).get("open_workspace_id"),
        data.get("workspace_id"),
        context.get("workspace_id"),
    ]
    # For actions, HERDR_WORKSPACE_ID is the workspace the action runs in.
    # For event hooks it may describe a different workspace, so skip it.
    if not is_event:
        candidates.append(os.environ.get("HERDR_WORKSPACE_ID"))
    for value in candidates:
        if value:
            return str(value)
    return None


# --------------------------------------------------------------- source -----


def is_bare_git_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and not (path / ".git").exists()


def worktree_paths(dest: Path) -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(dest), "worktree", "list", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as err:
        warn(f"git worktree list failed: {err}")
        return []
    if proc.returncode != 0:
        warn(proc.stderr.strip() or "git worktree list failed")
        return []
    paths: list[Path] = []
    bare: set[str] = set()
    current = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :]
            paths.append(Path(current))
        elif line == "bare" and current:
            bare.add(current)
    return [p for p in paths if str(p) not in bare]


def same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def pick_source(dest: Path, names: list[str], hint: Path | None) -> Path | None:
    if hint and hint.is_dir() and not same_path(hint, dest) and not is_bare_git_dir(hint):
        return hint
    ranked = [
        p
        for p in worktree_paths(dest)
        if not same_path(p, dest) and p.is_dir() and not is_bare_git_dir(p)
    ]
    for path in ranked:
        if any((path / name.rstrip("/")).exists() for name in names):
            return path
    return ranked[0] if ranked else None


# ----------------------------------------------------------------- copy -----


def safe_rel(name: str) -> Path | None:
    rel = Path(name.strip().rstrip("/"))
    if not str(rel) or rel.is_absolute() or ".." in rel.parts or str(rel) == ".":
        warn(f"skip {name!r}: entries must be relative paths inside the repo")
        return None
    return rel


def copy_one_file(src: Path, dst: Path, overwrite: bool) -> str:
    if dst.exists() and not overwrite:
        return "skipped"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return "copied"


def copy_entries(source: Path, dest: Path, names: list[str], overwrite: bool) -> int:
    copied = skipped = missing = 0
    for name in names:
        rel = safe_rel(name)
        if rel is None:
            continue
        src = source / rel
        dst = dest / rel
        try:
            if src.is_dir():
                n_copied = n_skipped = 0
                for root, dirs, files in os.walk(src):
                    dirs[:] = [d for d in dirs if d != ".git"]
                    root_path = Path(root)
                    target_root = dst / root_path.relative_to(src)
                    target_root.mkdir(parents=True, exist_ok=True)
                    for fname in files:
                        result = copy_one_file(
                            root_path / fname, target_root / fname, overwrite
                        )
                        if result == "copied":
                            n_copied += 1
                        else:
                            n_skipped += 1
                copied += n_copied
                skipped += n_skipped
                log(f"copied {rel}/ ({n_copied} files, {n_skipped} already present) from {source}")
            elif src.is_file():
                if copy_one_file(src, dst, overwrite) == "copied":
                    log(f"copied {rel} from {source}")
                    copied += 1
                else:
                    log(f"skip {rel} (already exists)")
                    skipped += 1
            else:
                missing += 1
        except OSError as err:
            warn(f"failed to copy {rel}: {err}")
            return 1
    if copied == 0 and skipped == 0:
        log(f"none of {names} found in {source}")
    elif copied == 0:
        log("nothing new to copy")
    return 0


# ----------------------------------------------------------------- ports ----


def port_is_free(port: int) -> bool:
    for host in ("127.0.0.1", "0.0.0.0"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        finally:
            sock.close()
    return True


def ports_state_path() -> Path | None:
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return Path(state_dir) / "ports.json" if state_dir else None


def load_claims() -> dict[str, int]:
    path = ports_state_path()
    data = read_json_file(path) if path else None
    claims: dict[str, int] = {}
    for key, value in (data or {}).items():
        # Forget worktrees that were removed.
        if isinstance(value, int) and Path(key).is_dir():
            claims[key] = value
    return claims


def save_claims(claims: dict[str, int]) -> None:
    path = ports_state_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(claims, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as err:
        warn(f"could not save port claims: {err}")


def pick_port(base: int, dest: Path) -> int | None:
    claims = load_claims()
    key = str(dest.resolve())
    taken = {port for path, port in claims.items() if path != key}
    previous = claims.get(key)
    if previous and previous not in taken and port_is_free(previous):
        return previous
    for port in range(base, min(base + PORT_SCAN_LIMIT, 65536)):
        if port in taken:
            continue
        if port_is_free(port):
            claims[key] = port
            save_claims(claims)
            return port
    return None


# ---------------------------------------------------------------- herdr -----


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_call(args: list[str]) -> dict | None:
    cmd = [herdr_bin(), *args]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError as err:
        warn(f"could not run {cmd[0]}: {err}")
        return None
    if proc.returncode != 0:
        warn(f"{' '.join(args[:2])} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
        return None
    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        value = json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}
    return value if isinstance(value, dict) else {"raw": value}


def start_server(cfg: dict, dest: Path, ws_id: str | None) -> int:
    dev = cfg.get("dev")
    if not dev:
        return 0
    if not ws_id:
        warn("dev is set but the worktree's Herdr workspace id is unknown; not starting the server")
        return 1
    port = pick_port(int(cfg.get("port_base") or 3000), dest)
    if port is None:
        warn(f"no free port found from {cfg.get('port_base')}")
        return 1
    label = (cfg.get("tab_label") or DEFAULTS["tab_label"]).replace("{port}", str(port))
    created = herdr_call(
        [
            "tab", "create",
            "--workspace", ws_id,
            "--cwd", str(dest),
            "--label", label,
            "--env", f"PORT={port}",
            "--no-focus",
        ]
    )
    if created is None:
        return 1
    pane_id = as_dict(as_dict(created.get("result")).get("root_pane")).get("pane_id")
    if not pane_id:
        warn(f"tab create returned no root pane id: {json.dumps(created)[:300]}")
        return 1
    install = cfg.get("install")
    command = f"{install} && {dev}" if install else dev
    if herdr_call(["pane", "run", str(pane_id), command]) is None:
        return 1
    log(f"started server in tab '{label}' (pane {pane_id}) with PORT={port}: {command}")
    return 0


# ----------------------------------------------------------------- main -----


def main() -> int:
    event = load_json(os.environ.get("HERDR_PLUGIN_EVENT_JSON"))
    context = load_json(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON"))
    is_event = bool(event)

    dest = event_dest(event) or context_dest(context)
    if dest is None:
        warn("could not resolve worktree path")
        return 1
    if not dest.is_dir():
        warn(f"destination does not exist: {dest}")
        return 1
    if is_bare_git_dir(dest):
        warn(f"refusing bare git dir: {dest}")
        return 1

    hint = repo_root_hint(event, context)
    # First pass with global/default names to find a source checkout.
    pre_cfg, _ = build_config(None, dest)
    source = pick_source(dest, pre_cfg["copy"] + [PROJECT_CONFIG], hint)
    cfg, used = build_config(source, dest)
    for path in used:
        log(f"using {path}")

    status = 0
    if source is None:
        warn(
            "no sibling worktree found to copy from; "
            "open the main checkout as a worktree group first, then retry"
        )
        status = 1
    else:
        status = copy_entries(source, dest, cfg["copy"], cfg["overwrite"])

    server_status = start_server(cfg, dest, workspace_id(event, context, is_event))
    return status or server_status


if __name__ == "__main__":
    raise SystemExit(main())
