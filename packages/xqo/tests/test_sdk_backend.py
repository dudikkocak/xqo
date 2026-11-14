"""Claude Agent SDK backend: default selection, the runner with Claude Code's tools and hooks, fallback.

A fake SDK client plays Claude Code: it streams scripted replies, runs the PreToolUse / PostToolUse hooks and
the in-process xqo tools the way Claude Code would, and does Read / Write / Edit on disk. No login,
network or CLI needed."""
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from xqo import providers
from xqo import sdk_backend
from xqo.attachments import validate
from xqo.config import build
from xqo.providers import default_provider, resolve_provider
from xqo.sdk_backend import SANDBOX_NOTE, ClaudeSdkLLM

ROOT = Path(__file__).resolve().parent.parent


# ---- a fake Claude Code -------------------------------------------------------------------------
@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-test"
    usage: dict = field(default_factory=lambda: {"input_tokens": 100, "output_tokens": 20})
    message_id: str | None = None
    stop_reason: str | None = None
    error: str | None = None


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    result: str | None = None
    usage: dict | None = None
    errors: list | None = None
    total_cost_usd: float | None = None
    num_turns: int = 0
    duration_ms: int = 0


@dataclass
class RateLimitInfo:
    status: str
    resets_at: int | None = None
    rate_limit_type: str | None = None
    utilization: float | None = None
    overage_status: str | None = None


@dataclass
class RateLimitEvent:
    rate_limit_info: RateLimitInfo
    uuid: str = "u1"
    session_id: str = "s1"


class CLINotFoundError(Exception):
    pass


class FakeClient:
    def __init__(self, fake, options):
        self.fake, self.o = fake, options
        self.inbox: list = []
        self.n = 0

    async def __aenter__(self):
        if self.fake.start_error:
            raise self.fake.start_error
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        if isinstance(prompt, str):
            self.inbox.append(prompt)
            return
        async for m in prompt:
            self.inbox.append(m["message"]["content"])

    async def _hooks(self, event, inp):
        out = []
        for _, cb in self.o.hooks.get(event, []):
            out.append(await cb(inp, inp["tool_use_id"], {"signal": None}))
        return out

    async def _run_tool(self, name, args, tid):
        """What Claude Code does with one tool call: PreToolUse, the tool, PostToolUse."""
        log = {"tool": name, "input": args}
        self.fake.calls.append(log)
        inp = {"tool_name": name, "tool_input": args, "tool_use_id": tid}
        for r in await self._hooks("PreToolUse", inp):
            spec = r.get("hookSpecificOutput") or {}
            if spec.get("permissionDecision") == "deny":
                log["denied"] = spec["permissionDecisionReason"]
                return
        if name.startswith("mcp__"):
            _, server, tool = name.split("__", 2)
            res = await self.o.mcp_servers[server]["tools"][tool]["handler"](args)
            log["result"] = res["content"][0]["text"]
            log["is_error"] = res.get("is_error", False)
        else:
            assert name in self.o.tools, f"{name} is not enabled"
            p = Path(self.o.cwd) / args["file_path"] if not Path(args["file_path"]).is_absolute() \
                else Path(args["file_path"])
            if name == "Write":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(args["content"])
            elif name == "Edit":
                p.write_text(p.read_text().replace(args["old_string"], args["new_string"], 1))
            elif name == "Read":
                log["result"] = p.read_text()
        ctx = [r["hookSpecificOutput"]["additionalContext"] for r in await self._hooks("PostToolUse", inp) if r]
        if ctx:
            log["context"] = "\n".join(ctx)

    async def receive_response(self):
        if "one-line title" in (self.o.system_prompt or ""):
            yield AssistantMessage([TextBlock("Scripted title")])
            yield ResultMessage(result="Scripted title")
            return
        if self.fake.single is not None:  # a single call: the scripted tool calls, then the result
            for name, args in self.fake.single:
                tid = f"toolu_{len(self.fake.calls)}"
                yield AssistantMessage([ToolUseBlock(tid, f"mcp__xqo__{name}", args)])
                await self._run_tool(f"mcp__xqo__{name}", args, tid)
            yield ResultMessage()
            return
        self.fake.prompts.append(self.inbox.pop(0) if self.inbox else None)
        rounds = self.fake.rounds
        steps = rounds.pop(0) if rounds else [("text", "Nothing more to do.")]
        last = ""
        for step in steps:
            self.n += 1
            if step[0] == "text":
                last = step[1]
                yield AssistantMessage([TextBlock(step[1])], message_id=f"m{self.n}")
            elif step[0] == "error":
                yield AssistantMessage([TextBlock("Invalid API key · Please run /login")], error=step[1])
                yield ResultMessage(subtype="success", is_error=True, result="Invalid API key")
                return
            elif step[0] == "do":
                step[1]()
            elif step[0] == "msg":  # any other SDK message, as is (e.g. a RateLimitEvent)
                yield step[1]
            else:
                _, name, args = step
                tid = f"toolu_{self.n}"
                yield AssistantMessage([ToolUseBlock(tid, name, args)], message_id=f"m{self.n}")
                await self._run_tool(name, args, tid)
        self.fake.cost += 0.25  # Claude Code reports the session's running total
        yield ResultMessage(result=last, usage={"input_tokens": 1, "output_tokens": 1}, total_cost_usd=self.fake.cost,
                            num_turns=len(steps), duration_ms=1000)


