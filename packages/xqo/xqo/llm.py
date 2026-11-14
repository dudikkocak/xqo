from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace as NS

from .models import new_id

DEFAULT_MODEL = os.getenv("XQO_MODEL", "claude-opus-5-5")
# Models that accept the server-side refusal fallback in its "default" form.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


EFFORTS = {"low", "medium", "high", "xhigh", "max"}
DEFAULT_MAX_TOKENS = int(os.getenv("XQO_MAX_TOKENS", "32000"))  # output limit per call (thinking included)
MIN_MAX_TOKENS = 1024


def resolve_max_tokens(agent: dict, cfg: dict, dept: dict | None = None) -> tuple[int, str]:
    """Output token limit for one agent: its "max_tokens", else the department's "default_max_tokens",
    else agents.json's "default_max_tokens", else XQO_MAX_TOKENS / 32000. Returns (value, source)."""
    for v, source in ((agent.get("max_tokens"), "agent"), ((dept or {}).get("default_max_tokens"), "department"),
                      (cfg.get("default_max_tokens"), "config")):
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or v < MIN_MAX_TOKENS:
            raise ValueError(f"agent {agent.get('name')!r}: max_tokens must be a whole number >= {MIN_MAX_TOKENS}")
        return v, source
    return DEFAULT_MAX_TOKENS, "default"


def resolve_model(agent: dict, cfg: dict, dept: dict | None = None) -> tuple[str, str | None, str]:
    """Model and effort for one agent: its own "model"/"effort", else its department's "default_model"/
    "default_effort", else agents.json's, else XQO_MODEL / the built-in default.
    Returns (model, effort, source) with source agent | department | config | default."""
    for key in ("model", "effort"):
        v = agent.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            raise ValueError(f"agent {agent.get('name')!r}: {key!r} must be a non-empty string")
    effort = agent.get("effort") or (dept or {}).get("default_effort") or cfg.get("default_effort", "medium")
    if effort and effort not in EFFORTS:
        raise ValueError(f"agent {agent.get('name')!r}: effort {effort!r} is not one of {sorted(EFFORTS)}")
    for model, source in ((agent.get("model"), "agent"), ((dept or {}).get("default_model"), "department"),
                          (cfg.get("default_model"), "config")):
        if model:
            return model.strip(), effort, source
    return DEFAULT_MODEL, effort, "default"


NO_CREDENTIALS = ("No Anthropic credentials: ANTHROPIC_API_KEY is not set in the environment that started the "
                  "xqo (and no `ant auth login` profile was found). Set it and restart: "
                  "PowerShell `$env:ANTHROPIC_API_KEY=\"sk-ant-...\"`, cmd `set ANTHROPIC_API_KEY=sk-ant-...`, "
                  "bash `export ANTHROPIC_API_KEY=sk-ant-...` (after `setx`, open a new terminal). "
                  "Or install the Claude Agent SDK (pip install \".[sdk]\") to sign in with your Claude Code login, "
                  "or run with --mock for the offline demo.")


def _no_credentials(e: Exception) -> bool:
    """The SDK raises a bare TypeError at request time when it finds no key, token or profile."""
    return isinstance(e, TypeError) and "Could not resolve authentication method" in str(e)


