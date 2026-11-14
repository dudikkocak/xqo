from __future__ import annotations

import asyncio
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING

from .compaction import compact
from .artifacts import ArtifactStore, diff_targets
from .bus import HandoverError
from .mcp_hub import PREFIX as MCP_PREFIX
from .grants import ToolPolicy, tool_allowed
from .mcp_hub import remote_params
from .skills import RUN_DIR as SKILLS_RUN_DIR, SKILL_TOOLS, prompt_section
from .patching import PatchError
from .sandbox import format_result
from . import netgrants
from .llm import DEFAULT_MAX_TOKENS, DEFAULT_MODEL
from .attachments import describe
from .fences import normalize_path
from .departments import HQ, BudgetExceeded, dept_root
from .chat import REPLY_USER_TOOL, RunStopped, add_user_text
from .sdk_backend import SDK_TOOLS
from .models import Event, Task

if TYPE_CHECKING:
    from .bus import MessageBus

TOOLS = [
    {
        "name": "list_agents",
        "description": "List all agents in the xqo with their skills.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ask_agent",
        "description": "Send a direct message to a named agent and wait for its reply. "
                       "Use when you know exactly which agent you need. The message must be complete: never "
                       "leave template placeholders such as {content}; never paste file contents: name workspace files "
                       "in 'files' and the receiver reads them.",
        "input_schema": {
            "type": "object",
            "properties": {"agent": {"type": "string"}, "message": {"type": "string"},
                           "attachments": {"type": "array", "items": {"type": "string"},
                                           "description": "Names of task attachments to forward (optional)."},
                           "files": {"type": "array", "items": {"type": "string"},
                                     "description": "Shared task workspace paths the receiver should read (optional). "
                                                    "They are sent as references, not content."},
                           "title": {"type": "string", "description": "One-line summary of the request (max ~8 words)."}},
            "required": ["agent", "message", "title"], "additionalProperties": False,
        },
    },
    {
        "name": "handover_task",
        "description": "Broadcast a lookup for an agent with a specific skill, hand the task over "
                       "to the best offer, and return its result. Use for work outside your own skills. "
                       "The lookup searches your own department first, then the heads of departments you may "
                       "contact. The task text must be self-contained (no placeholders such as {content}); "
                       "never paste file contents: name workspace files in 'files' and the receiver reads them.",
        "input_schema": {
            "type": "object",
            "properties": {"skill": {"type": "string", "description": "One of the skills listed for the "
                                     "agents in your system prompt (e.g. 'python', 'testing'), or an agent's name."},
                           "task": {"type": "string"},
                           "department": {"type": "string", "description": "Send the lookup straight to this "
                                          "department (optional; same as writing the skill as '@dept skill')."},
                           "attachments": {"type": "array", "items": {"type": "string"},
                                           "description": "Names of task attachments to forward (optional)."},
                           "files": {"type": "array", "items": {"type": "string"},
                                     "description": "Shared task workspace paths the receiver should read (optional). "
                                                    "They are sent as references, not content."},
                           "title": {"type": "string", "description": "One-line summary of the task (max ~8 words)."}},
            "required": ["skill", "task", "title"], "additionalProperties": False,
        },
    },
]

ASK_USER_TOOL = {
    "name": "ask_user",
    "description": "Flag the human operator and wait for their reply. Use only when you are blocked, need "
                   "approval for something risky or irreversible, or need information you cannot get otherwise. "
                   "If they don't reply in time, 'default' is used automatically, so make it a safe choice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Self-contained question for the user."},
            "reason": {"type": "string", "enum": ["question", "approval", "missing_info", "blocked"]},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "Suggested answers (optional)."},
            "default": {"type": "string", "description": "Answer used if the user does not reply in time."},
        },
        "required": ["question", "reason", "default"], "additionalProperties": False,
    },
}

FILE_TOOLS = [
    {
        "name": "list_files",
        "description": "List files in this task's shared workspace (every named artifact any agent produced).",
        "input_schema": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["task", "department"],
                                      "description": "task (default): the shared task workspace; department: "
                                                     "your department's private workspace."}}, "additionalProperties": False},
    },
    {
        "name": "read_file",
        "description": "Read the current content of a workspace file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "scope": {"type": "string", "enum": ["task", "department"],
                                      "description": "task (default): the shared task workspace; department: "
                                                     "your department's private workspace."}},
                         "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a workspace file with full content.",
        "input_schema": {"type": "object",
                         "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "scope": {"type": "string", "enum": ["task", "department"],
                                      "description": "task (default): the shared task workspace; department: "
                                                     "your department's private workspace."}},
                         "required": ["path", "content"], "additionalProperties": False},
    },
    {
        "name": "patch_file",
        "description": "Patch an existing workspace file. Give either 'edits' (each 'old' must match exactly "
                       "once; applied in order) or 'diff' (unified diff). A new timestamped version is saved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {"type": "array", "items": {
                    "type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                    "required": ["old", "new"], "additionalProperties": False}},
                "diff": {"type": "string"},
                "scope": {"type": "string", "enum": ["task", "department"],
                                      "description": "task (default): the shared task workspace; department: "
                                                     "your department's private workspace."},
            },
            "required": ["path"], "additionalProperties": False,
        },
    },
    {
        "name": "move_file",
        "description": "Move or rename a workspace file (use this instead of git mv or mv; the xqo commits "
                       "it as a rename). Fails if 'dst' already exists.",
        "input_schema": {"type": "object",
                         "properties": {"src": {"type": "string"}, "dst": {"type": "string"},
                                        "scope": {"type": "string", "enum": ["task", "department"],
                                                  "description": "task (default): the shared task workspace; "
                                                                 "department: your department's private workspace."}},
                         "required": ["src", "dst"], "additionalProperties": False},
    },
    {
        "name": "delete_path",
        "description": "Delete a workspace file or a whole folder with everything in it (e.g. a leftover "
                       "node_modules, build or cache folder). Use this instead of rm: shell commands run on a "
                       "copy of the workspace, so rm there changes nothing. The .git folder can't be deleted.",
        "input_schema": {"type": "object",
                         "properties": {"path": {"type": "string"},
                                        "scope": {"type": "string", "enum": ["task", "department"],
                                                  "description": "task (default): the shared task workspace; "
                                                                 "department: your department's private workspace."}},
                         "required": ["path"], "additionalProperties": False},
    },
]
MEMORY_TOOLS = [
    {
        "name": "note",
        "description": "Write a fact or decision to the task's shared notes board, visible to every agent on this "
                       "task. Same key overwrites; empty value deletes. Keep notes short.",
        "input_schema": {"type": "object",
                         "properties": {"key": {"type": "string"}, "value": {"type": "string"},
                                        "scope": {"type": "string", "enum": ["task", "department"],
                                                  "description": "task (default) or your department's own board."}},
                         "required": ["key", "value"], "additionalProperties": False},
    },
    {
        "name": "read_notes",
        "description": "Read the task's shared notes board (or your department's board with scope=department).",
        "input_schema": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["task", "department"]}},
                         "additionalProperties": False},
    },
]