class FakeSdk:
    """Replaces ClaudeSdkLLM's SDK calls (server, options, client); records what the runner asked for."""

    def __init__(self):
        self.rounds: list = []
        self.calls: list = []
        self.prompts: list = []
        self.options: list = []
        self.single = None
        self.start_error = None
        self.cost = 0.0

    @property
    def runs(self):
        """Options of agent sessions (titles and single calls have no working directory)."""
        return [o for o in self.options if o.cwd]

    def install(self, monkeypatch):
        fake = self
        monkeypatch.setattr(ClaudeSdkLLM, "server", lambda self, name, tools: {
            "name": name, "tools": {t["name"]: t for t in tools}})

        def options(self, **kw):
            o = NS(system_prompt=kw["system"], model=kw["model"], cwd=kw["cwd"], tools=kw["tools"],
                   allowed_tools=kw["allowed"], mcp_servers=kw["servers"], hooks=kw["hooks"],
                   max_turns=kw["max_turns"], env=self.env(kw["max_tokens"]), effort=kw["effort"],
                   disallowed_tools=kw.get("disallowed"))
            fake.options.append(o)
            return o
        monkeypatch.setattr(ClaudeSdkLLM, "options", options)
        monkeypatch.setattr(ClaudeSdkLLM, "client", lambda self, o: FakeClient(fake, o))
        return self


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.delenv("XQO_PROVIDER", raising=False)
    monkeypatch.setattr(providers, "sdk_installed", lambda: True)
    return FakeSdk().install(monkeypatch)


def make_bus(tmp_path, agents, departments="none", **cfg_extra):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg.pop("departments", None)
    cfg.pop("default_provider", None)
    cfg["agents"] = agents
    cfg["sandbox"] = {"backend": "local", "venv": False}
    cfg.update(cfg_extra)
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    return build(p, tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=False, departments=departments)


# ---- selection ----------------------------------------------------------------------------------
def test_default_provider_is_the_sdk_when_installed(monkeypatch):
    monkeypatch.delenv("XQO_PROVIDER", raising=False)
    monkeypatch.setattr(providers, "sdk_installed", lambda: False)
    assert default_provider() == "anthropic"
    assert resolve_provider({"name": "a"}, {})[::2] == ("anthropic", "default")
    with pytest.raises(ValueError, match=r"pip install"):  # asked for by name but not installed
        resolve_provider({"name": "a", "provider": "claude-sdk"}, {})

    monkeypatch.setattr(providers, "sdk_installed", lambda: True)
    name, spec, src = resolve_provider({"name": "a"}, {})
    assert (name, spec, src) == ("claude-sdk", {"type": "claude-sdk", "fallback": "anthropic"}, "default")
    name, spec, src = resolve_provider({"name": "a", "provider": "claude-sdk"}, {})
    assert "fallback" not in spec and src == "agent"  # chosen explicitly: never falls back silently
    assert resolve_provider({"name": "a"}, {"default_provider": "anthropic"})[0] == "anthropic"
    assert resolve_provider({"name": "a", "provider": "anthropic"}, {})[0] == "anthropic"
    monkeypatch.setenv("XQO_PROVIDER", "anthropic")
    assert resolve_provider({"name": "a"}, {})[0] == "anthropic"