class AnthropicLLM:
    """Thin async wrapper over the Anthropic Messages API."""

    def __init__(self, fallbacks: bool = os.getenv("XQO_FALLBACKS", "1") != "0", spec: dict | None = None):
        from anthropic import AsyncAnthropic

        # default: ANTHROPIC_API_KEY / `ant auth login` profile; a provider spec can name another key
        # variable and a base_url (a gateway). The SDK retries 429/5xx/connection errors.
        spec = spec or {}
        kw: dict = {"max_retries": int(os.getenv("XQO_MAX_RETRIES", "4"))}
        env = spec.get("api_key_env")
        if env and env != "ANTHROPIC_API_KEY":
            if not os.getenv(env):
                raise ValueError(f"{env} is not set: export it before starting the xqo")
            kw["api_key"] = os.environ[env]
        if spec.get("base_url"):
            kw["base_url"] = spec["base_url"]
        self.client = AsyncAnthropic(**kw)
        self.fallbacks = fallbacks and not spec.get("base_url")

    async def create(self, *, model: str, system: str, messages: list, tools: list,
                     effort: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS, agent: str = "",
                     mcp_servers: list | None = None):
        kwargs: dict = dict(model=model, max_tokens=max_tokens, system=system,
                            messages=messages, tools=tools,
                            cache_control={"type": "ephemeral"})  # cache tools+system+history prefix
        if effort:
            kwargs["output_config"] = {"effort": effort}
        betas = []
        if self.fallbacks and model in FALLBACK_MODELS:
            betas.append("server-side-fallback-2026-07-01")
            kwargs["fallbacks"] = "default"
        if mcp_servers:  # remote MCP servers via the API's MCP connector
            betas.append("mcp-client-2025-11-20")
            kwargs["mcp_servers"] = mcp_servers
        if betas:
            kwargs["betas"] = betas
        # streamed: large output limits need it (the SDK refuses long non-streaming calls), and long
        # replies don't hit HTTP timeouts; the result is the same final message
        try:
            async with self.client.beta.messages.stream(**kwargs) as stream:
                return await stream.get_final_message()
        except TypeError as e:
            if _no_credentials(e):
                raise RuntimeError(NO_CREDENTIALS) from None
            raise

    async def title(self, prompt: str, model: str = DEFAULT_MODEL) -> str:
        """One-line task title written by the model."""
        try:
            resp = await self.client.messages.create(
                model=os.getenv("XQO_TITLE_MODEL", model), max_tokens=1000,
                output_config={"effort": "low"},
                system="Write a one-line title (at most 8 words) summarizing the user's task. "
                       "Reply with the title only: no quotes, no trailing period.",
                messages=[{"role": "user", "content": prompt[:4000]}])
        except TypeError as e:
            if _no_credentials(e):
                raise RuntimeError(NO_CREDENTIALS) from None
            raise
        if resp.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in resp.content if b.type == "text").strip()


def _text(t):
    return NS(type="text", text=t)


def _tool(name, **inp):
    return NS(type="tool_use", id=new_id("toolu"), name=name, input=inp)


