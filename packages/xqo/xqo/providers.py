"""Model providers per agent: the Claude Agent SDK (default when installed), Anthropic, or any OpenAI-compatible
chat API (OpenAI, Ollama, vLLM, …).

agents.json:
    "default_provider": "anthropic",                  # optional; agents and departments can override
    "providers": {                                     # optional; extends / overrides the presets below
        "openai": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
        "local":  {"type": "openai", "base_url": "http://localhost:8001/v1", "api_key_env": "LOCAL_LLM_KEY"}},
    "agents": [{"name": "writer", "provider": "openai", "model": "gpt-5.1", ...},
               {"name": "helper", "provider": "http://localhost:11434/v1", "model": "llama3.3"}]

Keys are only ever read from environment variables named by "api_key_env" (never written in the config).
The OpenAI-compatible adapter speaks the agents' Anthropic-shaped messages: tool definitions become
functions, tool_use/tool_result become tool_calls/tool messages, images become image_url parts, and the
reply comes back as text / tool_use blocks with the same stop reasons and usage fields, so tools,
artifact extraction, patching, chat and budgets work the same. Remote MCP connector servers and the
server-side refusal fallback are Anthropic-only and are skipped for other providers.

"claude-sdk" (pip install ".[sdk]") runs each agent with the Claude Agent SDK, which drives Claude Code:
it signs in with ANTHROPIC_API_KEY or an existing `claude` login, and the agent also gets Claude Code's own
file tools. See sdk_backend.py. Without the package the default provider is "anthropic".
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
from types import SimpleNamespace as NS

from .models import new_id

PRESETS = {
    "anthropic": {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
               "max_tokens_param": "max_completion_tokens", "reasoning_effort": True},
    "ollama": {"type": "openai", "base_url": "http://localhost:11434/v1", "api_key_env": None},
    "claude-sdk": {"type": "claude-sdk"},
}
TYPES = ("anthropic", "openai", "claude-sdk")
URL = re.compile(r"^https?://", re.I)
STOP = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens",
        "content_filter": "refusal"}
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class ProviderError(RuntimeError):
    """An API call to a non-Anthropic provider failed after retries (the user is asked to retry)."""


def sdk_installed() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


def default_provider() -> str:
    """XQO_PROVIDER, else the Claude Agent SDK when it is installed, else the Anthropic API."""
    return os.getenv("XQO_PROVIDER") or ("claude-sdk" if sdk_installed() else "anthropic")


def provider_specs(cfg: dict) -> dict[str, dict]:
    specs = {k: dict(v) for k, v in PRESETS.items()}
    for name, spec in (cfg.get("providers") or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"provider {name!r}: expected an object")
        if "api_key" in spec:
            raise ValueError(f"provider {name!r}: put the key in an environment variable and name it in "
                             "'api_key_env'; keys are not read from agents.json")
        base = specs.get(name, {})
        specs[name] = {**base, **spec}
        specs[name].setdefault("type", "openai")
        if specs[name]["type"] not in TYPES:
            raise ValueError(f"provider {name!r}: type must be one of {', '.join(TYPES)}")
        if specs[name]["type"] == "openai" and not specs[name].get("base_url"):
            raise ValueError(f"provider {name!r}: an OpenAI-compatible provider needs 'base_url'")
    return specs


def resolve_provider(agent: dict, cfg: dict, dept: dict | None = None) -> tuple[str, dict, str]:
    """(name, spec, source) for an agent: its "provider", else the department's "default_provider",
    else agents.json's "default_provider", else default_provider(). A provider can also be an OpenAI-compatible URL."""
    specs = provider_specs(cfg)
    for value, source in ((agent.get("provider"), "agent"), ((dept or {}).get("default_provider"), "department"),
                          (cfg.get("default_provider"), "config"), (default_provider(), "default")):
        if not value:
            continue
        if not isinstance(value, str):
            raise ValueError(f"agent {agent.get('name')!r}: provider must be a name or a URL")
        value = value.strip()
        if URL.match(value):
            spec = {"type": "openai", "base_url": value.rstrip("/"), "api_key_env": None}
        elif value in specs:
            spec = dict(specs[value])
        else:
            raise ValueError(f"agent {agent.get('name')!r}: unknown provider {value!r}. "
                             f"Known: {', '.join(sorted(specs))}, or an http(s) URL")
        if agent.get("api_key_env") and source == "agent":
            spec["api_key_env"] = agent["api_key_env"]
        if spec["type"] == "claude-sdk" and not sdk_installed():
            raise ValueError(f"agent {agent.get('name')!r}: provider {value!r} needs the Claude Agent SDK: "
                             "pip install \".[sdk]\" (or claude-agent-sdk)")
        if spec["type"] == "claude-sdk" and source == "default" and not os.getenv("XQO_PROVIDER"):
            spec["fallback"] = "anthropic"  # picked only because it is installed: may fall back to the API
        return value, spec, source
    raise AssertionError("unreachable")


