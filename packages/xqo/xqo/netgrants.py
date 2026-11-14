"""Network access on request: an agent whose runs are offline (Docker backend) asks for network for one command or
for the rest of the task; the user approves or denies on an attention card. No reply in time means DENY (never an
auto-approval), and an unattended scheduled run is denied at once. Grants are per task (its root) and in memory:
a restart starts offline again. Every request and answer is logged as a `network_request` event."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .models import Event

if TYPE_CHECKING:
    from .models import Task

DENY, ALLOW_RUN, ALLOW_TASK = "Deny", "Allow this run", "Allow for this task"

REQUEST_NETWORK_TOOL = {
    "name": "request_network",
    "description": "Your run_code commands run WITHOUT network. When one truly needs it (a build that downloads "
                   "fonts or engines, a test against a real service), ask the user here: if they approve, the "
                   "command runs with network and you get its result like run_code. If they deny or don't answer "
                   "in time, it does not run. Don't ask for installing packages: add them to package.json / "
                   "requirements / go.mod / Cargo.toml / the project file instead, they are installed before runs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run with network."},
            "reason": {"type": "string", "description": "Why it needs network: what it downloads or contacts."},
            "grant": {"type": "string", "enum": ["run", "task"],
                      "description": "run (default): this command only; task: every run for the rest of this task."},
            "timeout": {"type": "number", "description": "Seconds (capped by config)."},
            "scope": {"type": "string", "enum": ["task", "department"],
                      "description": "Which workspace, as for run_code (default task)."},
        },
        "required": ["command", "reason"], "additionalProperties": False,
    },
}


def choice(answer: str) -> str:
    """deny | run | task from the user's answer (a button or typed text; anything unclear is a deny)."""
    a = (answer or "").strip().lower()
    yes = a.startswith(("allow", "approve", "yes", "ok"))
    if yes and "task" in a:
        return "task"
    return "run" if yes else "deny"


def grants(bus) -> set[str]:
    g = getattr(bus, "network_grants", None)
    if g is None:
        g = bus.network_grants = set()
    return g


def granted(bus, task: Task) -> bool:
    return (task.root_id or task.id) in grants(bus)


async def ask(agent, task: Task, inp: dict) -> tuple[str, bool]:
    """Put the request in front of the user; returns (deny|run|task, auto_answered)."""
    cmd, reason = str(inp.get("command", "")), str(inp.get("reason", "")).strip() or "(no reason given)"
    scope = "the rest of this task" if inp.get("grant") == "task" else "this command only"
    question = (f"{agent.name} asks to run a command WITH network access (runs are offline otherwise):\n"
                f"$ {cmd}\nReason: {reason}\nAsked for: {scope}.")
    options = [DENY, ALLOW_TASK, ALLOW_RUN] if inp.get("grant") == "task" else [DENY, ALLOW_RUN, ALLOW_TASK]
    answer, auto = await agent.bus.attention.request(task, agent.name, "network", question, default=DENY,
                                                     options=options)
    pick = "deny" if auto else choice(answer)
    if pick == "task":
        grants(agent.bus).add(task.root_id or task.id)
    agent.bus.emit(Event("network_request", agent.name, None, task.id, task.parent_id,
                         f"{'denied' if pick == 'deny' else 'allowed (' + pick + ')'}"
                         f"{' (no reply in time)' if auto else ''}: $ {cmd[:200]}",
                         {"command": cmd, "reason": reason, "asked": inp.get("grant") or "run", "answer": pick,
                          "auto": auto}))
    return pick, auto


def denied_text(auto: bool) -> str:
    return ("Network access was denied" + (" (the user did not answer in time)" if auto else " by the user")
            + "; the command did not run. Work without it: put the files in the workspace, add the dependency to "
              "the manifest so the sandbox installs it before runs, or skip that step and say so.")
