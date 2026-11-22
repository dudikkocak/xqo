# Sample setup: one HQ, one remote worker

| File | Where | What |
|---|---|---|
| `hq.agents.json` | HQ | Turns flows on, opens the worker API on port 8200 and lists two workers with their labels |
| `worker.agents.json` | worker | The worker's own agents and its `flows_worker` block (HQ address, name, token variable) |
| `plan-and-build.json` | HQ | A flow: plan on the HQ, then build on a `gpu` worker, then your approval, then a write-up |
| `env.example` | both | The token variables |
| `docker-compose.hq.yml` | HQ | Overlay for `packages/xqo/docker-compose.yml` that publishes the worker API |
| `docker-compose.worker.yml` | worker | A worker container with no published port |

## Without Docker

```bash
# HQ
pip install "xqo[flows]"
export XQO_WORKER_GPU_BOX_TOKEN=$(xqo flows token)       # note it: the worker needs the same value
xqo --config hq.agents.json serve                        # dashboard on 127.0.0.1:8000, worker API on :8200
xqo flows import plan-and-build.json

# worker (another machine)
pip install xqo-flows-runner
export XQO_WORKER_TOKEN=<the same token> ANTHROPIC_API_KEY=...
# edit "hq" in worker.agents.json to the HQ's address
xqo --config worker.agents.json worker

# back on the HQ
xqo flows workers                                        # gpu-box  online  labels: gpu, linux
xqo flows run plan-and-build -i what="a CSV parser with tests"
```

The run stops at "Looks good?". Answer it in the editor (Runs tab), or run
`xqo flows approve RUN_ID ok` (add `--reject` to stop instead).

## With Docker

```bash
# HQ, from packages/xqo (copy hq.agents.json to ./data/agents.json, tokens in .env)
XQO_PLUGINS="xqo-flows" docker compose -f docker-compose.yml -f ../xqo-flows/examples/setup/docker-compose.hq.yml up -d --build

# worker, from packages/xqo on the worker machine (worker.agents.json -> ./data/agents.json, token in .env)
docker compose -f ../xqo-flows/examples/setup/docker-compose.worker.yml up -d --build
```

## Trying it on one machine without a key

```bash
export XQO_WORKER_GPU_BOX_TOKEN=test XQO_WORKER_TOKEN=test
xqo --config hq.agents.json --data-dir /tmp/hq --mock serve &
# point "hq" in worker.agents.json at http://127.0.0.1:8200 first
xqo --config worker.agents.json --data-dir /tmp/worker --mock --departments none worker &
xqo flows import plan-and-build.json && xqo flows run plan-and-build
```

`--mock` uses the scripted offline model, so the answers are canned, but every step, worker job, approval and
live status in the editor is real.