RUN_CODE_TOOL = {
    "name": "run_code",
    "description": "Run a shell command in a sandbox containing a fresh copy of the task workspace "
                   "(e.g. 'python -m pytest -q', 'npm test', 'cd web && npm run build', 'go test ./...', "
                   "'cargo test', 'dotnet test'). Dependencies listed in the workspace's requirements/pyproject, "
                   "package.json, go.mod, Cargo.toml or project files are installed before the run. Changes made by "
                   "the command are discarded and git is not available there: change files (including those "
                   "dependency files) with write_file / patch_file / move_file / delete_path. Returns exit code, "
                   "stdout and stderr.",
    "input_schema": {"type": "object",
                     "properties": {"command": {"type": "string"},
                                    "timeout": {"type": "number", "description": "Seconds (capped by config)."},
                                    "scope": {"type": "string", "enum": ["task", "department"],
                                      "description": "task (default): the shared task workspace; department: "
                                                     "your department's private workspace."}},
                     "required": ["command"], "additionalProperties": False},
}

PUBLISH_TOOL = {
    "name": "publish_files",
    "description": "Copy files from your department's private workspace into the shared task workspace so other "
                   "departments and headquarters can see them. Each copy is a new 'publish' version.",
    "input_schema": {"type": "object",
                     "properties": {"files": {"type": "array", "items": {"type": "string"},
                                              "description": "Paths in the department workspace; ['*'] = all."}},
                     "required": ["files"], "additionalProperties": False},
}

REPORT_TOOL = {
    "name": "report",
    "description": "Write a report for the user in Markdown (progress, findings, a summary of finished work). It is "
                   "saved in the artifact history under your name and listed in the office next to you. Your final "
                   "answer is saved as a report too, so use this for reports you want to give along the way.",
    "input_schema": {"type": "object",
                     "properties": {"title": {"type": "string", "description": "A short title."},
                                    "markdown": {"type": "string", "description": "The report, in Markdown."}},
                     "required": ["title", "markdown"], "additionalProperties": False},
}

TOOLS = TOOLS + [ASK_USER_TOOL, REPLY_USER_TOOL] + FILE_TOOLS + MEMORY_TOOLS + [REPORT_TOOL]

ARTIFACT_GUIDE = """Artifacts: every fenced code block you write is saved with a timestamp.
- Name files so they land in the shared task workspace with folder structure: put the path in the fence
  info string, e.g. ```python src/app.py (or a **src/app.py** line right before the fence).
- Re-sending a block with the same path saves a new version. To change part of a file, prefer a patch:
  a ```diff block with a unified diff (--- a/path, +++ b/path, @@ hunks), a SEARCH/REPLACE block
  (path line, <<<<<<< SEARCH, old, =======, new, >>>>>>> REPLACE), or the patch_file tool.
- Move or rename a file with move_file and delete a file or folder with delete_path (never git mv / mv / rm:
  the xqo commits the workspace itself, and shell commands only change a throwaway copy).
- Use list_files / read_file to see what exists. If a patch fails you will be told why; fix and retry.
- Replies have an output limit. For long or many files, use the write_file tool with one file per call
  instead of pasting several whole files into one reply; change existing files with patches.
- Refer to workspace files by path: never paste a file's content into a message to another agent, a note or
  a reply to the user. Name the paths in the 'files' field of ask_agent / handover_task; the receiver reads them.

Reports: write your final answer as a short report in Markdown (a title line, then sections such as
## Summary, ## What I did, ## Files, ## Open issues). It is saved under your name and shown to the user in the
office. Use the report tool for reports you want to give along the way."""
SDK_ARTIFACT_GUIDE = """Files: the task's shared workspace is your working directory. Create and change files with the
Write and Edit tools (one file per call; Edit for small changes); every change is saved as a timestamped version.
Move or rename a file with move_file and delete a file or folder with delete_path (never git mv / mv / rm:
the xqo commits the workspace itself, and shell commands only change a throwaway copy).
A fenced code block with a path in its info string (```python src/app.py) is also saved to the workspace, and a
```diff block is applied as a patch; prefer Write / Edit.
Refer to files by path: never paste a file's content into a message to another agent, a note or a reply to the
user. Name workspace paths in the 'files' field of ask_agent / handover_task and attachments by name in their
'attachments' field; the receiver reads them itself. Read a file only when you need its content.

Reports: write your final answer as a short report in Markdown (a title line, then sections such as
## Summary, ## What I did, ## Files, ## Open issues). It is saved under your name and shown to the user in the
office. Use the report tool for reports you want to give along the way."""
MAX_CONTINUES = 2  # times a reply cut off at max_tokens is continued before the user is asked
CONTINUE_NOTE = ("[xqo] Your previous reply was cut off at the output limit ({n} tokens). Continue exactly "
                 "where it stopped: no repetition, no preamble. If you were inside a code block, continue the code "
                 "and then close the fence. Tool calls in the cut-off reply were not run; send them again. "
                 "For long files, use write_file with one file per call.")
MAX_PATCH_RETRIES = 2
MAX_API_ASKS = 3  # times the user is asked to retry a failed API call before the task fails


def avatar_public(name: str, info: dict | None) -> dict | None:
    from .avatars import public
    return public(name, info)


