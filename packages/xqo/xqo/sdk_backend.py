"""Claude Agent SDK backend ("provider": "claude-sdk"): the agent loop runs in Claude Code.

Optional: pip install ".[sdk]". When the package is installed it is the default provider for every agent;
"anthropic" and OpenAI-compatible providers stay selectable per agent (and globally in agents.json).

How a task runs (SdkRunner):
- The xqo's tools (handover_task, ask_agent, ask_user, reply_user, the notes board, local MCP tools,
  and run_code / the workspace file tools when Claude Code's own tools don't replace them) are served to
  Claude Code by an in-process MCP server; each call runs Agent._tool, the same code as on the API path.
- Claude Code's own tools ("sdk_tools" per agent, default Read, Grep, Glob, Edit, Write, Bash) work in the task
  workspace. PreToolUse hooks keep reads and writes inside it, apply write_paths and wait while the task is
  paused; Bash never runs on the host: the hook runs the command in the xqo sandbox (it needs
  can_run_code) and hands the output back. PostToolUse hooks record every Edit/Write as a workspace version
  (artifact event, history, git commit) and deliver chat messages (Ask / Steer) between tool calls.
- Attachments are not sent as content: the task prompt and chat messages list their paths, and the PreToolUse
  hook lets the read tools open the task's attachments folder (read-only). Delegated 'files' are paths too.
- Each session's usage and cost from Claude Code's result messages is recorded on the task (Bus.record_usage).
- Fenced code blocks in replies are still saved as artifacts; failed patches and refused writes are sent back
  in a follow-up message, as on the API path. Claude Code compacts its own history, so compaction.py and the
  max_tokens continue/ask logic do not apply; max_tokens is passed as Claude Code's output limit.
- The "no limits" override (limits.py) is read when a session starts (no max_turns, the larger output
  limit). Turned on mid-session, a session that hits max_turns is told to continue; when it runs out, a
  session started without max_turns is stopped once it has used the agent's max_turns.

Sign-in: ANTHROPIC_API_KEY (or the provider's "api_key_env"), else the `claude` login on this machine
(Claude Code's /login, or CLAUDE_CODE_OAUTH_TOKEN), else Bedrock / Vertex settings for Claude Code.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING

from .chat import RunStopped, add_user_text
from .fences import normalize_path
from .models import Event, new_id
from .patching import PatchError

if TYPE_CHECKING:
    from .agent import Agent
    from .models import Task

SDK_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]  # Claude Code tools an agent gets by default
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SERVER = "xqo"  # in-process MCP server name: its tools are mcp__xqo__<name>
TOOL_TIMEOUT_MS = 24 * 3600 * 1000  # handover_task / ask_agent / ask_user wait for other agents or the user
HOOK_TIMEOUT = 24 * 3600  # a hook may wait out a pause or run a long sandbox command
MAX_FOLLOWUPS = 3  # follow-up messages about failed patches / refused fenced writes per task
AUTH_ERRORS = {"authentication_failed", "billing_error"}
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

NO_LOGIN = ("The Claude Agent SDK could not sign in. Set ANTHROPIC_API_KEY in the environment that starts the "
            "xqo, or a token from `claude setup-token` as CLAUDE_CODE_OAUTH_TOKEN (in Docker: in .env, read before every "
            "session, no restart; natively also <data dir>/secrets/claude-oauth-token) (a wrong or expired token also ends here), or sign in to Claude Code on this machine (run `claude` and use /login), then restart. "
            "To use the Anthropic API instead, set \"default_provider\": \"anthropic\" in agents.json.")
SANDBOX_NOTE = ("[xqo] Ran in the xqo sandbox on a fresh copy of the workspace, not on this "
                "machine; file changes made by the command were discarded (change files with Edit / Write). "
                "Result:\n")

CONTINUE_TURNS = ("[xqo] The turn limit was lifted (resource limits are off for now). Continue the task "
                  "where you stopped.")


class SdkStartError(RuntimeError):
    """Claude Code could not be started or could not sign in (before the agent did any work)."""


TOKEN_FILE = Path("secrets") / "claude-oauth-token"


def oauth_token_file() -> Path:
    """Where a claude.ai OAuth token can be dropped without a restart: XQO_OAUTH_TOKEN_FILE, else
    <data dir>/secrets/claude-oauth-token (XQO_DATA, else the current folder). Outside artifacts/, so it is never
    served, zipped or backed up."""
    if os.getenv("XQO_OAUTH_TOKEN_FILE"):
        return Path(os.environ["XQO_OAUTH_TOKEN_FILE"]).expanduser()
    return Path(os.getenv("XQO_DATA") or ".") / TOKEN_FILE


def file_oauth_token() -> str | None:
    """The token in oauth_token_file(), read fresh on every call (so a new token applies to the next session).
    The file is either a bare token, or a .env file whose CLAUDE_CODE_OAUTH_TOKEN= line is used (other lines are
    ignored). Tolerates a byte-order mark, quotes, `export`, comments, blank lines and Windows line endings."""
    try:
        text = oauth_token_file().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" in line:
            key, _, value = line.partition("=")
            if key.strip() != "CLAUDE_CODE_OAUTH_TOKEN":
                continue
            line = value.split(" #", 1)[0].strip()
        line = line.strip("\"'").strip()
        if line:
            return line
    return None


def auth_method(spec: dict | None = None) -> str:
    """How Claude Code will sign in, for the dashboard (never the secret itself)."""
    env = (spec or {}).get("api_key_env") or "ANTHROPIC_API_KEY"
    if os.getenv(env):
        return f"api key ({env})"
    if os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "auth token (ANTHROPIC_AUTH_TOKEN)"
    if file_oauth_token():
        return f"oauth token (file {oauth_token_file()})"
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        return "oauth token (CLAUDE_CODE_OAUTH_TOKEN)"
    if os.getenv("CLAUDE_CODE_USE_BEDROCK") or os.getenv("CLAUDE_CODE_USE_VERTEX"):
        return "cloud provider (Bedrock / Vertex)"
    home = Path(os.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if (home / ".credentials.json").is_file():
        return "claude login"
    if sys.platform == "darwin":  # the login lives in the keychain: not checked here
        return "claude login (keychain)"
    return "none found"


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _kind(msg) -> str:
    return type(msg).__name__


def _block_text(b) -> str:
    return (b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or ""


def _btype(b) -> str:
    return (b.get("type") if isinstance(b, dict) else getattr(b, "type", "")) or ""


def transcript(messages: list) -> str:
    """A conversation as one prompt (for single calls such as chat side answers)."""
    if len(messages) == 1 and isinstance(messages[0]["content"], str):
        return messages[0]["content"]
    out = []
    for m in messages:
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        parts = []
        for b in content:
            t = _btype(b)
            if t == "text":
                parts.append(_block_text(b))
            elif t == "tool_use":
                inp = b.get("input") if isinstance(b, dict) else getattr(b, "input", {})
                name = b.get("name") if isinstance(b, dict) else getattr(b, "name", "")
                parts.append(f"[called {name} {json.dumps(inp)[:2000]}]")
            elif t == "tool_result":
                c = b.get("content") if isinstance(b, dict) else getattr(b, "content", "")
                parts.append(f"[tool result] {(c if isinstance(c, str) else json.dumps(c))[:2000]}")
            elif t in ("image", "document"):
                parts.append(f"[{t} attachment]")
        if parts:
            out.append(f"--- {m['role']} ---\n" + "\n".join(parts))
    return "\n\n".join(out)


async def _stream(content):
    """Streaming input for the first message: text, or content blocks (images, PDFs, text documents)."""
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


class ClaudeSdkLLM:
    """Factory for Claude Agent SDK sessions; also answers single calls (titles, chat side answers)."""

    is_sdk = True

    def __init__(self, spec: dict | None = None):
        self.spec = spec or {"type": "claude-sdk"}

    def env(self, max_tokens: int | None = None) -> dict:
        env = {"MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_MS)}
        key = self.spec.get("api_key_env")
        if key and key != "ANTHROPIC_API_KEY":
            if not os.getenv(key):
                raise ValueError(f"{key} is not set: export it before starting the xqo")
            env["ANTHROPIC_API_KEY"] = os.environ[key]
        if max_tokens:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
        if "ANTHROPIC_API_KEY" not in env and (token := file_oauth_token()):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token  # read per session: a renewed token needs no restart
        return env

    def server(self, name: str, tools: list[dict]):
        """In-process MCP server from [{name, description, input_schema, handler}]."""
        from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
        return create_sdk_mcp_server(name, tools=[SdkMcpTool(name=t["name"], description=t["description"],
                                                             input_schema=t["input_schema"], handler=t["handler"])
                                                  for t in tools])

    def options(self, *, system: str, model: str, effort: str | None, cwd, tools: list[str], allowed: list[str],
                servers: dict, hooks: dict, max_turns: int | None, max_tokens: int | None,
                disallowed: list[str] | None = None):
        """hooks: {event: [(matcher or None, callback)]}."""
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        kw: dict = {}
        if self.spec.get("fallback_model"):
            kw["fallback_model"] = self.spec["fallback_model"]
        if disallowed:  # remote MCP tools the agent's tool policy denies (grants.py)
            kw["disallowed_tools"] = disallowed
        return ClaudeAgentOptions(
            system_prompt=system, model=model, effort=effort, cwd=str(cwd) if cwd else None, tools=tools,
            allowed_tools=allowed, mcp_servers=servers, max_turns=max_turns, setting_sources=[],
            permission_mode="dontAsk", env=self.env(max_tokens),
            hooks={ev: [HookMatcher(matcher=m, hooks=[cb], timeout=HOOK_TIMEOUT) for m, cb in lst]
                   for ev, lst in hooks.items()} or None, **kw)

    def client(self, options):
        from claude_agent_sdk import ClaudeSDKClient
        return ClaudeSDKClient(options)

    async def _single(self, *, model: str, system: str, prompt: str, tools: list[dict], effort: str | None,
                      max_tokens: int | None):
        calls: list = []
        defs = []
        for t in tools:
            if "input_schema" not in t:
                continue

            async def handler(args, _name=t["name"]):
                calls.append(NS(type="tool_use", id=new_id("toolu"), name=_name, input=args))
                return {"content": [{"type": "text", "text": "ok"}]}
            defs.append({**t, "description": t.get("description", ""), "handler": handler})
        servers = {SERVER: self.server(SERVER, defs)} if defs else {}
        opts = self.options(system=system, model=model, effort=effort, cwd=None, tools=[],
                            allowed=[f"mcp__{SERVER}"] if defs else [], servers=servers, hooks={},
                            max_turns=2 if defs else 1, max_tokens=max_tokens)
        texts, served = [], model
        usage = NS(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        async with self.client(opts) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if _kind(msg) == "AssistantMessage":
                    if getattr(msg, "error", None) in AUTH_ERRORS:
                        raise RuntimeError(NO_LOGIN)
                    served = getattr(msg, "model", None) or served
                    texts += [b.text for b in msg.content if _kind(b) == "TextBlock" and b.text]
                elif _kind(msg) == "ResultMessage":
                    for k, v in (getattr(msg, "usage", None) or {}).items():
                        if hasattr(usage, k) and isinstance(v, int):
                            setattr(usage, k, v)
                    if getattr(msg, "is_error", False) and not texts and not calls:
                        raise RuntimeError(f"Claude Agent SDK: {getattr(msg, 'result', None) or msg.subtype}")
        content = ([NS(type="text", text="\n".join(texts))] if texts else []) + calls
        return NS(content=content, stop_reason="tool_use" if calls else "end_turn", usage=usage, model=served)

    async def create(self, *, model: str, system: str, messages: list, tools: list, effort: str | None = None,
                     max_tokens: int | None = None, agent: str = "", mcp_servers: list | None = None):
        """One reply to a conversation (no Claude Code tools); tool calls are returned, not run."""
        return await self._single(model=model, system=system, prompt=transcript(messages), tools=tools,
                                  effort=effort, max_tokens=max_tokens)

    async def title(self, prompt: str, model: str = "") -> str:
        r = await self._single(model=os.getenv("XQO_TITLE_MODEL", model), effort="low", max_tokens=None, tools=[],
                               system="Write a one-line title (at most 8 words) summarizing the user's task. "
                                      "Reply with the title only: no quotes, no trailing period.",
                               prompt=prompt[:4000])
        return "".join(b.text for b in r.content if b.type == "text").strip()


def remote_servers(servers: list[dict]) -> dict:
    """agents.json remote MCP servers ({"name", "url", token}) as Claude Code http servers."""
    out = {}
    for s in servers:
        if "url" not in s:
            continue
        cfg: dict = {"type": "http", "url": s["url"]}
        token = s.get("authorization_token") or (os.getenv(s["token_env"]) if s.get("token_env") else None)
        if token:
            cfg["headers"] = {"Authorization": f"Bearer {token}"}
        out[s["name"]] = cfg
    return out


def remote_allowed_tools(servers: dict, remote: list[dict], agent) -> list[str]:
    """Claude Code allowed_tools for the MCP servers: the whole server, or only the tools its allowed_tools and
    the agent's own filter name (mcp__<server>__<tool>)."""
    from .mcp_hub import remote_allowed
    out = []
    by_name = {s["name"]: s for s in remote}
    for n in servers:
        names = remote_allowed(by_name[n], agent.mcp_filters.get(n)) if n in by_name else None
        out += [f"mcp__{n}"] if names is None else [f"mcp__{n}__{t}" for t in names]
    return out


