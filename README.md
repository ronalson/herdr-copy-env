# herdr-copy-env

Small Herdr plugin that copies local `.env` files into a new Git worktree.

Herdr does not copy gitignored files when it creates a worktree. This plugin hooks `worktree.created` and copies env files from a sibling checkout of the same repo.

## Install

From GitHub:

```bash
herdr plugin install ronalson/herdr-copy-env
```

Or clone and link while developing:

```bash
git clone https://github.com/ronalson/herdr-copy-env.git
herdr plugin link ./herdr-copy-env
```

Requires Herdr `0.9.0+`, Python 3, and Git. Platforms: Linux and macOS.

## What it copies

Defaults:

- `.env`
- `.env.local`
- `.env.development`
- `.env.development.local`

It does **not** overwrite existing files unless you turn that on.

Source checkout: another non-bare worktree of the same repo that already has one of those files (usually your `main` worktree). Bare clones are skipped on purpose.

## Manual action

From a worktree workspace in Herdr, run **Copy .env into this worktree**.

## Optional config

After install, Herdr creates a config directory for this plugin (`HERDR_PLUGIN_CONFIG_DIR`). Put `config.json` there:

```json
{
  "files": [".env", ".env.local"],
  "overwrite": false
}
```

See `config.example.json`.

## Notes

- The hook is asynchronous. The new worktree pane already exists when the copy starts.
- Keep secrets out of Git. This only copies local files between your own checkouts.
