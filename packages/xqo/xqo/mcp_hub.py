"""MCP support.

Two kinds of servers, configured under "mcp_servers" in agents.json or a department file, granted with
"mcp" / "default_mcp" (grants.py):
- remote:  {"name": "docs", "url": "https://.../mcp", "authorization_token": "..."}
           passed to the Messages API MCP connector; Anthropic connects server-side.
- local:   {"name": "fs", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}
           launched by the xqo over stdio; its tools are exposed to the agent as
           client tools named mcp__<server>__<tool>.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack

PREFIX = "mcp__"
NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _attr(obj, *names):
    """Read a field that is camelCase in mcp 1.x and may be snake_case in 2.x."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


def is_remote(cfg: dict) -> bool:
    return "url" in cfg


def remote_params(servers: list[dict], filters: dict[str, list[str] | None] | None = None,
                  denied: dict[str, set[str]] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (mcp_servers, mcp_toolset tools) for the Messages API MCP connector.

    `filters`: per-agent tool names per server (grants.py; None = the server's own allowlist);
    `denied`: tool names the agent's tool policy denies, per server."""
    defs, toolsets = [], []
    for s in servers:
        if not is_remote(s):
            continue
        d = {"type": "url", "url": s["url"], "name": s["name"]}
        token = s.get("authorization_token") or (os.getenv(s["token_env"]) if s.get("token_env") else None)
        if token:
            d["authorization_token"] = token
        defs.append(d)
        ts = {"type": "mcp_toolset", "mcp_server_name": s["name"]}
        allowed = remote_allowed(s, (filters or {}).get(s["name"]))
        off = sorted((denied or {}).get(s["name"], set()))
        if allowed is not None:  # allowlist mode
            ts["default_config"] = {"enabled": False}
            ts["configs"] = {t: {"enabled": True} for t in allowed if t not in off}
        elif off:
            ts["configs"] = {t: {"enabled": False} for t in off}
        toolsets.append(ts)
    return defs, toolsets


def remote_allowed(server: dict, agent_tools: list[str] | None) -> list[str] | None:
    """Tool names a remote server may offer one agent: the server's allowed_tools and the agent's own filter
    (exact names: a remote server's tools are not listed in advance). None = all of them."""
    own = server.get("allowed_tools") or None
    if agent_tools is None:
        return list(own) if own else None
    return [t for t in agent_tools if own is None or t in own]


def api_name(server: str, tool: str) -> str:
    return f"{PREFIX}{NAME_RE.sub('_', server)}__{NAME_RE.sub('_', tool)}"[:64]


class _Conn:
    """One local server's session, owned by its own task (anyio contexts must exit where they entered)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None


class MCPHub:
    """Owns the local stdio MCP sessions. Servers can be added, changed and removed while running (sync)."""

    def __init__(self, servers: list[dict]):
        self.servers = [s for s in servers if not is_remote(s)]
        self.sessions: dict[str, object] = {}
        self.tools: dict[str, tuple[str, str, dict]] = {}  # api name -> (server, tool, api tool def)
        self.errors: dict[str, str] = {}
        self._conns: dict[str, _Conn] = {}
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        await asyncio.gather(*(self._open(s) for s in self.servers))

    async def _open(self, s: dict) -> None:
        conn = _Conn(s)
        self._conns[s["name"]] = conn
        conn.task = asyncio.create_task(self._serve(conn))
        await conn.ready.wait()

    async def _serve(self, conn: _Conn) -> None:
        s = conn.cfg
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError:
            self.errors[s["name"]] = "the 'mcp' package is not installed (it comes with claude-agent-sdk: reinstall xqo-lite)"
            conn.ready.set()
            return
        try:
            async with AsyncExitStack() as stack:
                params = StdioServerParameters(command=s["command"], args=s.get("args", []),
                                               env={**os.environ, **s.get("env", {})} if s.get("env") else None,
                                               cwd=s.get("cwd"))
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), s.get("startup_timeout", 30))
                listed = await session.list_tools()
                allowed = set(s.get("allowed_tools") or [])
                for t in listed.tools:
                    if allowed and t.name not in allowed:
                        continue
                    api = api_name(s["name"], t.name)
                    self.tools[api] = (s["name"], t.name, {
                        "name": api,
                        "description": f"[MCP {s['name']}] {t.description or t.name}"[:1024],
                        "input_schema": _attr(t, "inputSchema", "input_schema")
                                        or {"type": "object", "properties": {}},
                    })
                conn.session = self.sessions[s["name"]] = session
                self.errors.pop(s["name"], None)
                conn.ready.set()
                await conn.stop.wait()
        except Exception as e:  # one bad server must not stop the others
            self.errors[s["name"]] = f"{type(e).__name__}: {e}"
        finally:
            self._forget(s["name"])
            conn.ready.set()

    def _forget(self, name: str) -> None:
        self.sessions.pop(name, None)
        for api in [a for a, (srv, _, _) in self.tools.items() if srv == name]:
            del self.tools[api]

    async def _close(self, name: str) -> None:
        conn = self._conns.pop(name, None)
        if not conn or not conn.task:
            return
        conn.stop.set()
        try:
            await asyncio.wait_for(conn.task, 10)
        except Exception:
            conn.task.cancel()
        self._forget(name)

    async def stop(self) -> None:
        await asyncio.gather(*(self._close(n) for n in list(self._conns)))
        self.started = False

    async def sync(self, servers: list[dict]) -> dict:
        """Make the running set match `servers` (local ones): start new, restart changed, stop removed."""
        want = {s["name"]: s for s in servers if not is_remote(s)}
        have = {s["name"]: s for s in self.servers}
        added = [n for n in want if n not in have]
        removed = [n for n in have if n not in want]
        changed = [n for n in want if n in have and want[n] != have[n]]
        self.servers = list(want.values())
        for n in removed:
            self.errors.pop(n, None)
        if self.started:
            await asyncio.gather(*(self._close(n) for n in removed + changed))
            await asyncio.gather(*(self._open(want[n]) for n in added + changed))
        return {"added": added, "removed": removed, "restarted": changed}

    def tool_defs(self, servers: list[str] | dict[str, list[str] | None] | None = None) -> list[dict]:
        """Tool definitions of the given servers; a dict also filters each server's tools (fnmatch)."""
        from fnmatch import fnmatchcase
        out = []
        for api, (srv, tool, d) in self.tools.items():
            if servers is None:
                out.append(d)
            elif srv in servers:
                pats = servers.get(srv) if isinstance(servers, dict) else None
                if pats is None or any(fnmatchcase(tool, p) for p in pats):
                    out.append(d)
        return out

    async def call(self, api_name: str, args: dict, timeout: float = 120) -> tuple[str, bool]:
        """Return (text, is_error)."""
        server, tool, _ = self.tools[api_name]
        res = await asyncio.wait_for(self.sessions[server].call_tool(tool, args), timeout)
        parts = []
        for c in getattr(res, "content", None) or []:
            if getattr(c, "type", "") == "text":
                parts.append(c.text)
            else:
                parts.append(json.dumps(c.model_dump(exclude_none=True))[:4000])
        structured = _attr(res, "structuredContent", "structured_content")
        if structured and not parts:
            parts.append(json.dumps(structured))
        return "\n".join(parts) or "(no content)", bool(_attr(res, "isError", "is_error"))

    def status(self) -> list[dict]:
        out = []
        for s in self.servers:
            n = s["name"]
            out.append({"name": n, "kind": "local", "connected": n in self.sessions, "error": self.errors.get(n),
                        "tools": [t for api, (srv, t, _) in self.tools.items() if srv == n]})
        return out
