# herdr-copy-env

Herdr plugin that gets a new Git worktree ready to run, without prompting an agent:

1. Copies your local, gitignored files and folders (`.env`, `.env.local`, `context/`, ...) from the main checkout into the new worktree.
2. Picks a free port, starting at `port_base` (default 3000).
3. Opens a new tab in the worktree's workspace named `server :<port>`, with `PORT=<port>` set, and runs your install and dev commands there. Focus stays where it was.

It runs automatically on `worktree.created`. You can also run it by hand from any workspace with **Set up this worktree (copy files, start server)**.

## Install

```bash
herdr plugin install ronalson/herdr-copy-env
```

Or clone and link while developing:

```bash
git clone https://github.com/ronalson/herdr-copy-env.git
herdr plugin link ./herdr-copy-env
```

Requires Herdr `0.9.0+`, Python 3 (standard library only), and Git. Linux and macOS.

## Per-project config

Add `.herdr-worktree.json` to the root of your project (see `.herdr-worktree.example.json`):

```json
{
  "copy": [".env", ".env.local", "context/"],
  "install": "pnpm install",
  "dev": "pnpm dev",
  "port_base": 3000,
  "overwrite": false
}
```

| Key | Meaning | Default |
| --- | --- | --- |
| `copy` | Files and folders to copy, relative to the repo root. Folders are copied recursively. | `.env`, `.env.local`, `.env.development`, `.env.development.local` |
| `install` | Shell command run before `dev`, e.g. `pnpm install`. Optional. | none |
| `dev` | Shell command that starts the server. If it's missing, no tab is opened. | none |
| `port_base` | First port to try. The plugin moves up until it finds a free one. | `3000` |
| `overwrite` | Replace files that already exist in the worktree. | `false` |
| `tab_label` | Tab name. `{port}` is replaced with the port. | `server :{port}` |

The plugin reads the file from the main checkout and then from the new worktree, so you can keep it local (gitignored) or commit it. If both exist, the worktree's values win.

The tab runs `<install> && <dev>`, so the server only starts if install succeeds.

### Your dev server must read `PORT`

The plugin sets `PORT` in the new tab's shell. Next.js, Express, and most Node servers read it automatically. Vite does not, so pass it through, e.g. `"dev": "pnpm vite --port $PORT"`, or read `process.env.PORT` in `vite.config`.

## How it picks the port

Starting at `port_base`, a port is used only if it is free on `127.0.0.1` and `0.0.0.0` and no other worktree has already claimed it. Claims are stored in the plugin's state directory (`HERDR_PLUGIN_STATE_DIR/ports.json`) and dropped once a worktree's folder is gone.

## Where files are copied from

The main checkout that Herdr reports for the worktree group (`repo_root`). If that's not available, another non-bare worktree of the same repo, preferring one that already has the files. Bare clones are never used.

## Global defaults (optional)

Put `config.json` in the plugin's config directory (`herdr plugin config-dir ronalson.copy-env`). It accepts the same keys and applies to every project; a project's `.herdr-worktree.json` overrides it. The old `files` key from 0.1.x still works as an alias for `copy`. See `config.example.json`.

## Notes

- With no `.herdr-worktree.json` and no global config, the plugin behaves like 0.1.x: it copies the default `.env` files and starts nothing.
- The hook runs asynchronously, so the worktree's first pane may already be open when copying starts.
- Running the manual action again opens another server tab on a new port.
- Keep secrets out of Git. This only copies local files between your own checkouts.
- Logs: `herdr plugin log list --plugin ronalson.copy-env`.

## Development

```bash
python3 tests/smoke_test.py
```

The smoke test builds a temporary repo with worktrees, puts a fake `herdr` on `PATH`, runs the hook, and checks the copied files, chosen ports, and the `tab create` / `pane run` calls.
