# xqo-agency

`xqo agency`: import ready-made specialists from the
[agency-agents](https://github.com/msitarzewski/agency-agents) prompt library (MIT, about 280 agents in divisions such
as engineering, testing, security and marketing) into your [xqo](https://github.com/dudikkocak/xqo) HQ, and keep them
up to date. It is a CLI command only and adds nothing to a running server.

```bash
pip install xqo-agency

xqo agency list --division engineering            # browse (* = imported)
xqo agency add engineering/engineering-code-reviewer.md --skills "code review,security"
xqo agency add testing                            # a whole division (paths, globs or division names)
xqo agency update --dry-run                       # what changed upstream for your agents
xqo agency update                                 # move to the newest commit and regenerate
xqo agency remove code_reviewer                   # by path, glob or agent name
```

- **Your changes** live in `agency.json` next to agents.json: per upstream path any agent field (`skills`, `name`,
  `model`, `effort`, `can_run_code`, `write_paths`, `mcp`, ..., `department`), plus `ref`, `adapter` and
  `departments`. Run `xqo agency sync` after editing it. Generated agents carry `"_agency"` and are rewritten on every
  sync; hand-written agents are never touched.
- **Pinned**: `agency.lock.json` records the upstream commit and a hash of each imported file. Only `update` moves it.
- **Safe**: the result is loaded once before it counts, and any error puts every file back. `--source <checkout>`
  reads a local clone instead of fetching.
- **Scheduled check**: `xqo agency update --check` writes nothing and exits 1 when one of your agents changed upstream.
- **Licence**: upstream's licence is saved as `AGENCY_AGENTS_LICENSE` next to agents.json. `xqo backup --scope config`
  keeps the three agency files.
