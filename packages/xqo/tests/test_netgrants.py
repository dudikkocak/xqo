"""request_network: an agent asks for network for an offline run; the user approves (once / for the task) or denies;
no answer in time is a deny."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from xqo import netgrants
from xqo.agent import Agent
from xqo.artifacts import ArtifactStore
from xqo.bus import MessageBus
from xqo.sandbox import BACKENDS, Backend, Sandbox, SandboxConfig, format_result, register_backend


def text(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=None, model="m")


def tool(name, **inp):
    return NS(content=[NS(type="tool_use", id=f"t_{name}", name=name, input=inp)],
              stop_reason="tool_use", usage=None, model="m")


class Script:
    def __init__(self, fn):
        self.fn, self.seen, self.tools = fn, [], []

    async def create(self, *, messages, tools=None, **_):
        self.seen.append(messages[-1]["content"])
        self.tools.append([t["name"] for t in tools or []])
        return self.fn(sum(1 for m in messages if m["role"] == "assistant"), messages)


@pytest.fixture
def netfake():
    """A backend that isolates the network and prints whether this run got it."""
    @register_backend
    class NetFake(Backend):
        name = "netfake"
        isolates_network = True

        async def command(self, sb, work, command, env_dir, comps, network=False):
            return {"argv": [sys.executable, "-c", f"print('network={network}: ' + {command!r})"], "env": None,
                    "run_cmd": command}
    yield
    BACKENDS.pop("netfake", None)


def make(tmp_path, fn, timeout=30, **cfg):
    store = ArtifactStore(tmp_path / "art")
    b = MessageBus(tmp_path / "ev.jsonl")
    b.artifacts = store
    b.attention.timeout = timeout
    b.sandbox = Sandbox(SandboxConfig(**{"backend": "netfake", **cfg}))
    llm = Script(fn)
    b.register(Agent("tester", ["testing"], "sys", llm, store, can_run_code=True))
    return b, llm


def asks(grant="run", then=None):
    def fn(turn, msgs):
        if turn == 0:
            return tool("request_network", command="npm run build", reason="next/font downloads Google fonts",
                        grant=grant)
        if turn == 1 and then:
            return tool("run_code", command=then)
        return text(msgs[-1]["content"][0]["content"])
    return fn


def answer_with(b, reply):
    async def scenario():
        job = asyncio.create_task(b.submit("build it"))
        while not b.attention.pending():
            await asyncio.sleep(0.01)
        att = b.attention.pending()[0]
        assert att.kind == "network" and att.default == netgrants.DENY and "$ npm run build" in att.question
        assert "Google fonts" in att.question and att.options[0] == netgrants.DENY
        if reply is not None:
            b.attention.resolve(att.id, reply)
        return await job
    return asyncio.run(scenario())


def test_choice_parsing():
    assert netgrants.choice(netgrants.ALLOW_RUN) == "run" and netgrants.choice(netgrants.ALLOW_TASK) == "task"
    assert netgrants.choice("yes") == "run" and netgrants.choice("approve for the task") == "task"
    for a in (netgrants.DENY, "no", "", "maybe", "not now"):
        assert netgrants.choice(a) == "deny"


def test_tool_offered_only_when_runs_are_offline(tmp_path, netfake):
    def tools_of(**cfg):
        b, llm = make(tmp_path / str(len(cfg)) / json.dumps(cfg).replace("/", "_"), lambda *_: text("ok"), **cfg)
        asyncio.run(b.submit("x"))
        return llm.tools[0]
    assert "request_network" in tools_of() and "run_code" in tools_of()
    assert "request_network" not in tools_of(network=True)            # runs already have network
    assert "request_network" not in tools_of(network_requests=False)  # switched off (e.g. per department)
    assert "request_network" not in tools_of(backend="local")         # local runs always have network


def test_approved_once_runs_with_network_then_offline_again(tmp_path, netfake):
    b, llm = make(tmp_path, asks("run", then="npm test"))
    t = answer_with(b, netgrants.ALLOW_RUN)
    first, second = llm.seen[1][0]["content"], llm.seen[2][0]["content"]
    assert "network=True: npm run build" in first and "[network: on for this run (approved by the user)]" in first
    assert "network=False: npm test" in second  # a one-off grant: the next run is offline
    ev = next(e for e in b.history if e["kind"] == "network_request")
    assert ev["meta"]["answer"] == "run" and not ev["meta"]["auto"] and ev["meta"]["reason"].startswith("next/font")
    assert t.status == "done"


def test_approved_for_the_task_covers_later_runs(tmp_path, netfake):
    b, llm = make(tmp_path, asks("task", then="npm test"))
    answer_with(b, netgrants.ALLOW_TASK)
    assert "network=True: npm test" in llm.seen[2][0]["content"]
    assert netgrants.granted(b, next(iter(b.tasks.values())))


def test_denied_or_unanswered_never_runs(tmp_path, netfake):
    b, llm = make(tmp_path, asks())
    answer_with(b, netgrants.DENY)
    out = llm.seen[1][0]
    assert out["is_error"] and "denied by the user" in out["content"] and "network=" not in out["content"]
    b, llm = make(tmp_path / "late", asks(), timeout=0.2)
    asyncio.run(b.submit("build it"))  # nobody answers: the default is Deny, never an approval
    out = llm.seen[1][0]
    assert out["is_error"] and "did not answer in time" in out["content"]
    ev = next(e for e in b.history if e["kind"] == "network_request")
    assert ev["meta"]["answer"] == "deny" and ev["meta"]["auto"]


def test_backends_without_isolation_ignore_the_flag(tmp_path):
    r = asyncio.run(Sandbox(SandboxConfig(backend="local")).run(tmp_path / "ws", "echo hi", network=True))
    assert r["exit_code"] == 0 and "network" not in r and "[network" not in format_result(r)