def sdk_guide(builtins: list[str], can_run: bool) -> str:
    lines = []
    if set(builtins) & (READ_TOOLS | WRITE_TOOLS):
        lines.append("Claude Code tools: your working directory is this task's shared workspace. "
                     + ("Read, Grep and Glob read it. " if set(builtins) & READ_TOOLS else "")
                     + ("Edit and Write change its files; every change is saved as a workspace version that the "
                        "other agents and the user see. " if set(builtins) & WRITE_TOOLS else "")
                     + "Never use paths outside the workspace, except that Read may open the task's attached "
                       "files at the paths you are given.")
    if "Bash" in builtins and can_run:
        lines.append("Bash runs each command in the xqo sandbox on a fresh copy of the workspace, not on "
                     "this machine. The command's output comes back as a note on the Bash call (shown as "
                     "denied: that is expected). File changes made by a command are discarded and git is not "
                     "available there: change files with Edit / Write, move or rename them with move_file and delete files or "
                     "folders with delete_path.")
    return ("\n\n" + "\n".join(lines)) if lines else ""


class SdkRunner:
    """One task of one agent, run by Claude Code through the Agent SDK."""

    def __init__(self, agent: Agent, task: Task):
        self.agent, self.task, self.bus = agent, task, agent.bus
        self.ws = agent.artifacts.workspace(task.root_id)
        self.before: dict[str, tuple[str, str | None]] = {}  # tool_use_id -> (file, content before the edit)
        self.patch_errors: list[str] = []
        self.blocked: list[str] = []
        self.held: list = []  # chat content blocks (attachments) kept for the next follow-up message
        self.messages: list = []
        self.seen: set[str] = set()  # message ids already counted in usage
        self.turn = 0
        self.session: dict = {}  # usage and cost from Claude Code's result messages (recorded on the task)
        self.stopped: Exception | None = None  # a budget stop raised in a hook, re-raised after the reply

    # ---- tools ------------------------------------------------------------
    def builtins(self) -> list[str]:
        tools = list(self.agent.sdk_tools)
        if "Bash" in tools and self.agent.code_sandbox() is None:
            tools.remove("Bash")  # no sandbox for this agent: no commands at all (never on the host)
        plugins = getattr(self.bus, "plugins", None)
        if plugins:  # plugin gates (channels: public tasks; feeds: report/act mode) apply to Claude Code tools too
            tools = [t for t in tools if plugins.allows(self.task, self.agent, t)]
        return [t for t in tools if self.agent.tool_policy.allows(t)]  # "tools" / "default_tools" (grants.py)

    def tool_defs(self, builtins: list[str]) -> list[dict]:
        skip = set()  # xqo tools that Claude Code's own tools replace
        if {"Read", "Glob"} <= set(builtins):
            skip |= {"read_file", "list_files"}
        if {"Edit", "Write"} <= set(builtins):
            skip |= {"write_file", "patch_file"}
        if "Bash" in builtins:
            skip.add("run_code")
        if getattr(self.agent, "has_dept_workspace", lambda: False)():
            skip = set()  # hq department members keep them for the department workspace (scope='department')
        defs = []
        for t in self.agent.tools()[0]:
            if "input_schema" not in t or t["name"] in skip:
                continue
            plugins = getattr(self.bus, "plugins", None)
            if plugins and not plugins.allows(self.task, self.agent, t["name"]):
                continue

            async def handler(args, _name=t["name"]):
                res = await self.agent._tool(self.task, NS(id=new_id("toolu"), name=_name, input=args or {}))
                c = res.get("content")
                text = c if isinstance(c, str) else "\n".join(_block_text(b) for b in c or [])
                return {"content": [{"type": "text", "text": text or "(empty)"}], "is_error": bool(res.get("is_error"))}
            defs.append({"name": t["name"], "description": t.get("description", ""),
                         "input_schema": t["input_schema"], "handler": handler})
        return defs

    # ---- hooks --------------------------------------------------------------
    def _rel(self, path: str | None) -> tuple[str | None, str | None]:
        """(workspace-relative path, error)."""
        if not path:
            return None, None
        root = self.ws.resolve()
        p = Path(path)
        p = (p if p.is_absolute() else root / p).resolve()
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            return None, (f"{path} is outside the task workspace ({root}): work only on workspace files "
                          "(use relative paths).")
        if rel.split("/", 1)[0].lower() == ".git":
            return None, f"{path}: the .git folder is managed by the xqo"
        return ("." if rel in ("", ".") else (normalize_path(rel) or rel)), None

    def _attachment(self, path: str | None) -> bool:
        """Whether path is the task's attachments folder or a file in it (read-only for agents)."""
        store = getattr(self.bus, "attachments", None)
        if not path or not store:
            return False
        folder = store.folder(self.task.root_id).resolve()
        p = Path(path)
        p = (p if p.is_absolute() else self.ws.resolve() / p).resolve()
        return p == folder or folder in p.parents

    async def pre(self, inp: dict, tool_use_id: str | None, ctx) -> dict:
        agent, task = self.agent, self.task
        await self.bus.chat.gate(task, agent)  # paused: wait here before the next tool runs
        if getattr(self.bus, "budget", None):
            try:
                await self.bus.budget.check(task, agent.name, agent.department)
            except RuntimeError as e:  # BudgetExceeded: stop the run, then fail the task with it
                self.stopped = e
                return {"continue_": False, "stopReason": str(e), **_deny(str(e))}
        name = inp.get("tool_name", "")
        args = inp.get("tool_input") or {}
        if name.startswith("mcp__"):
            return {}
        if name == "Bash":
            return await self._bash(args)
        path = args.get("file_path") or args.get("notebook_path") or args.get("path")
        if name in READ_TOOLS and self._attachment(path):
            return {}  # the task's attachments: readable, never written
        rel, err = self._rel(path)
        if err:
            return _deny(err)
        if name in WRITE_TOOLS:
            if not rel or rel == ".":
                return _deny("name a file inside the workspace")
            if not agent.may_write(rel):
                return _deny("not written: " + agent._refusal([rel]))
            p = self.ws / rel
            try:
                old = p.read_text(encoding="utf-8") if p.is_file() else None
            except UnicodeDecodeError:
                return _deny(f"{rel} is not a text file")
            self.before[inp.get("tool_use_id") or tool_use_id or ""] = (rel, old)
        return {}

    async def _bash(self, args: dict) -> dict:
        agent, task = self.agent, self.task
        if agent.code_sandbox() is None:
            return _deny(f"{agent.name} may not run commands; hand over to an agent with skill 'testing'")
        timeout = args.get("timeout")
        try:
            out = await agent.run_code(task, args.get("command", ""), timeout / 1000 if timeout else None)
        except RunStopped as e:
            out = str(e)
        except (RuntimeError, OSError, asyncio.TimeoutError) as e:
            out = f"the sandbox run failed: {e}"
        return _deny(SANDBOX_NOTE + out)

    async def post(self, inp: dict, tool_use_id: str | None, ctx) -> dict:
        key = inp.get("tool_use_id") or tool_use_id or ""
        if inp.get("tool_name") in WRITE_TOOLS and key in self.before:
            self._record(*self.before.pop(key), inp.get("tool_name"))
        texts = []
        for item in self.bus.chat.drain(self.task):  # Ask / Steer messages reach the agent between tool calls
            if isinstance(item, str):
                texts.append(item)
            else:
                texts += [_block_text(b) for b in item if _btype(b) == "text"]
                if any(_btype(b) != "text" for b in item):
                    self.held += [b for b in item if _btype(b) != "text"]
                    texts.append("(its attachments follow in the next message)")
        if not texts:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(texts)}}

    def _record(self, rel: str, old: str | None, tool: str) -> None:
        """Save a file Claude Code changed as a workspace version (history, git, artifact event)."""
        agent, task = self.agent, self.task
        p = self.ws / rel
        try:
            new = p.read_text(encoding="utf-8") if p.is_file() else None
        except UnicodeDecodeError:
            return
        if new is None or new == old:
            return
        diff = None
        if old is not None:
            diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))
        action = "create" if old is None else ("update" if tool == "Write" else "patch")
        try:
            rec = agent.artifacts.write_file(task.root_id, rel, new, task.id, agent.name, action=action, diff=diff,
                                             source="claude-code")
        except PatchError as e:
            rec = {"action": "error", "file": rel, "error": str(e)}
        agent._emit_artifact(task, rec)

    # ---- the run ------------------------------------------------------------
    def _assistant(self, msg) -> list[str]:
        agent, task, bus = self.agent, self.task, self.bus
        if getattr(msg, "error", None) in AUTH_ERRORS:
            raise SdkStartError(NO_LOGIN + f" ({msg.error})")
        mid = getattr(msg, "message_id", None) or new_id("msg")
        usage = getattr(msg, "usage", None) or {}
        agent.served_model = getattr(msg, "model", None) or agent.model
        tools = [b.name for b in msg.content if _kind(b) == "ToolUseBlock"]
        stop = getattr(msg, "stop_reason", None) or ("tool_use" if tools else "end_turn")
        if mid not in self.seen:  # Claude Code may send one API reply as several messages
            self.seen.add(mid)
            self.turn += 1
            agent.usage["calls"] += 1
            for k in USAGE_KEYS:
                agent.usage[k] += usage.get(k) or 0
            self.account(msg, usage)
            bus.emit(Event("llm", agent.name, None, task.id, task.parent_id, f"turn {self.turn}: stop={stop}",
                           {"model": agent.served_model, "backend": "claude-sdk", "tools": tools,
                            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens")}))
        if stop == "max_tokens":
            agent.cutoffs["cut"] += 1
        texts = [b.text for b in msg.content if _kind(b) == "TextBlock" and b.text]
        allow = agent.may_write if agent.write_paths is not None else None
        for t in texts:
            recs, errs = agent.artifacts.process_text(t, task.id, agent.name, task.root_id, allow=allow)
            self.patch_errors += errs
            for rec in recs:
                self.blocked += rec.get("blocked", [])
                agent._emit_artifact(task, rec)
        if texts:
            blocks = [{"type": "text", "text": t} for t in texts]
            if self.messages and self.messages[-1]["role"] == "assistant":
                self.messages[-1] = {"role": "assistant", "content": list(self.messages[-1]["content"]) + blocks}
            else:
                self.messages.append({"role": "assistant", "content": blocks})
        return texts

    def _result(self, msg) -> None:
        """Add a result message's usage (per query) and take its cost (Claude Code totals it per session)."""
        s = self.session or {"cost_usd": 0.0, **{k: 0 for k in USAGE_KEYS}, "turns": 0, "duration_ms": 0,
                             "duration_api_ms": 0, "queries": 0}
        for k in USAGE_KEYS:
            s[k] += (getattr(msg, "usage", None) or {}).get(k) or 0
        if getattr(msg, "total_cost_usd", None) is not None:
            s["cost_usd"] = msg.total_cost_usd
        s["turns"] += getattr(msg, "num_turns", 0) or 0
        s["duration_ms"] += getattr(msg, "duration_ms", 0) or 0
        s["duration_api_ms"] += getattr(msg, "duration_api_ms", 0) or 0
        s["queries"] += 1
        if getattr(msg, "model_usage", None):
            s["models"] = {m: {"cost_usd": u.get("costUSD"), "input_tokens": u.get("inputTokens"),
                               "output_tokens": u.get("outputTokens"),
                               "cache_read_input_tokens": u.get("cacheReadInputTokens"),
                               "cache_creation_input_tokens": u.get("cacheCreationInputTokens")}
                           for m, u in msg.model_usage.items()}
        self.session = s

    def account(self, msg, usage: dict) -> None:
        """Department budgets: count each API reply's tokens and cost."""
        if getattr(self.bus, "budget", None) and usage:
            self.bus.budget.add(self.task.root_id, self.agent.department, self.agent.served_model, usage)

    def _followup(self, rounds: int) -> list | None:
        """Content for a follow-up message, or None when the task is finished."""
        parts: list = []
        if self.blocked and rounds < MAX_FOLLOWUPS:
            parts.append("Fenced blocks NOT written: " + self.agent._refusal(list(dict.fromkeys(self.blocked))))
        if self.patch_errors and rounds < MAX_FOLLOWUPS:
            parts.append("Some artifact patches failed and were NOT applied:\n- " + "\n- ".join(self.patch_errors)
                         + "\nRead the current file, then send a corrected patch or edit it with Edit.")
        self.blocked, self.patch_errors = [], []
        content: list = [{"type": "text", "text": p} for p in parts]
        for item in self.bus.chat.drain(self.task):
            content += [{"type": "text", "text": item}] if isinstance(item, str) else item
        content = self.held + content
        self.held = []
        return content or None

    async def run(self, messages: list) -> str:
        agent, task = self.agent, self.task
        self.messages = messages
        self.ws.mkdir(parents=True, exist_ok=True)
        builtins = self.builtins()
        defs = self.tool_defs(builtins)
        servers = {SERVER: agent.llm.server(SERVER, defs)}
        remote = agent.remote_servers()
        plugins = getattr(self.bus, "plugins", None)
        if plugins:  # plugin gates also pick the remote MCP servers ("mcp:<name>") of a task
            remote = [s for s in remote if plugins.allows(task, agent, f"mcp:{s['name']}")]
        remote = [s for s in remote if s["name"] != SERVER]  # a remote server can never stand in for our own tools
        servers.update(remote_servers(remote))
        guide = sdk_guide(builtins, agent.code_sandbox() is not None)
        base_turns = agent.task_limits(task).get("max_turns") or agent.max_turns  # a scheduled run's own limit
        unlimited = agent.turn_limit(task) != base_turns  # the "no limits" override is on (limits.py)
        options = agent.llm.options(
            system=agent._system(sdk=bool(set(builtins) & WRITE_TOOLS)) + guide, model=agent.model,
            effort=agent.effort, cwd=self.ws, tools=builtins, allowed=builtins + remote_allowed_tools(servers, remote, agent),
            servers=servers, hooks={"PreToolUse": [(None, self.pre)], "PostToolUse": [(None, self.post)]},
            max_turns=None if unlimited else base_turns, max_tokens=agent.output_limit(task),
            **({"disallowed": d} if (d := [f"mcp__{n}__{t}" for n, ts in agent.remote_denied().items()
                                           for t in sorted(ts)]) else {}))
        last_text, rounds, started = "", 0, False
        if getattr(self.bus, "budget", None):
            await self.bus.budget.check(task, agent.name, agent.department)
        try:
            async with agent.llm.client(options) as client:
                await client.query(_stream(messages[0]["content"]))
                while True:
                    result, capped = None, False
                    async for msg in client.receive_response():
                        started = True
                        if _kind(msg) == "AssistantMessage":
                            texts = self._assistant(msg)
                            if texts:
                                last_text = "\n".join(texts)
                            if unlimited and self.turn >= agent.turn_limit(task):  # the override ran out
                                capped = True
                                if hasattr(client, "interrupt"):
                                    await client.interrupt()
                                break
                        elif _kind(msg) == "ResultMessage":
                            result = msg
                            self._result(msg)
                        elif _kind(msg) == "RateLimitEvent" and hasattr(self.bus, "record_rate_limit"):
                            self.bus.record_rate_limit(agent.name, task.id, getattr(msg, "rate_limit_info", None))
                    if self.stopped:
                        raise self.stopped
                    if capped:
                        return last_text + "\n\n[stopped: max turns reached]"
                    if result is not None and getattr(result, "subtype", "") == "error_max_turns":
                        if agent.turn_limit(task) == base_turns:
                            return last_text + "\n\n[stopped: max turns reached]"
                        add_user_text(self.messages, CONTINUE_TURNS)  # turned on while this session ran
                        await client.query(_stream(CONTINUE_TURNS))
                        continue
                    if result is not None and getattr(result, "is_error", False):
                        detail = getattr(result, "result", None) or "; ".join(getattr(result, "errors", None) or []) \
                            or result.subtype
                        raise RuntimeError(f"Claude Agent SDK: {detail}")
                    if result is not None and getattr(result, "result", None) and not last_text:
                        last_text = result.result
                    follow = self._followup(rounds)
                    if follow is None:
                        return last_text
                    rounds += 1
                    add_user_text(self.messages, follow)
                    await client.query(_stream(follow))
        except SdkStartError:
            raise
        except Exception as e:
            if not started and _kind(e) in ("CLINotFoundError", "CLIConnectionError", "ProcessError",
                                            "CLIJSONDecodeError"):
                raise SdkStartError(f"Claude Code could not start: {e}") from None
            raise
        finally:
            if self.session and hasattr(self.bus, "record_usage"):
                self.bus.record_usage(task, agent.name, self.session)