def test_auth_method_never_shows_the_secret(monkeypatch, tmp_path):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(sdk_backend.sys, "platform", "linux")
    assert sdk_backend.auth_method() == "none found"
    (tmp_path / ".credentials.json").write_text("{}")
    assert sdk_backend.auth_method() == "claude login"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    assert sdk_backend.auth_method() == "api key (ANTHROPIC_API_KEY)"
    assert "sk-secret" not in json.dumps(providers.describe({"type": "claude-sdk"}))


# ---- the runner ---------------------------------------------------------------------------------
def test_task_runs_in_claude_code_with_hooks(fake, tmp_path):
    """Write / Edit are recorded as workspace versions, Bash runs in the sandbox, paths stay in the workspace,
    xqo tools run in-process, fenced blocks still become files."""
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"], "system": "You write code.",
                               "can_run_code": True, "max_tokens": 4096}])
    solo = bus.agents["solo"]
    assert (solo.provider, solo.provider_type, solo.provider_source) == ("claude-sdk", "claude-sdk", "default")
    fake.rounds = [[
        ("tool", "Write", {"file_path": "src/app.py", "content": "def f():\n    return 1\n"}),
        ("tool", "Edit", {"file_path": "src/app.py", "old_string": "return 1", "new_string": "return 2"}),
        ("tool", "Bash", {"command": "python -c \"print('hello from the sandbox')\""}),
        ("tool", "mcp__xqo__note", {"key": "approach", "value": "tiny"}),
        ("tool", "Read", {"file_path": "/etc/hostname"}),
        ("tool", "Write", {"file_path": "../escape.txt", "content": "x"}),
        ("text", "Done.\n\n```python src/extra.py\nX = 1\n```\n"),
    ]]
    t = asyncio.run(bus.submit("Write a module", agent="solo"))
    assert t.status == "done", t.result
    assert t.result.startswith("Done.")
    ws = bus.artifacts.workspace(t.id)
    assert (ws / "src" / "app.py").read_text() == "def f():\n    return 2\n"
    assert (ws / "src" / "extra.py").read_text() == "X = 1\n"
    assert not (tmp_path / "art" / "escape.txt").exists()

    bash = next(c for c in fake.calls if c["tool"] == "Bash")
    assert bash["denied"].startswith(SANDBOX_NOTE) and "hello from the sandbox" in bash["denied"]
    assert "backend=local exit=0" in bash["denied"]
    assert "outside the task workspace" in next(c for c in fake.calls if c["tool"] == "Read")["denied"]
    assert "outside the task workspace" in [c for c in fake.calls if c["tool"] == "Write"][1]["denied"]
    assert next(c for c in fake.calls if c["tool"].endswith("note"))["result"] == "set note 'approach'"
    assert bus.memory.render(t.id) and "tiny" in bus.memory.render(t.id)

    recs = [json.loads(x) for x in (tmp_path / "art" / "manifest.jsonl").read_text().splitlines()]
    app = [r for r in recs if r.get("file") == "src/app.py"]
    assert [(r["action"], r["source"]) for r in app] == [("create", "claude-code"), ("patch", "claude-code")]
    assert "-    return 1" in (tmp_path / "art" / app[1]["patch_path"]).read_text()
    assert any(r.get("action") == "run" for r in recs)  # the sandbox log is kept like run_code's

    o = fake.runs[0]
    assert Path(o.cwd) == ws and o.tools == ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
    assert "mcp__xqo" in o.allowed_tools and set(o.tools) <= set(o.allowed_tools)
    tools = set(o.mcp_servers["xqo"]["tools"])
    assert {"handover_task", "ask_agent", "ask_user", "reply_user", "note", "read_notes"} <= tools
    assert not tools & {"write_file", "patch_file", "read_file", "list_files", "run_code"}  # Claude Code's own
    assert "Write and Edit tools" in o.system_prompt and "xqo sandbox" in o.system_prompt
    assert o.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096" and o.max_turns == 12
    assert solo.usage["calls"] == 7 and solo.usage["input_tokens"] == 700
    assert solo.served_model == "claude-test" and t.name.endswith("Scripted title")
    assert solo.describe()["sdk_tools"] == o.tools


