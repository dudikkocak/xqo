"""Git history for task workspaces: one repo per root task, one commit per artifact version.

Configured under "git" in agents.json:
    {"enabled": true, "branch": "main", "remote": "", "push_token_env": "XQO_GIT_TOKEN",
     "author_domain": "agents.local"}
Push only happens on request (dashboard / API). The token is read from the environment at push
time, sent as an HTTP header, and never written to the repo config, logs or events.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
SEP = "\x1f"


class GitError(RuntimeError):
    pass


@dataclass
class GitConfig:
    enabled: bool = True
    branch: str = "main"
    remote: str = ""
    push_token_env: str = "XQO_GIT_TOKEN"
    author_domain: str = "agents.local"

    @classmethod
    def from_dict(cls, d: dict | None) -> "GitConfig":
        d = dict(d or {})
        if "XQO_GIT" in os.environ:
            d["enabled"] = os.environ["XQO_GIT"] not in ("0", "false", "off")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def git_available() -> bool:
    return shutil.which("git") is not None


class GitRepo:
    """Thin, synchronous wrapper around the git CLI (calls are short and run under the store lock)."""

    def __init__(self, config: GitConfig | None = None):
        self.config = config or GitConfig()
        self.enabled = self.config.enabled and git_available()
        self._lock = threading.RLock()

    def _env(self, author: str = "xqo", date: str | None = None) -> dict:
        email = f"{re.sub(r'[^A-Za-z0-9._-]+', '_', author)}@{self.config.author_domain}"
        env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email,
               "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        return env

    def _git(self, ws: Path, *args: str, env: dict | None = None, timeout: float = 30,
             secret: str | None = None) -> str:
        argv = ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args]
        r = subprocess.run(argv, cwd=ws, env=env or self._env(), capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            msg = (r.stderr or r.stdout).strip()
            if secret:
                msg = msg.replace(secret, "***")
            raise GitError(msg[-2000:] or f"git {args[0]} failed")
        return r.stdout

    def is_repo(self, ws: Path) -> bool:
        return (ws / ".git").is_dir()

    def init(self, ws: Path) -> None:
        with self._lock:
            ws.mkdir(parents=True, exist_ok=True)
            if not self.is_repo(ws):
                self._git(ws, "init", "-q", "-b", self.config.branch)
            _exclude_deps(ws)

    def commit(self, ws: Path, paths: list[str], message: str, author: str, date: str | None = None,
               allow_empty: bool = False) -> str | None:
        """Stage `paths` (added, changed or deleted) and commit. Returns the sha, or None if nothing changed
        (with `allow_empty`, a marker commit is made anyway, e.g. to record that a task was cancelled)."""
        if not self.enabled:
            return None
        with self._lock:
            self.init(ws)
            env = self._env(author, date)
            self._git(ws, "add", "-A", "--", *paths, env=env)
            if not self._git(ws, "status", "--porcelain", env=env).strip() and not allow_empty:
                return None
            self._git(ws, "commit", "-q", "--allow-empty", "-m", message, env=env)
            return self._git(ws, "rev-parse", "HEAD", env=env).strip()

    def log(self, ws: Path, limit: int = 500) -> list[dict]:
        if not (self.enabled and self.is_repo(ws)):
            return []
        try:
            out = self._git(ws, "log", f"-n{limit}", "--name-status",
                            f"--format=%x1e%H{SEP}%an{SEP}%aI{SEP}%s")
        except GitError:  # empty repo
            return []
        rows = []
        for chunk in out.split("\x1e")[1:]:
            head, *rest = chunk.strip("\n").split("\n")
            sha, author, date, subject = head.split(SEP, 3)
            files = [{"status": l.split("\t")[0], "file": l.split("\t")[-1]} for l in rest if "\t" in l]
            rows.append({"sha": sha, "author": author, "date": date, "message": subject, "files": files})
        return rows

    def show(self, ws: Path, sha: str, path: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise GitError("bad commit id")
        return self._git(ws, "show", f"{sha}:{path}")

    def push(self, ws: Path, remote_url: str | None = None, branch: str | None = None) -> dict:
        """Push the workspace history. Credentials come from the env var named by push_token_env."""
        if not (self.enabled and self.is_repo(ws)):
            raise GitError("this task has no git history")
        url = (remote_url or self.config.remote or "").strip()
        if not re.match(r"^(https://|ssh://|git@|file://|/)", url):
            raise GitError("set a remote URL (https://, ssh://, git@ or a local path)")
        if "@" in url.split("://", 1)[-1].split("/", 1)[0] and url.startswith("https://"):
            raise GitError("don't put credentials in the URL; set the token env var instead")
        branch = branch or self.config.branch
        if not SAFE_REF.match(branch) or ".." in branch:
            raise GitError("invalid branch name")
        token = os.getenv(self.config.push_token_env or "") or None
        args = []
        if token and url.startswith("https://"):
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            args = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
            secret = basic
        else:
            secret = token
        with self._lock:
            out = self._git(ws, *args, "push", "--porcelain", url, f"HEAD:refs/heads/{branch}",
                            timeout=120, secret=secret)
            head = self._git(ws, "rev-parse", "HEAD").strip()
        return {"remote": url, "branch": branch, "head": head, "output": out.strip()[-1000:]}


# Dependency and cache folders never go into workspace commits (.git/info/exclude, so no file appears in the
# workspace). They stay on disk, so offline sandbox runs still find an installed node_modules.
EXCLUDE_MARK = "# xqo: dependency and cache folders"
EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/"]


def _exclude_deps(ws: Path) -> None:
    f = ws / ".git" / "info" / "exclude"
    try:
        old = f.read_text(encoding="utf-8") if f.is_file() else ""
        if EXCLUDE_MARK in old:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(old.rstrip("\n") + ("\n" if old.strip() else "") + EXCLUDE_MARK + "\n" + "\n".join(EXCLUDES) + "\n",
                     encoding="utf-8")
    except OSError:
        pass
