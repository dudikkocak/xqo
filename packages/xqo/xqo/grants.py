"""Who gets which MCP servers, skill packs and tools.

Every grant is looked up like "model": the agent's own field, else its department's "default_*", else the
"default_*" in agents.json, else nothing. A list replaces what it inherits; entries written "+name" are added
to it instead (a list of only "+" entries keeps the inherited ones). "all" means everything the agent can see.

    "default_mcp": ["docs"]                          every agent gets the docs server
    department "default_mcp": ["+github"]            its members get docs and github
    agent "mcp": ["fs"]                              this agent gets fs only
    agent "mcp": {"github": ["search_*"], "+fs": "*"}   tool filters per server (fnmatch on the server's tool names)

Tool policy ("tools" / "default_tools"): {"allow": [patterns], "deny": [patterns]}, fnmatch over the final tool
names (handover_task, run_code, mcp__github__create_issue, Bash, ...). Each list inherits on its own with the
same "+" rule. A tool is offered when it matches "allow" (no "allow" = every tool) and no "deny" pattern.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

ALL = "all"


def _entries(value, field: str, where: str) -> dict[str, list[str] | None]:
    """A list, "all" or {name: patterns} as {name: patterns or None}; keeps any "+" prefixes."""
    if isinstance(value, str):
        if value != ALL:
            raise ValueError(f"{where}: {field} must be a list, an object or \"all\"")
        return {ALL: None}
    if isinstance(value, list):
        if not all(isinstance(x, str) and x.strip("+") for x in value):
            raise ValueError(f"{where}: {field} entries must be names")
        return {x: None for x in value}
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str) or not k.strip("+"):
                raise ValueError(f"{where}: {field} keys must be names")
            if v in (None, "*"):
                out[k] = None
            elif isinstance(v, list) and v and all(isinstance(p, str) and p for p in v):
                out[k] = list(v)
            else:
                raise ValueError(f"{where}: {field}.{k} must be \"*\" or a list of tool names / patterns")
        return out
    raise ValueError(f"{where}: {field} must be a list, an object or \"all\"")


def _apply(inherited: dict, value, field: str, where: str) -> dict:
    """One level of inheritance: plain entries replace, "+" entries add."""
    entries = _entries(value, field, where)
    plain = {k: v for k, v in entries.items() if not k.startswith("+")}
    added = {k[1:]: v for k, v in entries.items() if k.startswith("+")}
    base = dict(inherited) if added and not plain else dict(plain)  # [] clears what it inherits
    base.update(added)
    return base


def resolve(field: str, default_field: str, agent: dict, dept_cfg: dict | None, cfg: dict,
            where: str) -> tuple[dict[str, list[str] | None], str]:
    """({name: tool patterns or None}, source) for one grant; source is agent | department | config | none."""
    out: dict = {}
    source = "none"
    for level, d, key in (("config", cfg, default_field), ("department", dept_cfg or {}, default_field),
                          ("agent", agent, field)):
        if key in d and d[key] is not None:
            out = _apply(out, d[key], key, where if level == "agent" else f"{where} ({level} default)")
            source = level
    return out, source


def expand(granted: dict[str, list[str] | None], available: dict[str, dict], what: str,
           where: str, strict: bool = True) -> dict[str, list[str] | None]:
    """Replace "all" by every available name and check the names exist."""
    out: dict = {}
    if ALL in granted:
        out = {n: None for n in available}
    for n, pats in granted.items():
        if n == ALL:
            continue
        if n not in available:
            if strict:
                raise ValueError(f"{where}: unknown {what} {n!r}"
                                 + (f"; known: {', '.join(sorted(available))}" if available else ""))
            continue
        out[n] = pats
    return out


class ToolPolicy:
    """allow / deny patterns over tool names; see the module docstring."""

    def __init__(self, allow: list[str] | None = None, deny: list[str] | None = None,
                 source: str = "none"):
        self.allow, self.deny, self.source = allow, list(deny or []), source

    @classmethod
    def resolve(cls, agent: dict, dept_cfg: dict | None, cfg: dict, where: str) -> "ToolPolicy":
        lists: dict[str, list[str] | None] = {"allow": None, "deny": None}
        source = "none"
        for level, d, key in (("config", cfg, "default_tools"), ("department", dept_cfg or {}, "default_tools"),
                              ("agent", agent, "tools")):
            v = d.get(key)
            if v is None:
                continue
            if not isinstance(v, dict) or set(v) - {"allow", "deny"}:
                raise ValueError(f"{where}: {key} must be {{\"allow\": [...], \"deny\": [...]}}")
            for k in ("allow", "deny"):
                if k in v:
                    pats = v[k]
                    if not isinstance(pats, list) or not all(isinstance(p, str) and p.strip("+") for p in pats):
                        raise ValueError(f"{where}: {key}.{k} must be a list of tool names / patterns")
                    plain = [p for p in pats if not p.startswith("+")]
                    added = [p[1:] for p in pats if p.startswith("+")]
                    lists[k] = (list(lists[k] or []) if added and not plain else plain) + added
            source = level
        return cls(lists["allow"], lists["deny"], source)

    def allows(self, name: str) -> bool:
        if self.allow is not None and not any(fnmatchcase(name, p) for p in self.allow):
            return False
        return not any(fnmatchcase(name, p) for p in self.deny)

    def allows_server(self, server: str) -> bool:
        """Whether a remote MCP server may be offered at all (its tools are not known in advance)."""
        probe = f"mcp__{server}__*"  # a pattern covering the whole server matches this literal name
        if self.allow is not None and not any(fnmatchcase(probe, p) or p.startswith(f"mcp__{server}__")
                                              for p in self.allow):
            return False
        return not any(fnmatchcase(probe, p) for p in self.deny)

    def describe(self) -> dict:
        return {"allow": self.allow, "deny": self.deny, "source": self.source}


def tool_allowed(patterns: list[str] | None, tool: str) -> bool:
    """A per-agent MCP tool filter (None = every tool of the server)."""
    return patterns is None or any(fnmatchcase(tool, p) for p in patterns)