def test_write_paths_and_no_sandbox(fake, tmp_path):
    """A planner may write only its plan: Write elsewhere and fenced code are refused (and told so);
    without can_run_code there is no Bash at all, so nothing can run on the host."""
    bus = make_bus(tmp_path, [{"name": "planner", "skills": ["planning"], "write_paths": ["docs/*"]}])
    fake.rounds = [
        [("tool", "Write", {"file_path": "src/x.py", "content": "x"}),
         ("tool", "Write", {"file_path": "docs/plan.md", "content": "# Plan\n"}),
         ("text", "Also:\n\n```python src/y.py\nY = 1\n```\n")],
        [("text", "Understood, plan only.")],
    ]
    t = asyncio.run(bus.submit("Plan it", agent="planner"))
    assert t.status == "done" and t.result == "Understood, plan only."
    ws = bus.artifacts.workspace(t.id)
    assert (ws / "docs" / "plan.md").read_text() == "# Plan\n"
    assert not (ws / "src").exists()
    assert "you may only write docs/*" in fake.calls[0]["denied"]
    assert "Bash" not in fake.runs[0].tools
    assert "run_code" not in fake.runs[0].mcp_servers["xqo"]["tools"]
    follow = fake.prompts[1]
    assert "Fenced blocks NOT written" in json.dumps(follow) and "src/y.py" in json.dumps(follow)


def test_sdk_tools_can_be_turned_off(fake, tmp_path):
    """sdk_tools: [] keeps xqo's own file tools (and run_code) instead."""
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"], "can_run_code": True, "sdk_tools": []}])
    fake.rounds = [[("tool", "mcp__xqo__write_file", {"path": "a.txt", "content": "A"}),
                    ("text", "ok")]]
    t = asyncio.run(bus.submit("x", agent="solo"))
    assert t.status == "done"
    assert fake.runs[0].tools == []
    assert {"write_file", "patch_file", "run_code"} <= set(fake.runs[0].mcp_servers["xqo"]["tools"])
    assert (bus.artifacts.workspace(t.id) / "a.txt").read_text() == "A"
    with pytest.raises(ValueError, match="sdk_tools"):
        make_bus(tmp_path / "bad", [{"name": "x", "skills": ["a"], "sdk_tools": "Bash"}])


def test_chat_messages_reach_the_agent_between_tool_calls(fake, tmp_path):
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"]}])

    def steer():
        task = next(iter(bus.tasks.values()))
        bus.chat._queue(task.id, {"kind": "steer", "id": "msg_1", "text": "Use tabs, not spaces."})
    fake.rounds = [[("do", steer), ("tool", "Write", {"file_path": "a.py", "content": "A = 1\n"}),
                    ("text", "ok")]]
    t = asyncio.run(bus.submit("x", agent="solo"))
    assert t.status == "done"
    assert "Use tabs, not spaces." in fake.calls[0]["context"]


