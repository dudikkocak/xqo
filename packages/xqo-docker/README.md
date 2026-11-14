# xqo-docker

The `docker` sandbox backend for [xqo](https://github.com/dudikkocak/xqo)'s `run_code`: each run happens in a
throwaway container with no network (unless the user grants it), memory, CPU and process limits, and the host
user's uid. Python projects get a uv venv inside the image; Node, Go, Rust and .NET projects use their official
images when the toolchains plugin is installed.

```bash
pip install xqo-docker
```

```json
"sandbox": {"backend": "docker"}
```

With the plugin installed, `"backend": "auto"` picks Docker whenever the daemon answers and falls back to `local`
otherwise. `"plugins": {"disable": ["docker"]}` turns it off without uninstalling. `/api/plugins` shows whether the
daemon answered (`status.daemon`).

Needs the `docker` CLI and a running daemon (Docker Desktop, Docker Engine or a compatible one). The image is set
with `sandbox.image` (default `astral/uv:python3.12-bookworm-slim`); it needs `uv` and `python` on its PATH.
