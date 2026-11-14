# xqo-peering

Messages and files between separate [xqo](https://github.com/dudikkocak/xqo) HQs.

```bash
pip install xqo-peering
xqo peers                 # status; also: send, token
```

Run several isolated HQs (one process or container each, with its own data dir, port, departments, budgets and keys), and optionally let them exchange **messages, workspace files (text or binary) and attachments**, only with the peers each one lists. On one machine they share an exchange folder; across machines they use a separate peer listener (`"listen"`) with one bearer token per pair. An incoming message starts a task with the receiver's own agents, and with `want_reply` its final answer comes back into the sender's task. HQ agents and department heads get `list_peers` / `send_to_peer`. With the Claude Agent SDK backend they reach Claude Code through the in-process `xqo` MCP server like the other xqo tools. The dashboard's **⇆ peers** button and `xqo peers` show the links and the recent messages.

With peering on, the office takes this HQ's name from `peering.name`, and the planners world lists the other HQs
from `"office": {"hqs": [...]}`. Its state (outbox and message log) is in `artifacts/.peers/`, which
`xqo backup --scope state` keeps. Setup on one machine and across machines:
[SETUP.md](https://github.com/dudikkocak/xqo/blob/main/packages/xqo/docs/SETUP.md).