def describe(spec: dict) -> dict:
    """Safe summary for the dashboard: never the key itself."""
    if spec["type"] == "claude-sdk":
        from .sdk_backend import auth_method
        return {"type": "claude-sdk", "base_url": None, "api_key_env": spec.get("api_key_env"),
                "key_set": None, "auth": auth_method(spec)}
    env = spec.get("api_key_env")
    return {"type": spec["type"], "base_url": spec.get("base_url"), "api_key_env": env,
            "key_set": bool(env and os.getenv(env)) if env else None}


class Clients:
    """One client per distinct provider, shared by the agents that use it."""

    def __init__(self, anthropic_factory, transport=None):
        self._anthropic = anthropic_factory
        self._transport = transport
        self._cache: dict[str, object] = {}

    def get(self, spec: dict):
        key = json.dumps(spec, sort_keys=True)
        if key not in self._cache:
            if spec["type"] == "anthropic":
                self._cache[key] = self._anthropic(spec)
            elif spec["type"] == "claude-sdk":
                from .sdk_backend import ClaudeSdkLLM
                self._cache[key] = ClaudeSdkLLM(spec)
            else:
                self._cache[key] = OpenAICompatLLM(spec, transport=self._transport)
        return self._cache[key]


# ---------------------------------------------------------------- OpenAI-compatible adapter
def _attr(b, k, default=None):
    return b.get(k, default) if isinstance(b, dict) else getattr(b, k, default)


def _block_text(b) -> str:
    return _attr(b, "text", "") or ""


def to_openai_messages(system: str, messages: list) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            text = "\n".join(_block_text(b) for b in blocks if _attr(b, "type") == "text").strip()
            calls = [{"id": _attr(b, "id"), "type": "function",
                      "function": {"name": _attr(b, "name"), "arguments": json.dumps(_attr(b, "input") or {})}}
                     for b in blocks if _attr(b, "type") == "tool_use"]
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        if isinstance(content, str):
            out.append({"role": "user", "content": content})
            continue
        parts: list[dict] = []
        for b in content:
            t = _attr(b, "type")
            if t == "tool_result":  # tool messages must follow the assistant turn directly
                c = _attr(b, "content")
                if isinstance(c, list):
                    c = "\n".join(_block_text(x) for x in c)
                if _attr(b, "is_error"):
                    c = f"[error] {c}"
                out.append({"role": "tool", "tool_call_id": _attr(b, "tool_use_id"), "content": str(c or "")})
            elif t == "text":
                parts.append({"type": "text", "text": _block_text(b)})
            elif t == "image":
                src = _attr(b, "source") or {}
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{src.get('media_type')};base64,{src.get('data')}"}})
            elif t == "document":
                src = _attr(b, "source") or {}
                title = _attr(b, "title") or "document"
                if src.get("type") == "text":
                    parts.append({"type": "text", "text": f"--- {title} ---\n{src.get('data', '')}"})
                else:
                    parts.append({"type": "text", "text": f"[{title}: PDF attachments are only supported with "
                                                          "the Anthropic provider]"})
        if parts:
            out.append({"role": "user", "content": parts if any(p["type"] != "text" for p in parts)
                        else "\n\n".join(p["text"] for p in parts)})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}}
            for t in tools if "input_schema" in t]  # server-side tool sets (remote MCP) are Anthropic-only