def provider_info(spec: dict) -> dict:
    from .providers import describe
    return describe(spec)


def _is_api_error(e: Exception) -> bool:
    from .providers import ProviderError
    if isinstance(e, ProviderError):
        return True
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(e, anthropic.APIError)

WORD = re.compile(r"[a-z0-9+#]+")
SUFFIXES = ("ers", "ing", "er", "or", "s")


def stem(word: str) -> str:
    """Crude stem so role words meet skill words: coder~coding, tester~testing, researcher~research."""
    for suf in SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[:-len(suf)]
    return word


class Agent:
    def __init__(self, name: str, skills: list[str], system: str, llm, artifacts: ArtifactStore,
                 model: str = DEFAULT_MODEL, effort: str | None = "medium", max_turns: int = 12,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 max_concurrency: int = 4, can_run_code: bool = False, mcp_servers: list[dict] | None = None,
                 department: str = HQ, sandbox=None, write_paths: list[str] | None = None,
                 sdk_tools: list[str] | None = None):
        self.name, self.skills, self.system = name, [s.lower() for s in skills], system
        self.llm, self.artifacts, self.model, self.effort = llm, artifacts, model, effort
        self.max_turns = max_turns
        self.max_tokens, self.max_tokens_source = max_tokens, "default"
        self.cutoffs = {"cut": 0, "continued": 0, "gave_up": 0}  # replies that hit max_tokens
        self.can_run_code = can_run_code
        self.write_paths = write_paths  # workspace paths this agent may write (glob patterns); None = any
        self.mcp_servers = mcp_servers or []  # server configs this agent may use
        self.mcp_filters: dict[str, list[str] | None] = {s["name"]: None for s in self.mcp_servers}  # grants.py
        self.mcp_source = "agent"
        self.skill_packs: list = []  # skills.SkillPack objects granted to this agent
        self.skill_packs_source = "none"
        self.tool_policy = ToolPolicy()  # allow / deny patterns over tool names (grants.py)
        self.sdk_tools = list(SDK_TOOLS if sdk_tools is None else sdk_tools)  # Claude Code tools (claude-sdk)
        self.department = department
        self.sandbox = sandbox  # department sandbox override (None = the global one)
        self.active = 0
        self.waiting = 0  # tasks held by a pause
        self.model_source = "agent"  # where the model came from: agent | department | config | default
        self.served_model = None  # model that actually answered last (differs when a fallback served it)
        self.provider, self.provider_type, self.provider_source = "anthropic", "anthropic", "default"
        self.avatar: dict | None = None  # see avatars.py
        self.nickname: str | None = None  # display name only (branding.resolve_nickname); the id stays `name`
        self.hints: dict[str, str] = {}  # tool_use_id -> where the full output lives (for trimmed history)
        self.compacted = {"times": 0, "blocks": 0, "chars": 0}
        self.provider_spec: dict = {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"}
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        self._sem = asyncio.Semaphore(max_concurrency)
        self.bus: MessageBus | None = None

    def describe(self) -> dict:
        return {"name": self.name, "skills": self.skills, "model": self.model, "active": self.active,
                "model_source": self.model_source, "effort": self.effort, "served_model": self.served_model,
                "department": self.department, "head": self.is_head,
                "provider": self.provider, "provider_type": self.provider_type,
                "provider_source": self.provider_source, "provider_info": provider_info(self.provider_spec),
                "avatar": avatar_public(self.name, self.avatar), "nickname": self.nickname, "compacted": self.compacted,
                "max_tokens": self.max_tokens, "max_tokens_source": self.max_tokens_source, "cutoffs": self.cutoffs,
                "paused": self.waiting,
                "can_run_code": self.can_run_code, "write_paths": self.write_paths,
                "channels": getattr(self, "channels", None) or {}, "feeds": getattr(self, "feeds", None) or {},
                "sdk_tools": self.sdk_tools if self.provider_type == "claude-sdk" else None, "mcp": [s["name"] for s in self.mcp_servers],
                "mcp_filters": {k: v for k, v in self.mcp_filters.items() if v}, "mcp_source": self.mcp_source,
                "skill_packs": [p.name for p in self.skill_packs], "skill_packs_source": self.skill_packs_source,
                "tool_policy": self.tool_policy.describe(),
                "attention": sum(1 for a in self.bus.attention.pending() if a.agent == self.name) if self.bus else 0,
                "alerts": (self.bus.attention.by_agent().get(self.name) if self.bus else None) or None,
                "usage": self.usage}

    @property
    def is_head(self) -> bool:
        d = getattr(self.bus, "departments", {}).get(self.department) if self.bus else None
        return bool(d and d.head == self.name)

    @property
    def dept(self):
        return getattr(self.bus, "departments", {}).get(self.department) if self.bus else None

    def may_write(self, path: str) -> bool:
        """Whether write_paths lets this agent write a workspace file ('*' and '**' also match '/')."""
        if self.write_paths is None:
            return True
        p = normalize_path(path) or path
        return any(fnmatchcase(p, pat.replace("**", "*")) for pat in self.write_paths)

    def _refusal(self, denied: list[str]) -> str:
        return (f"{', '.join(denied)}: you may only write {', '.join(self.write_paths) or 'no files'}. Do not "
                "change code or other files yourself: hand fixes to the right department (for code, engineering) "
                "with handover_task, describing the problem and the change needed.")

    def _scope_root(self, task: Task, scope: str | None) -> str:
        if scope == "department":
            if self.department == HQ:
                raise HandoverError("headquarters has no department workspace; use the task workspace")
            return dept_root(task.root_id, self.department)
        return task.root_id

    def task_limits(self, task: Task | None) -> dict:
        """A scheduled run's own limits (scheduler.py), set on its top-level task; {} for other tasks."""
        if task is None:
            return {}
        root = self.bus.tasks.get(task.root_id) if getattr(self, "bus", None) else None
        return getattr(root or task, "limits", None) or {}

    def turn_limit(self, task: Task | None = None) -> float:
        """max_turns (a scheduled run's own one first), or no limit while the system-wide override is on
        (limits.py); scheduled runs ignore the override unless their limits say honor_no_limits."""
        own = self.task_limits(task)
        base = own.get("max_turns") or self.max_turns
        lim = getattr(self.bus, "limits", None)
        return lim.max_turns(base) if lim and own.get("honor_no_limits", True) else base

    def output_limit(self, task: Task | None = None) -> int:
        """max_tokens (a scheduled run's own one first), or UNLIMITED_MAX_TOKENS while the override is on."""
        own = self.task_limits(task)
        base = own.get("max_tokens") or self.max_tokens
        lim = getattr(self.bus, "limits", None)
        return lim.max_tokens(base) if lim and own.get("honor_no_limits", True) else base

    # ---- skill lookup ---------------------------------------------------
    async def consider_offer(self, skill: str, task_text: str) -> dict | None:
        want = skill.lower().strip()
        if want in self.skills or want == self.name.lower():  # models often name the role ('coder') as the skill
            score = 2
        elif {stem(w) for w in WORD.findall(want)} & {stem(w) for s in self.skills for w in WORD.findall(s)}:
            score = 1
        else:
            return None
        return {"agent": self.name, "skill": skill, "match": "exact" if score == 2 else "partial",
                "score": score, "load": self.active}

    # ---- main loop ------------------------------------------------------
    def _system(self, sdk: bool = False) -> str:
        guide = SDK_ARTIFACT_GUIDE if sdk else ARTIFACT_GUIDE
        depts = getattr(self.bus, "departments", {})
        if not depts:
            roster = "\n".join(f"- {a['name']}: {', '.join(a['skills'])}" for a in self.bus.roster())
            return (f"{self.system}\n\nYou are agent '{self.name}' with skills: {', '.join(self.skills)}.\n"
                    f"Other agents in the xqo:\n{roster}\n\n"
                    "Do work within your skills yourself. For work needing another skill, use handover_task "
                    "(skill broadcast) or ask_agent (direct). End with a concise final answer.\n\n"
                    + self._plugin_notes() + self._write_rule() + prompt_section(self.skill_packs) + guide)
        mine = [a for a in self.bus.agents.values() if a.department == self.department]
        lines = [f"- {a.name}{' (head)' if a.is_head else ''}: {', '.join(a.skills)}" for a in mine]
        others = []
        me = depts.get(self.department)
        for d in depts.values():
            if d.name == self.department or (me and not me.may_contact(d.name)):
                continue
            skills = sorted({s for n in d.members if n in self.bus.agents for s in self.bus.agents[n].skills})
            others.append(f"- {d.name} (head: {d.head}): {d.description} Skills: {', '.join(skills)}")
        where = ("headquarters (HQ): you can reach every department" if self.department == HQ else
                 f"the {self.department} department" + (" and you are its head: assign incoming work to your "
                 "members with handover_task, check their results, publish what others need, then report back"
                 if self.is_head else f" (head: {me.head})"))
        rules = ("handover_task searches your own department first, then asks the heads of the departments you may "
                 "contact; target one with department='name' or a skill like '@marketing copywriting'. "
                 "ask_agent works inside your department; to reach another department, ask its head.")
        if self.department != HQ:
            rules += (" You also have a private department workspace and notes board (scope='department'); "
                      "use publish_files to share department files with everyone.")
        return (f"{self.system}\n\nYou are agent '{self.name}' with skills: {', '.join(self.skills)}, in {where}.\n"
                f"Your {'headquarters' if self.department == HQ else 'department'}:\n" + "\n".join(lines)
                + ("\n\nOther departments you can reach:\n" + "\n".join(others) if others else "")
                + f"\n\n{rules} Do work within your skills yourself. End with a concise final answer.\n\n"
                + self._plugin_notes() + self._write_rule() + prompt_section(self.skill_packs) + guide)

    def _plugin_notes(self) -> str:
        """What active plugins add to the system prompt (e.g. the peers this agent may contact)."""
        plugins = getattr(self.bus, "plugins", None) if self.bus else None
        return plugins.prompt_notes(self) if plugins else ""

    def _write_rule(self) -> str:
        if self.write_paths is None:
            return ""
        return (f"You may write only these workspace files: {', '.join(self.write_paths) or 'none'}. Put plans, "
                "steps and notes there. Never write or patch code or other files yourself, not even a small fix: "
                "code changes, bug fixes and code or runtime errors go to the right department (for code, "
                "engineering) with handover_task.\n\n")

    async def handle(self, task: Task) -> str:
        async with self._sem:
            self.active += 1
            try:
                return await self._loop(task)
            finally:
                self.active -= 1

    async def _loop(self, task: Task) -> str:
        bus = self.bus
        intro = f"Task from {task.requester} ({task.name}):\n\n{task.prompt}"
        if task.context:
            intro += "\n\n" + task.context
        board = bus.memory.render(task.root_id) if getattr(bus, "memory", None) else ""
        if board:
            intro += "\n\n" + board
        if self.department != HQ and getattr(bus, "memory", None):
            dboard = bus.memory.render(dept_root(task.root_id, self.department))
            if dboard:
                intro += "\n\n" + dboard.replace("Shared notes board for this task",
                                                  f"{self.department} department notes board", 1)
        messages: list = [{"role": "user", "content": self._first_content(task, intro, self.file_refs())}]
        bus.chat.convos[task.id] = (self, messages)  # lets an urgent question be answered from here
        try:
            if getattr(self.llm, "is_sdk", False):
                from .sdk_backend import SdkStartError
                try:
                    return await self.sdk_runner(task).run(messages)
                except SdkStartError as e:
                    if not self._fall_back(task, e):
                        raise RuntimeError(str(e)) from None
                    messages[0]["content"] = self._first_content(task, intro, False)  # the API has no Read tool
            return await self._turns(task, messages)
        finally:
            bus.chat.convos.pop(task.id, None)

    def file_refs(self) -> bool:
        """Whether attachments reach this agent as file references it reads with Read (Claude Code agents)."""
        return bool(getattr(self.llm, "is_sdk", False)) and "Read" in self.sdk_tools

    def _first_content(self, task: Task, intro: str, refs: bool) -> str | list:
        store = getattr(self.bus, "attachments", None)
        if not task.attachments or not store:
            return intro
        forward = "To pass any of them on, list their names in the attachments field of ask_agent or handover_task."
        if refs:
            return intro + "\n\n" + store.refs(task.root_id, task.attachments) + "\n" + forward
        return store.blocks(task.root_id, task.attachments) + [{"type": "text", "text": (
            intro + f"\n\nAttached above: {describe(task.attachments)}. " + forward)}]

    def sdk_runner(self, task: Task):
        from .sdk_backend import SdkRunner
        return SdkRunner(self, task)

    def has_dept_workspace(self) -> bool:
        """A department member also has a department workspace, reached with the scope='department' tools."""
        return self.department != HQ and bool(getattr(self.bus, "departments", {}))

    def _fall_back(self, task: Task, err: Exception) -> bool:
        """claude-sdk picked only because it is installed, and Claude Code can't start or sign in: use the API."""
        if self.provider_spec.get("fallback") != "anthropic":
            return False
        import os
        if not os.getenv("ANTHROPIC_API_KEY"):  # no key: the API can't answer either, so keep Claude Code's own error
            return False                         # (e.g. a wrong or expired CLAUDE_CODE_OAUTH_TOKEN) instead of a
                                                 # misleading "No Anthropic credentials"
        from .llm import AnthropicLLM
        self.llm = AnthropicLLM(spec={"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"})
        self.provider, self.provider_type, self.provider_source = "anthropic", "anthropic", "fallback"
        self.provider_spec = {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"}
        self.bus.emit(Event("error", self.name, None, task.id, task.parent_id,
                            f"Claude Agent SDK unavailable, using the Anthropic API instead: {str(err)[:300]}",
                            {"fallback": "anthropic"}))
        return True

    async def _turns(self, task: Task, messages: list) -> str:
        bus = self.bus
        system = self._system()
        last_text = ""
        retries = 0
        patch_asks = 0
        refusals = 0  # times the agent was told that fenced writes outside write_paths were refused
        cut_text, cut_rounds = "", 0  # a reply cut off at max_tokens, collected across continuations
        turn = -1
        while (turn := turn + 1) < self.turn_limit(task):  # read each turn: the override can change mid-task
            await bus.chat.gate(task, self)  # paused: wait here, after any call in progress has finished
            for text in bus.chat.drain(task):  # Ask / Steer messages and notes join at this turn
                add_user_text(messages, text)
            self._compact(task, messages)
            resp = await self._call(task, system, messages)
            usage = getattr(resp, "usage", None)
            self.served_model = getattr(resp, "model", None) or self.model
            self.usage["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                self.usage[k] += getattr(usage, k, None) or 0
            if getattr(bus, "budget", None) and usage is not None:
                bus.budget.add(task.root_id, self.department, getattr(resp, "model", None) or self.model, usage)
            bus.emit(Event("llm", self.name, None, task.id, task.parent_id,
                           f"turn {turn + 1}: stop={resp.stop_reason}",
                           {"model": getattr(resp, "model", self.model),
                            "input_tokens": getattr(usage, "input_tokens", None),
                            "output_tokens": getattr(usage, "output_tokens", None),
                            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None)}))
            if resp.stop_reason == "refusal":
                raise RuntimeError(f"model refused: {getattr(resp, 'stop_details', None)}")
            if resp.stop_reason == "pause_turn":  # server-side tool (MCP connector) paused; resume
                messages.append({"role": "assistant", "content": resp.content})
                continue
            texts = [b.text for b in resp.content if b.type == "text" and b.text]
            if resp.stop_reason == "max_tokens":
                self.cutoffs["cut"] += 1
                cut_text += "\n".join(texts)
                if turn + 1 < self.turn_limit(task) and await self._may_continue(task, cut_rounds):
                    cut_rounds = cut_rounds + 1 if cut_rounds < MAX_CONTINUES else 1
                    self.cutoffs["continued"] += 1
                    kept = [b for b in resp.content if b.type == "text" and b.text] or \
                        [{"type": "text", "text": "(cut off before any text)"}]
                    messages.append({"role": "assistant", "content": kept})
                    messages.append({"role": "user", "content": CONTINUE_NOTE.format(n=self.output_limit(task))})
                    bus.emit(Event("llm", self.name, None, task.id, task.parent_id,
                                   f"reply cut off at max_tokens ({self.output_limit(task)}); asked to continue",
                                   {"cutoff": True, "round": cut_rounds}))
                    continue  # files are extracted once the reply is complete
                self.cutoffs["gave_up"] += 1
                texts, cut_text = ([cut_text] if cut_text else []), ""
            elif cut_text:  # the end of a continued reply: extract from the whole thing
                texts, cut_text, cut_rounds = [cut_text + "\n".join(texts)], "", 0
            patch_errors, blocked = [], []
            allow = self.may_write if self.write_paths is not None else None
            for t in texts:
                recs, errs = self.artifacts.process_text(t, task.id, self.name, task.root_id, allow=allow)
                patch_errors += errs
                for rec in recs:
                    blocked += rec.get("blocked", [])
                    self._emit_artifact(task, rec)
            refused = ("Fenced blocks NOT written: " + self._refusal(list(dict.fromkeys(blocked)))) if blocked else ""
            if texts:
                last_text = "\n".join(texts)
            messages.append({"role": "assistant", "content": resp.content})  # keep full content (thinking)
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls and refused and refusals < 2 and turn + 1 < self.turn_limit(task):
                refusals += 1
                messages.append({"role": "user", "content": refused})
                continue
            if not calls and patch_errors and retries < MAX_PATCH_RETRIES and turn + 1 < self.turn_limit(task):
                retries += 1  # tell the model its fenced patch failed so it can fix it
                messages.append({"role": "user", "content": "Some artifact patches failed and were NOT applied:\n- "
                                 + "\n- ".join(patch_errors)
                                 + "\nUse read_file to see the current content, then send a corrected patch "
                                   "or the full file."})
                continue
            if not calls and patch_errors and patch_asks < 2 and turn + 1 < self.turn_limit(task):
                patch_asks += 1  # still failing after retries: flag the user
                answer, auto = await bus.attention.request(
                    task, self.name, "patch_failed",
                    f"{self.name}'s patches still fail after {MAX_PATCH_RETRIES} retries:\n- "
                    + "\n- ".join(patch_errors) + "\nReply with guidance for the agent, or 'ignore'.",
                    default="ignore", options=["ignore"])
                if not auto and answer.strip().lower() != "ignore":
                    retries = 0
                    messages.append({"role": "user", "content": f"Guidance from the user about the failed patches: {answer}"})
                    continue
            if resp.stop_reason != "tool_use" or not calls:
                if resp.stop_reason == "max_tokens":
                    last_text += (f"\n\n[truncated: max_tokens ({self.output_limit(task)}) reached"
                                  + (" after continuing" if cut_rounds else "") + "]")
                if bus.chat.has_mail(task) and turn + 1 < self.turn_limit(task):
                    continue  # a message arrived during the last call: let the agent see it before finishing
                return last_text
            results = list(await asyncio.gather(*(self._tool(task, c) for c in calls)))
            if patch_errors:
                results.append({"type": "text", "text": "Fenced patches that failed:\n- " + "\n- ".join(patch_errors)})
            if refused:
                results.append({"type": "text", "text": refused})
            messages.append({"role": "user", "content": results})
        return last_text + "\n\n[stopped: max turns reached]"

    async def _may_continue(self, task: Task, rounds: int) -> bool:
        """Continue a cut-off reply automatically up to MAX_CONTINUES times, then ask the user."""
        if rounds < MAX_CONTINUES:
            return True
        answer, _ = await self.bus.attention.request(
            task, self.name, "output_limit",
            f"{self.name}'s reply hit the output limit ({self.output_limit(task)} tokens) {rounds + 1} times in a row. "
            "Let it continue, or stop here and keep what it wrote (an unfinished code block is kept as a snippet, "
            "not written to the workspace)? Raise max_tokens for this agent in agents.json if this happens often.",
            default="stop", options=["continue", "stop"])
        return answer.strip().lower() in ("continue", "yes", "y", "ok")

    def _compact(self, task: Task, messages: list) -> None:
        """Trim old tool results / large tool inputs before a call (see compaction.py)."""
        cfg = getattr(self.bus, "compaction", None)
        if not cfg:
            return
        n, saved = compact(messages, cfg, self.hints)
        if n:
            c = self.compacted
            c["times"] += 1
            c["blocks"] += n
            c["chars"] += saved
            self.bus.emit(Event("llm", self.name, None, task.id, task.parent_id,
                                f"trimmed {n} old tool block(s) from the history, ~{saved // 4} tokens saved",
                                {"compacted": n, "saved_chars": saved}))

    def code_sandbox(self):
        """The sandbox this agent's commands run in (its department's, else the global one), or None."""
        sandbox = self.sandbox or getattr(self.bus, "sandbox", None)
        return sandbox if self.can_run_code and sandbox and sandbox.backend != "off" else None

    async def run_code(self, task: Task, command: str, timeout: float | None = None, call_id: str | None = None,
                       scope: str | None = None, network: bool | None = None) -> str:
        """Run a command in the sandbox on a copy of a workspace; returns the formatted result and verdict."""
        network = netgrants.granted(self.bus, task) if network is None else network  # True: approved now
        sandbox = self.sandbox or self.bus.sandbox
        self.bus.emit(Event("sandbox", self.name, None, task.id, task.parent_id,
                            f"$ {command}", {"phase": "start", "backend": sandbox.backend}))
        scope_root = self._scope_root(task, scope)  # each workspace gets its own venv
        try:
            r = await self.bus.chat.track_run(task, sandbox.run(
                self.artifacts.workspace(scope_root), command, timeout,
                env_dir=self.artifacts.env_dir(scope_root), extra=self.skill_folders(), **({"network": True} if network else {})))
        except RunStopped:
            self.bus.emit(Event("sandbox", self.name, None, task.id, task.parent_id,
                                f"stopped (task paused): $ {command}", {"phase": "stopped"}))
            raise
        out = format_result(r)
        rec = self.artifacts.save(out, "text", task.id, self.name, task.root_id, action="run")
        self.bus.emit(Event("sandbox", self.name, None, task.id, task.parent_id,
                            f"exit {r['exit_code']}{' (timed out)' if r['timed_out'] else ''}: $ {r['command']}",
                            {"phase": "end", **{k: r[k] for k in ("exit_code", "timed_out", "seconds", "backend")},
                             "log": rec["path"]}))
        self._emit_artifact(task, rec)
        if call_id:
            self.hints[call_id] = (f"the full log is saved as run artifact {rec['path']}; "
                                   f"run `{command[:120]}` again for fresh output")
            if len(self.hints) > 500:
                self.hints.pop(next(iter(self.hints)))
        return out + await self._test_verdict(task, r)

    def tools(self) -> tuple[list[dict], list[dict]]:
        """Return (tools, mcp_servers) for this agent's API call."""
        tools = list(TOOLS)
        if self.department != HQ and getattr(self.bus, "departments", {}):
            tools.append(PUBLISH_TOOL)
        if self.code_sandbox() is not None:
            tools.append(RUN_CODE_TOOL)
            if (getattr(self, 'sandbox', None) or self.bus.sandbox).network_requestable:
                tools.append(netgrants.REQUEST_NETWORK_TOOL)
        if self.skill_packs:
            tools += SKILL_TOOLS
        hub = getattr(self.bus, "mcp", None)
        if hub:
            tools += hub.tool_defs(self.mcp_filters)
        plugins = getattr(self.bus, "plugins", None)
        if plugins:  # tools of active plugins (peering, channels, feeds, …)
            tools += plugins.tool_defs(self)
        tools = [t for t in tools if self.tool_policy.allows(t["name"])]
        if self.provider_type != "anthropic":  # the MCP connector is an Anthropic API feature
            return tools, []
        servers, toolsets = remote_params(self.remote_servers(), self.mcp_filters, self.remote_denied())
        return tools + toolsets, servers

    def remote_servers(self) -> list[dict]:
        """Granted remote MCP servers that the tool policy doesn't rule out."""
        return [s for s in self.mcp_servers if "url" in s and self.tool_policy.allows_server(s["name"])]

    def remote_denied(self) -> dict[str, set[str]]:
        """Remote tools the policy denies by exact name (mcp__<server>__<tool>)."""
        out: dict[str, set[str]] = {}
        for p in self.tool_policy.deny:
            for s in self.mcp_servers:
                pre = f"mcp__{s['name']}__"
                if "url" in s and p.startswith(pre) and not any(c in p for c in "*?["):
                    out.setdefault(s["name"], set()).add(p[len(pre):])
        return out

    def may_use_mcp(self, api: str) -> bool:
        """Whether a local MCP tool (mcp__<server>__<tool>) is granted to this agent."""
        hub = getattr(self.bus, "mcp", None)
        if not hub or api not in hub.tools:
            return False
        server, tool, _ = hub.tools[api]
        return server in self.mcp_filters and tool_allowed(self.mcp_filters[server], tool)

    def skill(self, name: str):
        pack = next((p for p in self.skill_packs if p.name == name), None)
        if not pack:
            raise HandoverError(f"no skill pack {name!r}; yours: {', '.join(p.name for p in self.skill_packs) or 'none'}")
        return pack

    def skill_folders(self) -> dict[str, Path]:
        return {p.name: p.path for p in self.skill_packs}

    async def _call(self, task: Task, system: str, messages: list):
        """One model call; when the SDK's own retries are exhausted, ask the user whether to retry."""
        asks = 0
        tools, servers = self.tools()
        plugins = getattr(self.bus, "plugins", None)
        if plugins:  # e.g. a task that answers the public, or one a feed rule started: their tools only
            tools, servers = plugins.filter_tools(task, self, tools, servers)
        if getattr(self.bus, "budget", None):
            await self.bus.budget.check(task, self.name, self.department)
        while True:
            try:
                return await self.llm.create(model=self.model, system=system, messages=messages,
                                             tools=tools, effort=self.effort, agent=self.name,
                                             max_tokens=self.output_limit(task),
                                             mcp_servers=servers or None)
            except Exception as e:
                if not _is_api_error(e) or asks >= MAX_API_ASKS:
                    raise
                asks += 1
                answer, _ = await self.bus.attention.request(
                    task, self.name, "retries_exhausted",
                    f"API call for {self.name} failed after automatic retries: {type(e).__name__}: {str(e)[:300]}. "
                    f"Retry ({asks}/{MAX_API_ASKS}) or stop?", default="retry", options=["retry", "stop"])
                if answer.strip().lower() not in ("retry", "yes", "y", "ok"):
                    raise

    async def _test_verdict(self, task: Task, r: dict) -> str:
        """Track pass/fail; after repeated failures ask the user whether to keep fixing."""
        from .bus import MAX_TEST_FAILS
        s = self.bus.record_test(task, r["exit_code"] if not r["timed_out"] else -1)
        if s["last_exit"] == 0:
            return "\n\n[PASSED]"
        msg = (f"\n\n[FAILED: exit {s['last_exit']}, {s['fails_in_row']} failed run(s) in a row] Find the cause, "
               "fix the code (patch_file) or hand the exact failures back to the author, then run the tests again.")
        if s["fails_in_row"] < MAX_TEST_FAILS:
            return msg
        s["fails_in_row"] = 0
        tail = (r.get("stdout", "") + "\n" + r.get("stderr", "")).strip()[-600:]
        answer, auto = await self.bus.attention.request(
            task, self.name, "tests_failing",
            f"Tests still fail after {MAX_TEST_FAILS} runs in a row ({task.name}). Last output:\n{tail}\n\n"
            "Keep fixing, stop and report the failures, or reply with guidance.",
            default="stop", options=["keep fixing", "stop"])
        a = answer.strip().lower()
        if a in ("stop", ""):
            return ("\n\n[FAILED] The user asked to stop fixing: do not run the tests again. Finish now and report "
                    "the failing tests clearly in your final answer.")
        if a in ("keep fixing", "continue", "keep", "yes"):
            return msg
        return msg + f"\nGuidance from the user: {answer}"

    def _emit_artifact(self, task: Task, rec: dict) -> None:
        label = f"{rec.get('action', 'snippet')}: {rec.get('file') or rec.get('path')}"
        if rec.get("error"):
            label += f" ({rec['error'][:200]})"
        self.bus.emit(Event("artifact", self.name, None, task.id, task.parent_id, label, rec))

    async def _tool(self, task: Task, call) -> dict:
        inp = call.input if isinstance(call.input, dict) else json.loads(call.input or "{}")
        plugins = getattr(self.bus, "plugins", None)
        denied = plugins.denial(task, self, call.name) if plugins else None  # plugin gates fail closed
        if denied:
            return {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": denied}
        if not self.tool_policy.allows(call.name):
            return {"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                    "content": f"{call.name} is not allowed for {self.name} (tool policy in agents.json)"}
        try:
            if call.name == "list_agents":
                out = json.dumps(self.bus.roster())
            elif call.name == "reply_user":
                self.bus.chat.reply(task, self.name, inp["message"])
                out = "sent to the user"
            elif call.name == "ask_agent":
                out = await self.bus.direct(task, inp["agent"], inp["message"], inp.get("title", ""),
                                            inp.get("attachments"), inp.get("files"))
            elif call.name == "handover_task":
                out = await self.bus.handover(task, inp["skill"], inp["task"], inp.get("title", ""),
                                              inp.get("attachments"), inp.get("department"), inp.get("files"))
            elif call.name == "ask_user":
                answer, auto = await self.bus.attention.request(
                    task, self.name, inp.get("reason", "question"), inp["question"],
                    default=inp.get("default", ""), options=inp.get("options") or [])
                out = (f"No reply within {int(self.bus.attention.timeout)}s; auto-confirmed default: {answer}"
                       if auto else f"User replied: {answer}")
            elif call.name == "note":
                board = self._scope_root(task, inp.get("scope"))
                entry = self.bus.memory.set(board, inp["key"], inp["value"], self.name)
                scope = "department" if board != task.root_id else "task"
                self.bus.emit(Event("memory", self.name, None, task.id, task.parent_id,
                                    f"{'[' + self.department + '] ' if scope == 'department' else ''}"
                                    f"{entry['op']} {entry['key']}: {entry['value'][:200]}",
                                    {**entry, "scope": scope}))
                out = f"{entry['op']} note {entry['key']!r}"
            elif call.name == "report":
                rec = self.artifacts.save_report(task.root_id, task.id, self.name, str(inp.get("title") or "Report"),
                                                 str(inp.get("markdown") or ""))
                self._emit_artifact(task, rec)
                out = f"Report saved: {rec['path']}"
            elif call.name == "read_notes":
                out = self.bus.memory.render(self._scope_root(task, inp.get("scope"))) or "(the board is empty)"
            elif call.name in ("run_code", "request_network"):
                if not self.can_run_code:
                    raise HandoverError(f"{self.name} may not run code; hand over to an agent with skill 'testing'")
                network = None  # run_code: the task's grant decides
                if call.name == "request_network":
                    sandbox = getattr(self, "sandbox", None) or self.bus.sandbox
                    if not sandbox.network_requestable:
                        raise HandoverError("network requests are off here (runs already have network, or "
                                            "sandbox.network_requests is false)")
                    pick, auto = await netgrants.ask(self, task, inp)
                    if pick == "deny":
                        return {"type": "tool_result", "tool_use_id": call.id, "content": netgrants.denied_text(auto),
                                "is_error": True}
                    network = True
                try:
                    out = await self.run_code(task, inp["command"], inp.get("timeout"), call.id, inp.get("scope"),
                                              network=network)
                except RunStopped as e:
                    return {"type": "tool_result", "tool_use_id": call.id, "content": str(e), "is_error": True}
            elif call.name == "load_skill":
                pack = self.skill(inp["name"])
                files = pack.files()
                self.bus.emit(Event("skill", self.name, None, task.id, task.parent_id, f"loaded skill {pack.name}",
                                    {"skill": pack.name}))
                out = (f"# Skill: {pack.name}\n\n{pack.body()}"
                       + (f"\n\nFiles (read_skill_file; scripts run with run_code from {SKILLS_RUN_DIR}/{pack.name}/):\n"
                          + "\n".join(f"- {f}" for f in files) if files else ""))
            elif call.name == "read_skill_file":
                out = self.skill(inp["name"]).read(inp["path"])
            elif call.name.startswith(MCP_PREFIX) and self.may_use_mcp(call.name):
                text, is_err = await self.bus.mcp.call(call.name, inp)
                self.bus.emit(Event("mcp", self.name, call.name.split("__")[1], task.id, task.parent_id,
                                    f"{call.name}({json.dumps(inp)[:200]}) -> {text[:200]}", {"error": is_err}))
                return {"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_err}
            elif call.name == "list_files":
                out = json.dumps(self.artifacts.list_files(self._scope_root(task, inp.get("scope"))))
            elif call.name == "read_file":
                out = self.artifacts.read_file(self._scope_root(task, inp.get("scope")), inp["path"])
            elif call.name in ("write_file", "patch_file"):
                root = self._scope_root(task, inp.get("scope"))
                paths = diff_targets(inp["diff"], inp["path"]) if call.name == "patch_file" and inp.get("diff") \
                    else [inp["path"]]
                denied = [p for p in paths if not self.may_write(p)]
                if denied:
                    raise HandoverError("not written: " + self._refusal(denied))
                if call.name == "write_file":
                    recs = [self.artifacts.write_file(root, inp["path"], inp["content"], task.id,
                                                      self.name, source="tool")]
                else:
                    recs = self.artifacts.patch_file(root, inp["path"], task.id, self.name,
                                                     edits=inp.get("edits"), diff=inp.get("diff"))
                for rec in recs:
                    self._emit_artifact(task, rec)
                out = "; ".join(f"{r['action']} {r['file']} (version {r['timestamp']})" for r in recs)
            elif call.name == "delete_path":
                root = self._scope_root(task, inp.get("scope"))
                if not self.may_write(inp["path"]):
                    raise HandoverError("not deleted: " + self._refusal([inp["path"]]))
                rec = self.artifacts.delete_path(root, inp["path"], task.id, self.name)
                self._emit_artifact(task, rec)
                out = (f"deleted folder {rec['file']}/ ({rec['files']} files)" if rec.get("folder")
                       else f"deleted {rec['file']}") + f" (version {rec['timestamp']})"
            elif call.name == "move_file":
                root = self._scope_root(task, inp.get("scope"))
                denied = [p for p in (inp["src"], inp["dst"]) if not self.may_write(p)]
                if denied:
                    raise HandoverError("not moved: " + self._refusal(denied))
                recs = self.artifacts.move_file(root, inp["src"], inp["dst"], task.id, self.name)
                for rec in recs:
                    self._emit_artifact(task, rec)
                out = f"moved {recs[1]['file']} -> {recs[0]['file']} (version {recs[0]['timestamp']})"
            elif call.name == "publish_files":
                src = self._scope_root(task, "department")
                files = inp.get("files") or []
                names = [f["file"] for f in self.artifacts.list_files(src)] if files == ["*"] else files
                if not names:
                    raise HandoverError("nothing to publish: the department workspace is empty")
                recs = [self.artifacts.write_file(task.root_id, n, self.artifacts.read_file(src, n), task.id,
                                                  self.name, action="publish", source=f"department:{self.department}")
                        for n in names]
                for rec in recs:
                    self._emit_artifact(task, rec)
                out = f"published {len(recs)} file(s) to the task workspace: {', '.join(r['file'] for r in recs)}"
            elif plugins and plugins.owner(call.name):  # a tool of an active plugin
                out = await plugins.call_tool(task, self, call.name, inp)
            else:
                raise HandoverError(f"unknown tool {call.name}")
            return {"type": "tool_result", "tool_use_id": call.id, "content": out or "(empty)"}
        except BudgetExceeded:
            raise  # stops the agent, not just this tool call
        except (HandoverError, PatchError, KeyError, TypeError, ValueError, RuntimeError, OSError,
                asyncio.TimeoutError) as e:
            return {"type": "tool_result", "tool_use_id": call.id, "content": str(e), "is_error": True}