def test_handover_runs_through_the_in_process_tool(fake, tmp_path):
    bus = make_bus(tmp_path, [{"name": "lead", "skills": ["planning"]}, {"name": "coder", "skills": ["python"]}])
    fake.rounds = [  # the coder's session runs inside the lead's handover call, so it takes the next round
        [("tool", "mcp__xqo__handover_task", {"skill": "python", "task": "Write b.py", "title": "b"}),
         ("text", "All done.")],
        [("tool", "Write", {"file_path": "b.py", "content": "B = 2\n"}), ("text", "b.py written")],
    ]
    t = asyncio.run(bus.submit("Build b", agent="lead"))
    assert t.status == "done", t.result
    hand = next(c for c in fake.calls if c["tool"].endswith("handover_task"))
    assert "b.py written" in hand["result"] and not hand["is_error"]
    assert (bus.artifacts.workspace(t.id) / "b.py").read_text() == "B = 2\n"


# ---- failures and fallback ------------------------------------------------------------------------
def test_falls_back_to_the_api_when_claude_code_cannot_start(fake, tmp_path, monkeypatch):
    fake.start_error = CLINotFoundError("Claude Code not found")
    answered = []

    class FakeAPI:
        def __init__(self, **kw):
            pass

        async def create(self, **kw):
            answered.append(kw["model"])
            return NS(content=[NS(type="text", text="answered by the API")], stop_reason="end_turn",
                      usage=NS(input_tokens=1, output_tokens=1), model=kw["model"])
    monkeypatch.setattr("xqo.llm.AnthropicLLM", FakeAPI)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"]}])
    t = asyncio.run(bus.submit("x", agent="solo"))
    assert t.status == "done" and t.result == "answered by the API" and answered
    solo = bus.agents["solo"]
    assert (solo.provider, solo.provider_source) == ("anthropic", "fallback")

    bus = make_bus(tmp_path / "explicit", [{"name": "solo", "skills": ["python"], "provider": "claude-sdk"}])
    t = asyncio.run(bus.submit("x", agent="solo"))
    assert t.status != "done" and "could not start" in str(t.result)


def test_no_fallback_without_an_api_key_keeps_the_sign_in_error(fake, tmp_path, monkeypatch):
    """Default SDK provider, a token Claude Code rejects and no API key: say so, not "No Anthropic credentials"."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-expired")
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"]}])
    fake.rounds = [[("error", "authentication_failed")]]
    t = asyncio.run(bus.submit("x", agent="solo"))
    assert t.status != "done"
    assert "could not sign in" in str(t.result) and "CLAUDE_CODE_OAUTH_TOKEN" in str(t.result)
    assert "No Anthropic credentials" not in str(t.result)
    assert bus.agents["solo"].provider_source != "fallback"


def test_sign_in_failure_says_how_to_fix(fake, tmp_path):
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"], "provider": "claude-sdk"}])
    fake.rounds = [[("error", "authentication_failed")]]
    t = asyncio.run(bus.submit("x", agent="solo"))
    assert t.status != "done" and "/login" in str(t.result) and "ANTHROPIC_API_KEY" in str(t.result)


def test_single_calls_return_tool_calls_without_running_them(fake):
    """Chat side answers use create(): reply_user comes back as a tool_use block, like the API's."""
    from xqo.chat import REPLY_USER_TOOL
    fake.single = [("reply_user", {"message": "It is going well."})]
    r = asyncio.run(ClaudeSdkLLM().create(model="m", system="s", tools=[REPLY_USER_TOOL],
                                          messages=[{"role": "user", "content": "How is it going?"}]))
    assert r.stop_reason == "tool_use"
    assert [(b.name, b.input) for b in r.content if b.type == "tool_use"] == [
        ("reply_user", {"message": "It is going well."})]
    assert fake.options[0].tools == [] and not fake.runs  # no Claude Code tools on single calls


# ---- hq: departments and budgets -----------------------------------------------------------------
def _dept(tmp_path, **extra):
    d = tmp_path / "departments"
    d.mkdir(parents=True, exist_ok=True)
    (d / "lab.json").write_text(json.dumps({
        "name": "lab", "description": "Builds things.", "head": "lab_lead", "contacts": "*",
        "agents": [{"name": "lab_lead", "skills": ["planning"]},
                   {"name": "builder", "skills": ["python"], "can_run_code": True}], **extra}))