def from_openai(data: dict, model: str):
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    if msg.get("content"):
        text = msg["content"] if isinstance(msg["content"], str) else \
            "".join(p.get("text", "") for p in msg["content"] if isinstance(p, dict))
        if text:
            content.append(NS(type="text", text=text))
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        content.append(NS(type="tool_use", id=c.get("id") or new_id("call"), name=fn.get("name"),
                          input=args if isinstance(args, dict) else {"value": args}))
    stop = STOP.get(choice.get("finish_reason") or "stop", "end_turn")
    if stop != "tool_use" and any(b.type == "tool_use" for b in content):
        stop = "tool_use"  # some servers say "stop" even with tool calls
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    usage = NS(input_tokens=(u.get("prompt_tokens") or 0) - cached, output_tokens=u.get("completion_tokens") or 0,
               cache_read_input_tokens=cached, cache_creation_input_tokens=0)
    return NS(content=content, stop_reason=stop, usage=usage, model=data.get("model") or model)


class OpenAICompatLLM:
    """Chat Completions client with the same create()/title() interface as AnthropicLLM."""

    RETRY = {408, 409, 429, 500, 502, 503, 504}

    def __init__(self, spec: dict, transport=None, retries: int = int(os.getenv("XQO_MAX_RETRIES", "4"))):
        import httpx

        self.spec = spec
        self.retries = retries
        self.client = httpx.AsyncClient(base_url=spec["base_url"].rstrip("/") + "/", transport=transport,
                                        timeout=httpx.Timeout(float(spec.get("timeout", 600)), connect=15))

    def _headers(self) -> dict:
        env = self.spec.get("api_key_env")
        h = {"Content-Type": "application/json", **(self.spec.get("headers") or {})}
        if env:
            key = os.getenv(env)
            if not key:
                raise ProviderError(f"{env} is not set: export it before starting the xqo")
            h["Authorization"] = f"Bearer {key}"
        return h

    async def _post(self, body: dict) -> dict:
        import httpx

        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                r = await self.client.post("chat/completions", json=body, headers=self._headers())
            except httpx.HTTPError as e:
                if attempt >= self.retries:
                    raise ProviderError(f"{self.spec['base_url']}: {type(e).__name__}: {e}") from None
            else:
                if r.status_code < 400:
                    return r.json()
                if r.status_code not in self.RETRY or attempt >= self.retries:
                    raise ProviderError(f"{self.spec['base_url']} answered {r.status_code}: {r.text[:500]}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        raise ProviderError("unreachable")

    async def create(self, *, model: str, system: str, messages: list, tools: list, effort: str | None = None,
                     max_tokens: int = 32000, agent: str = "", mcp_servers: list | None = None):
        body: dict = {"model": model, "messages": to_openai_messages(system, messages),
                      self.spec.get("max_tokens_param", "max_tokens"): max_tokens}
        fns = to_openai_tools(tools)
        if fns:
            body["tools"] = fns
        if effort and self.spec.get("reasoning_effort"):
            body["reasoning_effort"] = EFFORT.get(effort, effort)
        body.update(self.spec.get("extra_body") or {})
        return from_openai(await self._post(body), model)

    async def title(self, prompt: str, model: str = "") -> str:
        r = await self.create(model=model, system="Write a one-line title (at most 8 words) summarizing the user's "
                              "task. Reply with the title only: no quotes, no trailing period.",
                              messages=[{"role": "user", "content": prompt[:4000]}], tools=[], max_tokens=200)
        return "".join(b.text for b in r.content if b.type == "text").strip()


def image_data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"
