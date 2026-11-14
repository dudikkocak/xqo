# xqo-toolchains

Node (including Nuxt and Next), Go, Rust and .NET for [xqo](https://github.com/dudikkocak/xqo)'s `run_code`, next to
the Python venv the base package already makes.

```bash
pip install xqo-toolchains
```

Before each run it:

- **splits the workspace into components**: folders with `package.json`, `go.mod`, `Cargo.toml` or `*.sln`/`*.csproj`,
  so a Go service with a Nuxt app in `web/` is two components;
- **picks versions** from `.nvmrc`, `.node-version`, `package.json` engines, `go.mod`, `global.json`,
  `rust-toolchain.toml`, `.tool-versions` and `mise.toml`. With `mise` on PATH they are installed once and shared;
  without it the host's toolchains are used and a mismatch is reported;
- **installs dependencies** only when a manifest or lockfile changed: `npm ci` / `pnpm` / `yarn` / `bun`,
  `go mod download`, `cargo fetch`, `dotnet restore`. Download caches are shared by all tasks, build outputs belong
  to the task.

With [xqo-docker](https://pypi.org/project/xqo-docker/) installed, runs use each project's official image at the
version it asks for (`node:<v>-bookworm`, `golang:<v>-bookworm`, `rust:<channel>-bookworm`,
`mcr.microsoft.com/dotnet/sdk:<v>`), and a mixed project gets a composite image built once.

| `sandbox` key | Default | |
|---|---|---|
| `toolchains` | `true` | `false` turns the setup off for this HQ |
| `versions` | `auto` | `auto` (mise if installed, else the host's), `mise` or `host` |
| `cache_dir` | `~/.cache/xqo/toolchains` | shared download caches (or `XQO_CACHE_DIR`) |
| `node_modules` | `auto` | `auto` (link; a real copy for Next), `link` or `clone` |
| `images` | | per-toolchain image overrides for the docker backend, e.g. `{"node": "node:20-bookworm"}` |

The local backend is not a security boundary: commands run as xqo's user, with network. Use xqo-docker for isolation.