def test_department_budget_stops_the_run(fake, tmp_path):
    """Tokens from Claude Code count against the department budget; when it runs out (and the user
    doesn't extend it) the run stops before the next tool."""
    _dept(tmp_path, budget={"max_tokens": 250})
    bus = make_bus(tmp_path, [{"name": "boss", "skills": ["management"]}], departments=None,
                   attention_timeout=0.2)
    fake.rounds = [[("tool", "Write", {"file_path": f"f{i}.txt", "content": str(i)}) for i in range(6)]
                   + [("text", "never reached")]]
    t = asyncio.run(bus.submit("Build", agent="builder"))
    assert t.status == "error" and "budget exhausted" in t.result
    written = [c for c in fake.calls if c["tool"] == "Write" and "denied" not in c]
    assert 1 <= len(written) < 6
    assert bus.budget.report("lab")["tokens"] >= 250


def test_department_member_keeps_department_file_tools(fake, tmp_path):
    _dept(tmp_path)
    bus = make_bus(tmp_path, [{"name": "boss", "skills": ["management"]}], departments=None)
    fake.rounds = [[("tool", "mcp__xqo__write_file", {"path": "notes.md", "content": "lab",
                                                                "scope": "department"}),
                    ("tool", "Write", {"file_path": "task.md", "content": "task"}), ("text", "ok")]]
    t = asyncio.run(bus.submit("x", agent="builder"))
    assert t.status == "done", t.result
    tools = set(fake.runs[0].mcp_servers["xqo"]["tools"])
    assert {"write_file", "read_file", "run_code", "publish_files"} <= tools
    assert (bus.artifacts.workspace(t.id) / "task.md").read_text() == "task"
    assert not (bus.artifacts.workspace(t.id) / "notes.md").exists()  # went to the department workspace


# ---- the system-wide "no limits" hour (limits.py) -----------------------------------------------
class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def _limits_bus(tmp_path):
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"], "max_turns": 2, "max_tokens": 4096}])
    bus.limits.clock = _Clock()
    return bus


def test_no_limits_hour_starts_sessions_without_limits(fake, tmp_path):
    from xqo.limits import UNLIMITED_MAX_TOKENS
    bus = _limits_bus(tmp_path)
    asyncio.run(bus.submit("one", agent="solo"))
    assert fake.runs[-1].max_turns == 2 and fake.runs[-1].env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    bus.limits.enable()
    asyncio.run(bus.submit("two", agent="solo"))
    assert fake.runs[-1].max_turns is None
    assert fake.runs[-1].env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(UNLIMITED_MAX_TOKENS)


def test_no_limits_hour_running_out_stops_an_unlimited_session(fake, tmp_path):
    from xqo.limits import OVERRIDE_SECONDS
    bus = _limits_bus(tmp_path)
    bus.limits.enable()

    def expire():
        bus.limits.clock.now += OVERRIDE_SECONDS + 1
    fake.rounds = [[("text", "t1"), ("text", "t2"), ("do", expire), ("text", "t3"), ("text", "t4")]]
    t = asyncio.run(bus.submit("work", agent="solo"))
    assert t.result == "t3\n\n[stopped: max turns reached]"


def test_no_limits_hour_turned_on_mid_session_continues_past_max_turns(fake, tmp_path, monkeypatch):
    bus = _limits_bus(tmp_path)

    class MaxTurnsClient(FakeClient):
        async def receive_response(self):
            if not getattr(self, "capped", False):  # the first round runs into max_turns
                self.capped = True
                self.fake.prompts.append(self.inbox.pop(0) if self.inbox else None)
                bus.limits.enable()  # the user ticks the box meanwhile
                yield AssistantMessage([TextBlock("halfway")], message_id="h1")
                yield ResultMessage(subtype="error_max_turns", is_error=True)
                return
            async for m in super().receive_response():
                yield m
    monkeypatch.setattr(ClaudeSdkLLM, "client", lambda self, o: MaxTurnsClient(fake, o))
    fake.rounds = [[("text", "finished")]]
    t = asyncio.run(bus.submit("work", agent="solo"))
    assert t.status == "done" and t.result == "finished"
    assert fake.prompts[-1] == sdk_backend.CONTINUE_TURNS


