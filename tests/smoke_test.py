#!/usr/bin/env python3
"""Smoke test: temp repo + worktree, fake `herdr` on PATH, run the hook.

Run: python3 tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "copy_env.py"

SHIM = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["tab", "create"]:
    ws = args[args.index("--workspace") + 1]
    label = args[args.index("--label") + 1]
    print(json.dumps({"id": "cli", "result": {
        "type": "tab_created",
        "tab": {"tab_id": ws + ":t2", "workspace_id": ws, "label": label, "number": 2, "focused": False},
        "root_pane": {"pane_id": ws + ":p7", "tab_id": ws + ":t2", "workspace_id": ws},
    }}))
elif args[:2] == ["pane", "run"]:
    print(json.dumps({"id": "cli", "result": {"type": "ok"}}))
else:
    print("unexpected", args, file=sys.stderr)
    sys.exit(2)
'''


def sh(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def event_for(main: Path, wt: Path, ws_id: str) -> str:
    return json.dumps({
        "event": "worktree.created",
        "data": {
            "type": "worktree_created",
            "workspace": {
                "workspace_id": ws_id, "label": wt.name, "number": 2, "focused": False,
                "tab_count": 1, "pane_count": 1, "active_tab_id": ws_id + ":t1",
                "worktree": {
                    "repo_key": str(main), "repo_name": "app", "repo_root": str(main),
                    "checkout_path": str(wt), "is_linked_worktree": True,
                },
            },
            "worktree": {
                "path": str(wt), "branch": wt.name, "label": wt.name, "is_bare": False,
                "is_detached": False, "is_prunable": False, "is_linked_worktree": True,
                "open_workspace_id": ws_id,
            },
        },
    })


def run_hook(env: dict, event: str) -> subprocess.CompletedProcess:
    e = dict(env, HERDR_PLUGIN_EVENT="worktree.created", HERDR_PLUGIN_EVENT_JSON=event)
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, env=e, capture_output=True, text=True)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="herdr-copy-env-"))
    main_dir = tmp / "app"
    main_dir.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=main_dir)
    sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=main_dir)

    # Local, gitignored files in the main checkout.
    (main_dir / ".env").write_text("A=1\n")
    (main_dir / ".env.local").write_text("B=2\n")
    (main_dir / "context" / "nested").mkdir(parents=True)
    (main_dir / "context" / "notes.md").write_text("notes\n")
    (main_dir / "context" / "nested" / "deep.txt").write_text("deep\n")

    # Hold port 4100 so the picker must skip it.
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 4100))
    blocker.listen()

    (main_dir / ".herdr-worktree.json").write_text(json.dumps({
        "copy": [".env", ".env.local", "context/", "missing.txt", "../escape"],
        "install": "pnpm install",
        "dev": "pnpm dev",
        "port_base": 4100,
    }))

    wt1 = tmp / "wt-one"
    wt2 = tmp / "wt-two"
    sh("git", "worktree", "add", "-q", "-b", "one", str(wt1), cwd=main_dir)
    sh("git", "worktree", "add", "-q", "-b", "two", str(wt2), cwd=main_dir)
    (wt2 / ".env").write_text("KEEP=me\n")  # must not be overwritten

    bindir = tmp / "bin"
    bindir.mkdir()
    shim = bindir / "herdr"
    shim.write_text(SHIM)
    shim.chmod(0o755)
    log_path = tmp / "herdr-calls.jsonl"
    state = tmp / "state"
    cfgdir = tmp / "config"
    cfgdir.mkdir()

    env = dict(os.environ)
    env.pop("HERDR_BIN_PATH", None)  # exercise PATH lookup
    env.update(
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        FAKE_HERDR_LOG=str(log_path),
        HERDR_PLUGIN_STATE_DIR=str(state),
        HERDR_PLUGIN_CONFIG_DIR=str(cfgdir),
        HERDR_WORKSPACE_ID="w1",  # source workspace; must NOT be used for events
    )

    r1 = run_hook(env, event_for(main_dir, wt1, "w2"))
    print(r1.stdout + r1.stderr)
    if r1.returncode != 0:
        fail(f"hook 1 exit {r1.returncode}")
    for rel, text in [(".env", "A=1\n"), (".env.local", "B=2\n"),
                      ("context/notes.md", "notes\n"), ("context/nested/deep.txt", "deep\n")]:
        p = wt1 / rel
        if not p.is_file() or p.read_text() != text:
            fail(f"{rel} not copied correctly")
    if (tmp / "escape").exists():
        fail("path traversal entry was copied")

    r2 = run_hook(env, event_for(main_dir, wt2, "w3"))
    print(r2.stdout + r2.stderr)
    if r2.returncode != 0:
        fail(f"hook 2 exit {r2.returncode}")
    if (wt2 / ".env").read_text() != "KEEP=me\n":
        fail("existing .env was overwritten")
    if not (wt2 / "context" / "nested" / "deep.txt").is_file():
        fail("context/ not copied into second worktree")

    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    print("herdr calls:")
    for c in calls:
        print("  herdr " + " ".join(c))
    expected = [
        ["tab", "create", "--workspace", "w2", "--cwd", str(wt1), "--label", "server :4101",
         "--env", "PORT=4101", "--no-focus"],
        ["pane", "run", "w2:p7", "pnpm install && pnpm dev"],
        ["tab", "create", "--workspace", "w3", "--cwd", str(wt2), "--label", "server :4102",
         "--env", "PORT=4102", "--no-focus"],
        ["pane", "run", "w3:p7", "pnpm install && pnpm dev"],
    ]
    if calls != expected:
        fail("unexpected herdr calls")
    claims = json.loads((state / "ports.json").read_text())
    if sorted(claims.values()) != [4101, 4102]:
        fail(f"unexpected port claims {claims}")

    # Backward compat: no project config => default .env list, no server step.
    (main_dir / ".herdr-worktree.json").unlink()
    wt3 = tmp / "wt-three"
    sh("git", "worktree", "add", "-q", "-b", "three", str(wt3), cwd=main_dir)
    before = len(calls)
    r3 = run_hook(env, event_for(main_dir, wt3, "w4"))
    print(r3.stdout + r3.stderr)
    if r3.returncode != 0 or not (wt3 / ".env").is_file() or (wt3 / "context").exists():
        fail("backward-compat run failed")
    if len(log_path.read_text().splitlines()) != before:
        fail("server started without dev config")

    blocker.close()
    print("PASS")


if __name__ == "__main__":
    main()
