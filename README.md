# xqo

A small, plugin-based HQ of Claude agents with a live dashboard and a 2D / 3D office view.

This repository is the xqo monorepo. Each folder under `packages/` is one package on PyPI, and all packages are
released together with the same version (starting at 3.0.0).

| Package | Folder | What it is |
|---|---|---|
| `xqo` | [packages/xqo](packages/xqo) | the base package: core, office plugin, Claude Agent SDK, jsonl event store |
| `xqo-schedules`, `xqo-feeds`, `xqo-channels`, `xqo-peering`, `xqo-toolchains`, `xqo-docker`, `xqo-agency`, `xqo-sqlite`, `xqo-postgres` | `packages/xqo-<name>` (coming) | optional plugins |

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
```

Plugins import the core only through `xqo.api`; CI tests every package against an installed `xqo` wheel.

## License

MIT, see [LICENSE](LICENSE).