def test_attachments_are_file_references_and_session_usage_is_recorded(fake, tmp_path):
    """Attachments reach Claude Code as paths it may Read (never write); delegated files are paths too; each
    session's result usage and cost is stored on the task, logged, shown and replayed."""
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"]}])
    fake.rounds = [
        [("tool", "Read", {"file_path": "../attachments/notes.txt"}),
         ("tool", "Write", {"file_path": "../attachments/notes.txt", "content": "changed"}),
         ("text", "Done.")],
    ]

    async def go():
        t = await bus.create("Use the notes", "solo", files=validate([("notes.txt", None, b"secret notes")]))
        bus.chat._queue(t.id, {"kind": "note", "id": "msg_1", "text": "One more file", "attachments": t.attachments})
        return await bus._run(t)
    t = asyncio.run(go())
    assert t.status == "done", t.result
    folder = bus.attachments.folder(t.id).resolve()
    [first] = fake.prompts
    assert isinstance(first, str) and f"- {folder / 'notes.txt'} (text" in first and "secret notes" not in first
    read, write = [c for c in fake.calls if c["tool"] in ("Read", "Write")]
    assert "denied" not in read and read["result"] == "secret notes"
    assert read["context"] == "One more file\n\n" + bus.attachments.refs(t.id, t.attachments)  # chat: paths too
    assert "outside the task workspace" in write["denied"]
    assert (folder / "notes.txt").read_bytes() == b"secret notes"

    u = t.usage
    assert u["cost_usd"] == 0.25 and u["sessions"] == 1 and u["queries"] == 1
    assert (u["input_tokens"], u["output_tokens"], u["turns"], u["duration_ms"]) == (1, 1, 3, 1000)
    done = [e for e in bus.history if e["kind"] == "done" and e["task_id"] == t.id]
    assert done[-1]["meta"]["usage"] == u
    assert any(e["kind"] == "llm" and e["meta"].get("session_usage") for e in bus.history)
    assert bus.tree(t.id)["usage"] == u
    again = make_bus(tmp_path, [{"name": "solo", "skills": ["python"]}])  # restart: replayed from the log
    assert again.tasks[t.id].usage == u


def test_plan_limit_updates_reach_the_header_usage(fake, tmp_path):
    """Claude Code's RateLimitEvent is kept per window for the header chip (GET /api/usage), told to the dashboard
    as an llm event, survives a restart, and drops off once its window has reset."""
    import time
    from fastapi.testclient import TestClient
    from xqo.web import create_app
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"]}])
    soon = int(time.time()) + 3600
    fake.rounds = [[("msg", RateLimitEvent(RateLimitInfo("allowed_warning", soon, "five_hour", 0.72))),
                    ("msg", RateLimitEvent(RateLimitInfo("allowed", int(time.time()) - 5, "seven_day", 0.3))),
                    ("msg", RateLimitEvent(RateLimitInfo("bogus"))), ("text", "Done.")]]

    async def go():
        return await bus._run(await bus.create("Work", "solo"))
    t = asyncio.run(go())
    assert t.status == "done", t.result
    ev = [e for e in bus.history if e["kind"] == "llm" and e["meta"].get("rate_limit")]
    assert [e["content"] for e in ev] == ["plan limit five hour 72%: allowed warning", "plan limit seven day 30%: allowed"]
    r = TestClient(create_app(bus)).get("/api/usage").json()
    assert [(x["type"], x["status"], x["utilization"], x["resets_at"]) for x in r["rate_limits"]] == \
        [("five_hour", "allowed_warning", 0.72, soon)]  # the seven-day window has reset already
    assert r["cost_usd"] == 0.25 and r["sessions"] == 1
    again = make_bus(tmp_path, [{"name": "solo", "skills": ["python"]}])
    assert [x["type"] for x in again.usage_total.report()["rate_limits"]] == ["five_hour"]


