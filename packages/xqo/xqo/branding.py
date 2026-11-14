"""The HQ's display name: "name" in agents.json, shown in the dashboard (and office plugin) headers."""
from __future__ import annotations

import html
import re
from pathlib import Path

PLACEHOLDER = "{{HQ_NAME}}"  # the literal title in the shipped HTML pages, replaced when a page is served
DEFAULT_TITLE = "xqo"


def hq_name(v) -> str:
    """ "name" in agents.json (free text, one line)."""
    if v is None:
        return ""
    if not isinstance(v, str) or len(v.strip()) > 80 or re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError("name: one line of text, at most 80 characters")
    return v.strip()


def resolve_nickname(v, who: str) -> str | None:
    """A display name for an agent or the user node ("nickname"): one line, at most 40 characters. The id
    (the agent's "name", "user") stays what events, tools and the CLI use; the nickname is only shown."""
    if v is None or v == "":
        return None
    if not isinstance(v, str) or re.search(r"[\x00-\x1f\x7f]", v) or not v.strip() or len(v.strip()) > 40:
        raise ValueError(f"{who}: nickname must be one line of text, at most 40 characters")
    return v.strip()


def title(bus) -> str:
    return getattr(bus, "hq_name", "") or DEFAULT_TITLE


def named_page(path: Path, bus) -> str:
    """An HTML page with the HQ's name in its <title> and header."""
    return path.read_text(encoding="utf-8").replace(PLACEHOLDER, html.escape(title(bus)))
