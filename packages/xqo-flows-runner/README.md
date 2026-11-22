# xqo-flows-runner

A headless remote worker for [xqo-flows](../xqo-flows). It adds the `xqo worker` command, which polls an HQ's
flows worker API and runs jobs with this machine's own agents. There is no dashboard, no office and no open
port: the worker only makes outgoing HTTP calls to the HQ.

```bash
pip install xqo-flows-runner     # brings xqo and xqo-flows
```

## Set up a worker

1. **On the HQ**, make a token and list the worker with its labels (`agents.json`):

   ```bash
   xqo flows token                       # prints a random token
   export XQO_WORKER_GPU_BOX_TOKEN=...   # the HQ reads the worker's token from this variable
   ```

   ```json
   "flows": {"listen": "0.0.0.0:8200",
             "workers": {"allow": {"gpu-box": {"token_env": "XQO_WORKER_GPU_BOX_TOKEN", "labels": ["gpu", "linux"]}}}}
   ```

   Restart `xqo serve`. Its banner shows `Flows worker API and webhooks: http://0.0.0.0:8200/flows/v1/`.

2. **On the worker machine**, give it its own `agents.json` with its agents and a `flows_worker` block:

   ```json
   {"agents": [{"name": "builder", "skills": ["python", "build"]}],
    "flows_worker": {"hq": "http://hq.example.internal:8200", "name": "gpu-box", "token_env": "XQO_WORKER_TOKEN", "jobs": 2}}
   ```

   ```bash
   export XQO_WORKER_TOKEN=...           # the same token as on the HQ
   export ANTHROPIC_API_KEY=...          # the worker's own model access
   xqo --config agents.json worker
   # xqo worker gpu-box: connected to HQ 'Main HQ' at http://hq.example.internal:8200, labels: gpu, linux, ...
   ```

   Flags override the file: `--hq`, `--name`, `--token-env`, `--jobs`, `--once` (run one job, then exit). The
   environment variables `XQO_WORKER_HQ`, `XQO_WORKER_NAME` and `XQO_WORKER_JOBS` work too. Global flags such as
   `--data-dir`, `--mock` and `--departments` go before `worker`, as with any xqo command.

3. **Use it**: add a **Remote** step with label `gpu` to a flow, or run a whole flow on the worker with
   `xqo flows run NAME --labels gpu`. `xqo flows workers` on the HQ shows whether the worker is online.

A complete sample is in [`../xqo-flows/examples/setup/`](../xqo-flows/examples/setup). It has the HQ and worker
configs, a remote flow, `docker-compose.worker.yml` (a worker in Docker, with no published port) and an `.env`
template.

## What the worker does

| Job | Runs as |
|---|---|
| `task`: one Remote step | An ordinary root task on the worker, for the step's agent (empty = the worker's first agent). It is unattended unless the step says otherwise. Its result goes back to the HQ. |
| `flow`: a whole flow | The same flow engine as the HQ, using the worker's agents. Step states go back with every heartbeat, so the HQ's editor shows the run live. Approval steps are asked on the HQ and the answer comes back with a heartbeat. A resumed run brings the finished steps' results, so they do not run again. |

- **Heartbeats** go out every `lease_seconds / 3`, and every second while an approval waits. If the HQ answers "cancel" (the run was cancelled there), the
  worker cancels the task and reports `cancelled`.
- **HQ unreachable:** the worker keeps retrying with a growing pause, up to 30 s. If the outage outlasts the lease,
  the HQ gives the job to another worker. A rejected token (401/403) stops the worker.
- **Data:** each worker has its own data dir (tasks, workspaces, logs), so its tasks can be inspected with the
  usual tools. To see them in a dashboard, run `xqo serve` on the same data dir while the worker is stopped (one
  process per data dir).
