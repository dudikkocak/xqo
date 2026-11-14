from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import attachments as attachment_limits
from .attachments import validate
from .avatars import public as avatar_public
from .bus import Draining, MessageBus
from .folders import FolderConfig
from .branding import named_page
from .gitstore import GitError
from .models import Event

STATIC = Path(__file__).parent / "static"


class NoteIn(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=4000)


class RollbackIn(BaseModel):
    file: str
    version: str  # history record path


class AnswerIn(BaseModel):
    answer: str = Field(max_length=20_000)


class AckIn(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=5000)  # none: every alert matching agent / root_id
    agent: str | None = None
    root_id: str | None = None


class ForkIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    prompt: str | None = Field(default=None, max_length=100_000)
    agent: str | None = None


class PushIn(BaseModel):
    remote_url: str | None = Field(default=None, max_length=500)
    branch: str | None = Field(default=None, max_length=100)


class AttachmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    media_type: str | None = Field(default=None, max_length=100)
    data: str  # base64; size checked against the configured attachment limits


class ReloadIn(BaseModel):
    departments: str | None = Field(default=None, max_length=1000)  # "eng,marketing", "all", "none"; omit = keep


class CancelIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class DeleteIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    permanent: bool = False


class ChatIn(BaseModel):
    text: str = Field(default="", max_length=100_000)
    kind: str = Field(default="ask", pattern="^(ask|steer)$")
    to: str | None = Field(default=None, max_length=100)  # agent name; omit or "all" = the task's entry agent
    urgent: bool = False
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=1000)  # count: limits.attachments_max_files


class LimitsIn(BaseModel):
    enabled: bool = True  # true: limits off for a fresh hour (also when already off); false: limits back on now


class PauseIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class ArchiveIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    archived: bool = True


class BackupIn(BaseModel):
    scope: str = Field(default="full", pattern="^(full|state|config)$")
    keep: int | None = Field(default=None, ge=1, le=1000)


class DrainIn(BaseModel):
    on: bool = True  # false: take new work again


class TaskIn(BaseModel):
    prompt: str = Field(default="", max_length=100_000)
    agent: str | None = None
    urgent: bool = False  # with pause_task: pause that task first
    pause_task: str | None = None
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=1000)  # count: limits.attachments_max_files


def decode_attachments(items: list[AttachmentIn]) -> list[dict]:
    """Base64 attachments from a request body, checked against the configured limits (also used by plugins)."""
    cap = max(attachment_limits.MAX_BYTES.values()) * 4 // 3 + 8
    if any(len(a.data) > cap for a in items):
        raise HTTPException(413, "an attachment is larger than the configured limits")
    try:
        return validate([(a.name, a.media_type, base64.b64decode(a.data.split(",", 1)[-1], validate=True))
                         for a in items])
    except binascii.Error:
        raise HTTPException(400, "attachment data must be base64")
    except ValueError as e:
        raise HTTPException(400, str(e))


