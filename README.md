# xqo

A small, plugin-based HQ of Claude agents with a live dashboard and a 2D / 3D office view.

This repository is the xqo monorepo. Each folder under `packages/` is one package on PyPI, and all packages are
released together with the same version (starting at 3.0.0).

| Package | Folder | What it is |
|---|---|---|
| `xqo` | [packages/xqo](packages/xqo) | the base package: core, office plugin, Claude Agent SDK, jsonl event store |
| `xqo-sqlite` | [packages/xqo-sqlite](packages/xqo-sqlite) | SQLite event store (`"storage": {"backend": "sqlite"}`) |
| `xqo-postgres` | [packages/xqo-postgres](packages/xqo-postgres) | PostgreSQL event store, for several servers sharing one store |
| `xqo-docker` | [packages/xqo-docker](packages/xqo-docker) | Docker sandbox backend for `run_code` |
| `xqo-schedules`, `xqo-feeds`, `xqo-channels`, `xqo-peering`, `xqo-toolchains`, `xqo-agency` | `packages/xqo-<name>` (coming) | optional plugins |

```bash
pip install xqo            # the base package
pip install "xqo[all]"     # with every plugin (once the plugin packages are published)
```

Start with [packages/xqo/README.md](packages/xqo/README.md). Writing a plugin: [packages/xqo/PLUGINS.md](packages/xqo/PLUGINS.md).

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e "packages/xqo[dev]"
python -m pytest packages/xqo/tests -q --timeout 120
python tools/check_wheel.py packages/xqo    # builds the wheel and checks that the browser files are in it

# a plugin package: install it next to the base package, in its own venv (the base tests expect no plugins)
uv pip install -e "packages/xqo[dev]" -e "packages/xqo-sqlite[dev]"
python -m pytest packages/xqo-sqlite/tests -q
```

The postgres tests need a database: set `XQO_TEST_PG_DSN` (they skip without it). `xqo.testing` has the shared
checks a storage backend must pass (`check_event_store`, `check_restart_keeps_tasks`).

Plugins import the core only through `xqo.api`; CI tests every package against an installed `xqo` wheel.

## Releasing

All packages share one version. Bump it in every `packages/*/pyproject.toml` (the base package's is
`xqo/__init__.py`) and in the base package's plugin extras, merge to `main`, then push a tag:

```bash
git tag v3.0.0 && git push origin v3.0.0
```

The `release` workflow checks the versions against the tag, builds every package and publishes them to PyPI with
trusted publishing (no token in the repository). Each package name needs a trusted publisher on PyPI: owner
`dudikkocak`, repository `xqo`, workflow `release.yml`, environment `pypi`.

## License

MIT, see [LICENSE](LICENSE).