class MockLLM:
    """Scripted offline LLM so the full flow (handover, direct, artifacts) can be demoed without a key.

    Agents with skill 'planning' delegate: handover to a 'python' agent, then ask 'reviewer'.
    Every other agent answers with a fenced code block.
    """

    CHAT_MARKERS = ("Message from the user (ask", "New instruction from the user (steer", "URGENT message from the user")

    def __init__(self, roster_skills: dict[str, list[str]], heads: dict[str, list] | None = None):
        self.skills = roster_skills
        self.heads = heads or {}  # department head -> [(member, skills)]
        self.failing = False  # demo: a prompt with "failing tests" makes the tester's runs fail

    @staticmethod
    def _texts(content) -> str:
        if isinstance(content, str):
            return content
        return "\n\n".join((b.get("text") or "") if isinstance(b, dict) else (getattr(b, "text", "") or "")
                         for b in content if (b.get("type") if isinstance(b, dict) else getattr(b, "type", "")) == "text")

    @staticmethod
    def _only_replies(m) -> bool:
        blocks = m["content"] if isinstance(m["content"], list) else []
        tools = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        return bool(tools) and all(b.name == "reply_user" for b in tools)

    async def create(self, *, model, system, messages, tools, effort=None, max_tokens=0, agent="", **_):
        # turns of the scripted flow; chat replies don't advance it
        turns = sum(1 for m in messages if m["role"] == "assistant" and not self._only_replies(m))
        first = messages[0]["content"]
        prompt = first if isinstance(first, str) else "\n".join(b.get("text", "") for b in first if b.get("type") == "text")
        usage = NS(input_tokens=len(json.dumps(prompt)) // 4, output_tokens=120)
        last = self._texts(messages[-1]["content"]) if messages[-1]["role"] == "user" else ""
        chat = [line for line in last.split("\n\n") if line.startswith(self.CHAT_MARKERS)]
        if chat:  # answer Ask / acknowledge Steer messages with reply_user, then carry on
            calls = []
            for c in chat:
                head, _, body = c.partition(":\n")
                verb = "Noted, adjusting the plan" if "steer" in head else "Answer"
                calls.append(_tool("reply_user", message=f"{verb} ({agent}): {body.strip()[:200]}"))
            return NS(content=[_text("Replying to the user.")] + calls, stop_reason="tool_use", usage=usage, model=model)
        if agent in self.heads:  # department head: assign the work to a member, then report back
            if turns == 0:
                body = prompt.split("\n\n", 1)[-1].split("\n\nShared notes board")[0]
                words = set(re.findall(r"[a-z]{4,}", body.lower()))
                members = self.heads[agent]
                pick = next((s for _, skills in members for s in skills
                             if any(w.startswith(x) or x.startswith(w) for w in re.findall(r"[a-z]{4,}", s)
                                    for x in words)), members[0][1][0] if members else "general")
                return NS(content=[_text(f"Assigning this inside the department (skill '{pick}')."),
                                   _tool("handover_task", skill=pick, task=body[-1500:],
                                         title=f"Department work: {pick}")],
                          stop_reason="tool_use", usage=usage, model=model)
            if turns == 1:
                return NS(content=[_tool("note", key="last_assignment", value="delegated and reviewed",
                                         scope="department")],
                          stop_reason="tool_use", usage=usage, model=model)
            done = next((m["content"][0]["content"] for m in messages
                         if m["role"] == "user" and isinstance(m["content"], list) and m["content"]
                         and m["content"][0].get("type") == "tool_result"), "")
            summary = (done.strip().splitlines() or ["(no result)"])[0][:300]
            return NS(content=[_text(f"Department result: {summary}")], stop_reason="end_turn",
                      usage=usage, model=model)
        if "planning" in self.skills.get(agent, []):
            if turns == 0:
                self.failing = "failing tests" in prompt.lower()
            if "This task ran before" in prompt:  # reopened: the reply above was enough
                return NS(content=[_text("Continued the task: answered the user; the workspace is unchanged.")],
                          stop_reason="end_turn", usage=usage, model=model)
            if "confirm" in prompt.lower():  # demo of the attention flow
                if turns == 0:
                    return NS(content=[_text("I need a decision first."),
                                       _tool("ask_user", question="Use an iterative or a recursive fib?",
                                             reason="approval", options=["iterative", "recursive"],
                                             default="iterative")],
                              stop_reason="tool_use", usage=usage, model=model)
                turns -= 1
            if turns == 0:
                content = [_text("Plan: delegate implementation to a python specialist."),
                           _tool("note", key="approach", value="iterative fib, O(n), raise on negative n"),
                           _tool("handover_task", skill="python", task=f"Implement: {prompt[-300:]}",
                                 title="Implement fib in Python")]
                return NS(content=content, stop_reason="tool_use", usage=usage, model=model)
            if "run tests" in prompt.lower() and "tester" in self.skills:  # demo of the sandbox
                if turns == 1:
                    return NS(content=[_text("Asking the tester to run the tests."),
                                       _tool("handover_task", skill="testing", task="Run the test suite in the workspace.",
                                             title="Run fib tests")],
                              stop_reason="tool_use", usage=usage, model=model)
                if turns > 1:
                    turns -= 1
            if turns == 1 and "reviewer" in self.skills:
                return NS(content=[_text("Asking reviewer for feedback."),
                                   _tool("ask_agent", agent="reviewer", message="Review the implementation for bugs.",
                                         title="Review fib implementation")],
                          stop_reason="tool_use", usage=usage, model=model)
            body = "Final plan & summary:\n\n```markdown\n# Summary\n- implementation delegated\n- review collected\n```\n"
            return NS(content=[_text(body)], stop_reason="end_turn", usage=usage, model=model)
        if agent == "tester":
            last = messages[-1]["content"][0].get("content", "") if isinstance(messages[-1]["content"], list) \
                and isinstance(messages[-1]["content"][0], dict) else ""
            if self.failing and turns < 6 and "stop fixing" not in str(last):
                return NS(content=[_text("Running the tests."),
                                   _tool("run_code", command="python -c \"import sys; print('FAILED tests/test_fib.py::test_fib'); sys.exit(1)\"")],
                          stop_reason="tool_use", usage=usage, model=model)
            if turns == 0:
                return NS(content=[_text("Running the tests."),
                                   _tool("run_code", command="python -c \"from src.fib import fib; assert fib(10) == 55; print('fib ok')\"")],
                          stop_reason="tool_use", usage=usage, model=model)
            return NS(content=[_text(f"Test run finished:\n\n```text\n{last[:2000]}\n```")],
                      stop_reason="end_turn", usage=usage, model=model)
        if agent == "reviewer":
            body = ("Added a guard for negative input:\n\n```diff\n--- a/src/fib.py\n+++ b/src/fib.py\n"
                    "@@ -1,2 +1,4 @@\n def fib(n: int) -> int:\n+    if n < 0:\n+        raise ValueError(\"n must be >= 0\")\n"
                    "     a, b = 0, 1\n```\n\nReview notes:\n\n```json\n{\"issues\": [], \"verdict\": \"approve\"}\n```")
        else:
            body = ("Here is the implementation:\n\n```python src/fib.py\ndef fib(n: int) -> int:\n"
                    "    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n```\n\n"
                    "**tests/test_fib.py**\n```python\nfrom src.fib import fib\n\n\ndef test_fib():\n"
                    "    assert fib(10) == 55\n```")
        return NS(content=[_text(body)], stop_reason="end_turn", usage=usage, model=model)