def create_app(bus: MessageBus) -> FastAPI:
    async def purge_loop():  # remove trashed tasks past their retention time
        while True:
            await asyncio.sleep(3600)
            try:
                bus.lifecycle.purge_expired()
            except Exception:  # never take the server down over cleanup
                pass

    plugins = bus.plugins

    @asynccontextmanager
    async def lifespan(_app):
        await bus.mcp.start()  # launch local MCP servers
        purger = asyncio.create_task(purge_loop())
        if plugins:  # schedules, channels, feeds, peering, …: in dependency order; a failing one is turned off
            await plugins.start()
        yield
        purger.cancel()
        if plugins:
            await plugins.stop()
        await bus.mcp.stop()

    app = FastAPI(title="xqo", lifespan=lifespan)

    @app.exception_handler(Draining)
    async def draining(_request, exc: Draining):
        return JSONResponse({"detail": str(exc)}, status_code=503)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")  # render.js + vendored libraries
    store = bus.artifacts
    background: set[asyncio.Task] = set()

    def named(page: str) -> str:  # the HQ name ("name" in agents.json) in the page's <title> and header
        return named_page(STATIC / page, bus)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return named("index.html")

    @app.get("/api/usage")
    async def usage():
        """Overall tokens and estimated cost of this xqo (usage.py), for the office header."""
        return bus.usage_total.report() if getattr(bus, "usage_total", None) else {}

    @app.get("/api/agents")
    async def agents():
        return bus.roster()

    @app.get("/api/avatars/{name}")
    async def avatar(name: str):
        a = bus.agents.get(name)
        info = getattr(bus, "user_avatar", None) if name == "user" and not a else getattr(a, "avatar", None)
        if not info or info["kind"] != "file" or not Path(info["path"]).is_file():
            raise HTTPException(404)
        return FileResponse(info["path"], media_type=info["type"],
                            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
                                     "Cache-Control": "max-age=86400"})

    archived = lambda root: bool(bus.lifecycle and bus.lifecycle.is_archived(root))

    @app.get("/api/history")
    async def history():
        """Recent events; archived task trees are left out (their archive events stay)."""
        if not (bus.lifecycle and bus.lifecycle.archived):
            return bus.history
        return [e for e in bus.history if e["kind"] in ("task_archived", "task_unarchived")
                or not archived(e.get("root_task_id"))]

    @app.get("/api/tasks")
    async def tasks(archived_: str = Query("all", alias="archived", pattern="^(all|true|false)$"),
                    roots: bool = False, status: str | None = Query(None, max_length=40),
                    q: str | None = Query(None, max_length=200), limit: int | None = Query(None, ge=1, le=5000),
                    offset: int = Query(0, ge=0)):
        """Task records, every task the store knows (not only recent ones). ?archived=false|true filters archived
        trees, ?roots=true only top-level tasks, ?status= and ?q= (name / prompt text) filter, ?limit=&offset=
        page them newest first. Without limit: the full list, as before."""
        rows = list(bus.tasks.values())
        if archived_ != "all":
            want = archived_ == "true"
            rows = [t for t in rows if archived(t.root_id) == want]
        if roots:
            rows = [t for t in rows if t.parent_id is None]
        if status:
            rows = [t for t in rows if t.status == status]
        if q:
            ql = q.lower()
            rows = [t for t in rows if ql in (t.name or "").lower() or ql in (t.prompt or "").lower()]
        if limit is None:
            return [t.to_dict() for t in rows]
        rows.sort(key=lambda t: t.created, reverse=True)
        return [t.to_dict() for t in rows[offset:offset + limit]]

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str):
        if task_id not in bus.tasks:
            raise HTTPException(404)
        return bus.tree(task_id)

    def start(coro):
        job = asyncio.create_task(coro)
        background.add(job)
        job.add_done_callback(background.discard)

    decode = decode_attachments

    @app.post("/api/tasks")
    async def submit(body: TaskIn):
        if body.agent:
            body.agent = bus.agent_id(body.agent) or body.agent  # id or nickname
        if body.agent and body.agent not in bus.agents:
            raise HTTPException(404, f"unknown agent {body.agent}")
        if not body.prompt.strip() and not body.attachments:
            raise HTTPException(422, "write a prompt or attach a file")
        files = decode(body.attachments)
        paused = None
        if body.urgent and body.pause_task:
            try:
                paused = bus.chat.pause(body.pause_task, "user", "urgent new task").id
            except (KeyError, ValueError):
                pass  # nothing running there: just start the new task
        task = await bus.create(body.prompt.strip(), body.agent, files=files)
        start(bus._run(task))  # created up front so the dashboard can select it straight away
        return {"accepted": True, "id": task.id, "name": task.name, "paused": paused, "attachments": [{k: f[k] for k in ("name", "kind", "media_type")} for f in files]}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str, body: CancelIn | None = None):
        try:
            task = await bus.cancel(task_id, "user", (body.reason if body else ""))
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return task.to_dict()

    def wake_archived(task_id: str) -> None:
        """Asking, steering or resuming an archived task brings it back to the task list first."""
        t = bus.tasks.get(task_id)
        if t and archived(t.root_id):
            bus.lifecycle.unarchive(t.root_id, "user")

    @app.post("/api/tasks/{task_id}/chat")
    async def chat(task_id: str, body: ChatIn):
        """Ask or Steer a task's agents; a task that is not running is reopened."""
        if not body.text.strip() and not body.attachments:
            raise HTTPException(422, "write a message or attach a file")
        files = decode(body.attachments)
        wake_archived(task_id)
        try:
            return await bus.chat.send(task_id, body.text, body.kind, body.to, body.urgent, "user", files)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0] if e.args else e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/tasks/{task_id}/pause")
    async def pause(task_id: str, body: PauseIn | None = None):
        try:
            return bus.chat.pause(task_id, "user", body.reason if body else "").to_dict()
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: str):
        """Release a paused task; one that is no longer running continues in the same task."""
        wake_archived(task_id)
        try:
            task, reopened = bus.chat.resume(task_id, "user")
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {**task.to_dict(), "reopened": reopened}

    def _delete(task_id: str, permanent: bool) -> dict:
        try:
            return bus.lifecycle.delete(task_id, "user", permanent)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str, permanent: bool = False):
        return _delete(task_id, permanent)

    @app.post("/api/tasks/delete")
    async def delete_many(body: DeleteIn):
        """Delete several tasks; each one succeeds or fails on its own."""
        out = []
        for tid in dict.fromkeys(body.ids):
            try:
                out.append({"id": tid, "ok": True, **_delete(tid, body.permanent)})
            except HTTPException as e:
                out.append({"id": tid, "ok": False, "error": e.detail, "status": e.status_code})
        return out

    def _archive(task_id: str, on: bool) -> dict:
        try:
            return bus.lifecycle.archive(task_id, "user") if on else bus.lifecycle.unarchive(task_id, "user")
        except KeyError as e:
            raise HTTPException(404, str(e.args[0] if e.args else e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/archive")
    async def archive_list():
        return {"items": bus.lifecycle.archived_entries()}

    @app.post("/api/tasks/{task_id}/archive")
    async def archive_task(task_id: str):
        return _archive(task_id, True)

    @app.post("/api/tasks/{task_id}/unarchive")
    async def unarchive_task(task_id: str):
        return _archive(task_id, False)

    @app.post("/api/tasks/archive")
    async def archive_many(body: ArchiveIn):
        """Archive (or with archived=false unarchive) several tasks; each one succeeds or fails on its own."""
        out = []
        for tid in dict.fromkeys(body.ids):
            try:
                out.append({"ok": True, **_archive(tid, body.archived)})
            except HTTPException as e:
                out.append({"id": tid, "ok": False, "error": e.detail, "status": e.status_code})
        return out

    @app.get("/api/trash")
    async def trash():
        return {"retention_days": bus.lifecycle.retention_days, "items": bus.lifecycle.trash_entries()}

    @app.post("/api/trash/{task_id}/restore")
    async def restore(task_id: str):
        try:
            return {**bus.lifecycle.restore(task_id, "user"), "archived": archived(task_id)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.delete("/api/trash/{task_id}")
    async def purge(task_id: str):
        try:
            return bus.lifecycle.purge(task_id, "user")
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/folders/{root_id}")
    async def folders(root_id: str):
        """A task's folders as the user sees them (host path in Docker, remote path over SSH) + editor links."""
        task_dir = store.workspace(root_id).parent
        if not task_dir.is_dir():
            raise HTTPException(404, "no folder for this task yet")
        cfg = getattr(bus, "folders", None) or FolderConfig()
        return cfg.describe([("task", task_dir), ("workspace", store.workspace(root_id)),
                             ("attachments", bus.attachments.folder(root_id))])

    @app.get("/api/attachments/{root_id}/{name}")
    async def attachment(root_id: str, name: str, download: bool = False):
        p = bus.attachments.path(root_id, name)
        if not p:
            raise HTTPException(404)
        meta = next((a for a in getattr(bus.tasks.get(root_id), "attachments", []) if a["name"] == p.name), {})
        mt = meta.get("media_type") if meta.get("kind") in ("image", "pdf") else "text/plain; charset=utf-8"
        return FileResponse(p, media_type=mt, filename=p.name if download else None,
                            headers={"X-Content-Type-Options": "nosniff",
                                     **({} if meta.get("kind") == "pdf" else {"Content-Security-Policy": "sandbox"})})

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str):
        """Full event log of a task tree, oldest first, for step-by-step replay."""
        events = bus.task_events(task_id)
        if not events or bus.lifecycle.entry(task_id):
            raise HTTPException(404)
        return events

    @app.post("/api/tasks/{task_id}/fork")
    async def fork(task_id: str, body: ForkIn):
        if body.agent:
            body.agent = bus.agent_id(body.agent) or body.agent  # id or nickname
        if body.agent and body.agent not in bus.agents:
            raise HTTPException(404, f"unknown agent {body.agent}")
        if bus.lifecycle.entry(task_id):
            raise HTTPException(404, f"{task_id} is in the trash; restore it first")
        try:
            task = await bus.fork(task_id, body.event_id, (body.prompt or "").strip() or None, body.agent)
        except KeyError as e:
            raise HTTPException(404, str(e))
        start(bus._run(task))
        return task.to_dict()

    @app.get("/api/git/{root_id}/log")
    async def git_log(root_id: str):
        return {"enabled": bool(store.git and store.git.enabled), "commits": store.git_log(root_id)}

    @app.post("/api/git/{root_id}/push")
    async def git_push(root_id: str, body: PushIn):
        if not (store.git and store.git.enabled):
            raise HTTPException(400, "git is disabled")
        try:
            res = await asyncio.to_thread(store.git.push, store.workspace(root_id), body.remote_url, body.branch)
        except (GitError, OSError) as e:
            raise HTTPException(400, str(e))
        bus.emit(Event("git", "user", None, root_id, None, f"pushed {res['head'][:10]} to {res['remote']} ({res['branch']})",
                       {k: res[k] for k in ("remote", "branch", "head")}))
        return res

    @app.get("/api/events")
    async def events():
        q = bus.subscribe()

        async def gen():
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/artifacts")
    async def artifacts(task_id: str | None = None):
        rows, names = store.list(task_id), {}
        for r in rows:
            root = r.get("root_task_id") or r["task_id"]
            if root not in names:
                base, _, dept = root.rpartition(".")
                t = bus.tasks.get(root)
                if not t and base and dept in bus.departments:  # a department's own workspace
                    bt = bus.tasks.get(base)
                    names[root] = f"{bt.name if bt else base} · {dept} department"
                else:
                    names[root] = t.name if t else store.task_meta(root).get("name", root)
            r["root_task_name"] = names[root]
        return rows

    @app.get("/api/artifacts/file/{rel:path}")
    async def artifact_file(rel: str):
        p = store.resolve(rel)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, filename=rel.replace("/", "__"))

    @app.get("/api/artifacts/zip/{task_id}")
    async def artifact_zip(task_id: str, workspace_only: bool = False, source_only: bool = False):
        data = store.zip_task(task_id, workspace_only, source_only)
        kind = "source" if source_only else "workspace" if workspace_only else "artifacts"
        name = f"{task_id}_{kind}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/departments")
    async def departments():
        out = []
        groups = [("hq", None)] + [(n, d) for n, d in bus.departments.items()]
        for name, d in groups:
            members = [a.describe() for a in bus.agents.values() if getattr(a, "department", "hq") == name]
            if not members and d is None:
                continue
            info = d.to_dict() if d else {"name": "hq", "description": "Headquarters: agents from agents.json; "
                                          "they can reach every department.", "head": None, "contacts": "*",
                                          "budget": {}, "members": [m["name"] for m in members]}
            info["agents"] = members
            info["active"] = sum(m["active"] for m in members)
            info["usage"] = bus.budget.report(name) if bus.budget else {}
            out.append(info)
        return out

    @app.post("/api/departments/reload")
    async def reload(body: ReloadIn | None = None):
        from .config import reload_departments, sync_mcp
        try:
            summary = reload_departments(bus, "keep" if not body or body.departments is None else body.departments)
        except (ValueError, KeyError, OSError) as e:
            raise HTTPException(400, str(e))
        summary["mcp"] = await sync_mcp(bus)  # local MCP servers follow the new config
        return summary

    @app.get("/api/config")
    async def config():
        g = store.git
        return {"attention_timeout": bus.attention.timeout,
                "git": {"enabled": bool(g and g.enabled), "branch": g.config.branch if g else None,
                        "remote": g.config.remote if g else None,
                        "token_env": g.config.push_token_env if g else None,
                        "token_set": bool(g and os.getenv(g.config.push_token_env or ""))},
                "sandbox": {"backend": bus.sandbox.backend, "image": bus.sandbox.config.image,
                            "timeout": bus.sandbox.config.timeout},
                "mcp": bus.mcp.status() + [{"name": s["name"], "kind": "remote", "url": s["url"]}
                                           for s in getattr(bus, "mcp_remote", [])],
                "skills": bus.skills.describe() if getattr(bus, "skills", None) else [],
                "providers": getattr(bus, "providers", {}),
                "plugins": plugins.describe() if plugins else [],
                "storage": bus.store.describe(),
                "limits": bus.limits.config or None,
                "user_avatar": avatar_public("user", getattr(bus, "user_avatar", None)),
                "user_nickname": getattr(bus, "user_nickname", None),
                "graph_layout": getattr(bus, "graph_layout", None),
                "offer_judge": bus.offer_judge.status() if getattr(bus, "offer_judge", None) else None,
                "office": office.describe() if (office := plugins and plugins.get("office")) else None}

    @app.get("/api/limits")
    async def limits():
        return bus.limits.status()

    @app.post("/api/limits")
    async def set_limits(body: LimitsIn):
        """System-wide: max_turns / max_tokens, the task time limit and department budgets off for one hour
        for every agent, running tasks included; open budget / time-limit questions are answered 'extend'."""
        st = bus.limits.enable() if body.enabled else bus.limits.disable()
        extended = bus.lift_pending() if st["active"] else []
        bus.emit(Event("limits", "user", None, None, None,
                       "resource limits (turns, output size, task time, budgets) off for one hour"
                       + (f"; {len(extended)} open question(s) extended" if extended else "")
                       if st["active"] else "resource limits back on", {**st, "extended": extended}))
        return st

    @app.get("/api/memory/{root_id}")
    async def memory(root_id: str):
        return {"notes": list(bus.memory.notes(root_id).values()), "log": bus.memory.log(root_id)}

    @app.post("/api/memory/{root_id}")
    async def add_note(root_id: str, body: NoteIn):
        try:
            entry = bus.memory.set(root_id, body.key, body.value, "user")
        except ValueError as e:
            raise HTTPException(400, str(e))
        bus.emit(Event("memory", "user", None, root_id, None, f"{entry['op']} {entry['key']}: {entry['value'][:200]}", entry))
        return entry

    @app.post("/api/workspace/{root_id}/rollback")
    async def rollback(root_id: str, body: RollbackIn):
        try:
            rec = store.rollback(root_id, body.file, body.version)
        except ValueError as e:
            raise HTTPException(404, str(e))
        bus.emit(Event("artifact", "user", None, root_id, None, f"rollback: {rec['file']}", rec))
        return rec

    @app.get("/api/attention")
    async def attention(pending: bool = False):
        items = bus.attention.pending() if pending else list(bus.attention.items.values())
        return [a.to_dict() for a in reversed(items)]

    @app.post("/api/attention/{att_id}")
    async def answer_attention(att_id: str, body: AnswerIn):
        try:
            return bus.attention.resolve(att_id, body.answer).to_dict()
        except KeyError:
            raise HTTPException(404)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/agents/{name}/reports")
    async def agent_reports(name: str, limit: int = 10):
        """The agent's newest Markdown reports (final answers and the report tool); content: /api/artifacts/file/."""
        if name not in bus.agents:
            raise HTTPException(404, f"no agent {name!r}")
        rows = store.reports(name, max(1, min(limit, 100)))
        for r in rows:
            root = r.get("root_task_id") or r["task_id"]
            t = bus.tasks.get(root)
            r["root_task_name"] = t.name if t else store.task_meta(root).get("name", root)
        return rows

    @app.get("/api/reports/{rel:path}")
    async def report_content(rel: str):
        """One report's Markdown as JSON (the office reads it through its relay)."""
        p = store.resolve(rel)
        parts = rel.split("/")
        if not p or ".." in parts or len(parts) < 2 or parts[-2] != "reports" or not rel.endswith(".md"):
            raise HTTPException(404)
        return {"path": rel, "markdown": p.read_text(encoding="utf-8", errors="replace")}

    @app.get("/api/alerts")
    async def alerts(agent: str | None = None, root_id: str | None = None):
        """What needs the user (attention.py alerts): open questions, auto-answered ones, errors, finished root tasks."""
        return {"items": [a.to_dict() for a in reversed(bus.attention.alerts_pending(agent, root_id))],
                "by_agent": bus.attention.by_agent()}

    @app.post("/api/alerts/ack")
    async def ack_alerts(body: AckIn):
        """Dismiss alerts; open questions stay until they are answered."""
        return {"acked": bus.attention.ack(body.ids, body.agent, body.root_id)}

    @app.get("/api/workspace/{root_id}")
    async def workspace(root_id: str):
        files = store.list_files(root_id)
        for f in files:
            f["versions"] = len(store.history(root_id, f["file"]))
        return files

    @app.get("/api/workspace/{root_id}/file/{rel:path}")
    async def workspace_file(root_id: str, rel: str):
        try:
            _, p = store._ws_file(root_id, rel)
        except ValueError:
            raise HTTPException(404)
        p = store.resolve(store._rel(p)) if p.is_file() else None
        if not p:
            raise HTTPException(404)
        return FileResponse(p, filename=Path(rel).name)

    @app.get("/api/workspace/{root_id}/history/{rel:path}")
    async def workspace_history(root_id: str, rel: str):
        return store.history(root_id, rel)

    # ---- operations: health, backup, drain (upgrade path; see SETUP.md) ----------------------------
    @app.get("/api/health")
    async def health():
        from . import __version__
        from .migrations import DATA_VERSION, read_data_version
        running = bus.running_roots()
        return {"ok": True, "version": __version__, "data_version": read_data_version(store.root),
                "code_data_version": DATA_VERSION, "migrations_applied": getattr(bus, "migrations_applied", []),
                "draining": bus.draining, "backup_running": bus.quiesced > 0,
                "running_tasks": [{"id": t.id, "name": t.name, "status": t.status} for t in running],
                "plugins": plugins.health() if plugins else {},
                "mcp_errors": {k: v for k, v in bus.mcp.errors.items()},
                "warnings": list(getattr(bus, "config_warnings", []))}

    @app.post("/api/admin/backup")
    async def admin_backup(body: BackupIn | None = None):
        from . import backup
        body = body or BackupIn()
        bus.quiesced += 1  # scheduler, feeds and channel polling wait while the archive is written
        try:
            res = await asyncio.to_thread(backup.create, bus, body.scope, getattr(bus, "backup_dir", None),
                                          getattr(bus, "data_dir", "."), body.keep)
        except (OSError, ValueError) as e:
            raise HTTPException(500, f"backup failed: {e}")
        finally:
            bus.quiesced -= 1
        bus.emit(Event("backup", "user", None, None, None,
                       f"backup ({res['scope']}): {Path(res['path']).name}, {res['files']} file(s)", res))
        return res

    @app.post("/api/admin/drain")
    async def admin_drain(body: DrainIn | None = None):
        on = body.on if body else True
        bus.draining = on
        bus.emit(Event("drain", "user", None, None, None,
                       "draining: no new tasks until the restart" if on else "taking new tasks again"))
        return await health()

    @app.post("/api/admin/stop")
    async def admin_stop():
        """Stop the server process (after a drain). Docker/systemd restart it with the new version."""
        import signal
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
        return {"stopping": True, "running_tasks": len(bus.running_roots())}

    # ---- plugins: their state, then their own routes and browser files ----------------------------------
    @app.get("/api/plugins")
    async def plugin_list():
        """Every plugin this installation has: active, inactive (not configured), disabled or failed (and why)."""
        return {"strict": plugins.strict if plugins else False, "items": plugins.describe() if plugins else []}

    if plugins:
        for p in plugins.plugins.values():
            if plugins.states[p.name].state in ("failed", "disabled"):
                continue
            try:
                if p.static:
                    prefix, folder = p.static
                    app.mount(prefix, StaticFiles(directory=folder), name=f"plugin-{p.name}")
                p.routes(app, bus)
            except Exception as e:  # a broken plugin loses its routes; the dashboard still starts
                if plugins.strict:
                    raise
                plugins._fail(p.name, f"routes: {type(e).__name__}: {e}")

    return app