def test_usage_totals_rank_plan_limits_tightest_first(tmp_path):
    from xqo.usage import UsageTotals
    u = UsageTotals(tmp_path)
    assert u.report()["rate_limits"] == []
    u.rate_limit({"status": "allowed", "rate_limit_type": "seven_day", "utilization": 0.9})
    u.rate_limit({"status": "rejected", "rate_limit_type": "five_hour", "utilization": 1.4})
    u.rate_limit({"status": "allowed_warning", "rate_limit_type": "seven_day_opus"})
    assert u.rate_limit({"status": "nope"}) is None
    got = [(x["type"], x["utilization"]) for x in u.limits()]
    assert got == [("five_hour", 1.0), ("seven_day_opus", None), ("seven_day", 0.9)]
    u.rate_limit({"status": "allowed", "rate_limit_type": "five_hour", "utilization": 0.1})  # newest state wins
    assert [x["status"] for x in u.limits() if x["type"] == "five_hour"] == ["allowed"]
    (tmp_path / "usage.json").write_text('{"cost_usd": 1, "rate_limits": {"x": {"status": "weird"}, "y": 3}}')
    assert UsageTotals(tmp_path).report()["rate_limits"] == []


def test_move_file_without_a_shell(fake, tmp_path):
    """An agent with no Bash still renames files: move_file is an xqo tool, not a command."""
    bus = make_bus(tmp_path, [{"name": "planner", "skills": ["planning"], "write_paths": ["docs/*"]}])
    fake.rounds = [
        [("tool", "Write", {"file_path": "docs/draft.md", "content": "# Plan\n"}),
         ("tool", "mcp__xqo__move_file", {"src": "docs/draft.md", "dst": "docs/plan.md"}),
         ("text", "moved")],
    ]
    t = asyncio.run(bus.submit("Plan it", agent="planner"))
    assert t.status == "done"
    assert "Bash" not in fake.runs[0].tools
    assert "move_file" in fake.runs[0].mcp_servers["xqo"]["tools"]
    assert "move_file" in fake.runs[0].system_prompt
    assert [f["file"] for f in bus.artifacts.list_files(t.id)] == ["docs/plan.md"]


def test_grants_skills_and_tool_policy_in_claude_code(fake, tmp_path):
    """Remote MCP filters become exact allowed_tools, policy denies reach builtins and disallowed_tools, and
    skill packs are served by the in-process server."""
    sk = tmp_path / "skills" / "notes"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: notes\ndescription: How we write notes.\n---\nBe brief.\n")
    bus = make_bus(tmp_path, [{"name": "solo", "skills": ["python"], "skill_packs": ["notes"],
                               "mcp": {"gh": ["search_code", "get_file"], "docs": "*"},
                               "tools": {"deny": ["Bash", "mcp__docs__drop", "note"]}}],
                   mcp_servers=[{"name": "gh", "url": "https://gh/mcp"}, {"name": "docs", "url": "https://d/mcp"}])
    fake.rounds = [[("tool", "mcp__xqo__load_skill", {"name": "notes"}), ("text", "ok")]]
    t = asyncio.run(bus.submit("x", agent="solo"))
    assert t.status == "done"
    o = fake.runs[0]
    assert "Bash" not in o.tools and "Read" in o.tools
    assert {"mcp__gh__search_code", "mcp__gh__get_file", "mcp__docs"} <= set(o.allowed_tools)
    assert "mcp__gh" not in o.allowed_tools
    assert o.disallowed_tools == ["mcp__docs__drop"]
    served = set(o.mcp_servers["xqo"]["tools"])
    assert {"load_skill", "read_skill_file"} <= served and "note" not in served
    assert "notes: How we write notes." in o.system_prompt
    assert any(c.get("result", "").startswith("# Skill: notes") for c in fake.calls)
