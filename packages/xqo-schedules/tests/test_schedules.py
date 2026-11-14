"""Scheduled and repeating tasks (scheduler.py) and archiving finished tasks (archive.py)."""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo_schedules.scheduler import Cron, Scheduler, next_after, normalize, parse_duration
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent  # agents.json: a copy of the base package's example config
BERLIN = ZoneInfo("Europe/Berlin")


def make(tmp_path, cfg=None):
    path = ROOT / "agents.json"
    if cfg is not None:
        path = tmp_path / "agents.json"
        path.write_text(json.dumps({**json.loads((ROOT / "agents.json").read_text()), **cfg}))
    return build(path, tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)


async def until(cond, timeout=10):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def ts(s, tz=BERLIN):
    return datetime.fromisoformat(s).replace(tzinfo=tz).timestamp()


def runs_of(b, sid):
    return [t for t in b.tasks.values() if (t.schedule or {}).get("id") == sid]


# ------------------------------------------------------------------ triggers
def test_cron_next_runs():
    c = Cron("0 2 * * 1-5")  # 02:00 on weekdays
    fri = ts("2026-10-30T03:00")  # a Friday, after 02:00
    assert datetime.fromtimestamp(c.next_after(fri, BERLIN), BERLIN).isoformat()[:16] == "2026-11-02T02:00"
    q = Cron("*/15 * * * *")
    assert q.next_after(ts("2026-10-30T10:07"), BERLIN) == ts("2026-10-30T10:15")
    assert q.next_after(ts("2026-10-30T10:15"), BERLIN) == ts("2026-10-30T10:30")  # strictly after
    both = Cron("0 9 13 * fri")  # the 13th or a Friday, as in cron
    nxt = datetime.fromtimestamp(both.next_after(ts("2026-11-01T00:00"), BERLIN), BERLIN)
    assert nxt.isoformat()[:10] == "2026-11-06"
    assert Cron("@daily").next_after(ts("2026-10-30T10:00"), BERLIN) == ts("2026-10-31T00:00")
    assert Cron("30 8 1 jan,jul *").next_after(ts("2026-02-01T00:00"), BERLIN) == ts("2026-07-01T08:30")
    for bad in ("* * *", "61 * * * *", "0 25 * * *", "a b c d e", "5-1 * * * *", "*/0 * * * *"):
        with pytest.raises(ValueError):
            Cron(bad)


def test_interval_once_and_durations():
    assert parse_duration("90s") == 90 and parse_duration("6h") == 21600 and parse_duration(120) == 120
    with pytest.raises(ValueError):
        parse_duration("soon")
    s = normalize({"name": "x", "prompt": "p", "trigger": {"kind": "interval", "every": "1h", "start": 1000}})
    assert next_after(s, 500) == 1000 and next_after(s, 1000) == 4600 and next_after(s, 4599) == 4600
    with pytest.raises(ValueError):
        normalize({"name": "x", "prompt": "p", "every": "10s"})  # shorter than a minute
    o = normalize({"name": "x", "prompt": "p", "at": "2026-11-01T09:00", "tz": "Europe/Berlin"})
    assert o.trigger == {"kind": "once", "at": ts("2026-11-01T09:00")}
    assert next_after(o, ts("2026-11-01T09:00")) is None
    for bad in ({"name": "x", "prompt": "p"},  # no trigger
                {"name": "", "prompt": "p", "every": "1h"},
                {"name": "x", "prompt": "", "every": "1h"},
                {"name": "x", "prompt": "p", "every": "1h", "tz": "Mars/Base"},
                {"name": "x", "prompt": "p", "every": "1h", "overlap": "sometimes"},
                {"name": "x", "prompt": "p", "every": "1h", "limits": {"max_turns": 0}},
                {"name": "x", "prompt": "p", "every": "1h", "limits": {"turns": 3}},
                {"name": "x", "prompt": "p", "every": "1h", "colour": "red"},
                {"name": "x", "prompt": "p", "every": "1h", "cron": "* * * * *"}):
        with pytest.raises(ValueError):
            normalize(bad)


# ------------------------------------------------------------------ firing
def test_schedule_fires_a_normal_task_and_survives_restart(tmp_path):
    b = make(tmp_path)
    sc = b.scheduler
    sc.clock = clk = Clock(ts("2026-10-30T09:00"))
    v = sc.create({"name": "Nightly fib", "prompt": "Build fib", "cron": "0 2 * * *", "tz": "Europe/Berlin"})
    assert v["next_run"].startswith("2026-10-31T01:00")  # 02:00 Berlin (CET, UTC+1)
    assert v["summary"] == "daily at 02:00 (Europe/Berlin)"

    async def go():
        await sc.tick()
        assert not runs_of(b, v["id"])  # not due yet
        clk.t = ts("2026-10-31T02:00")
        await sc.tick()
        await until(lambda: not sc._jobs.get(v["id"]))

    asyncio.run(go())
    (run,) = runs_of(b, v["id"])
    assert run.status == "done" and run.title == "Nightly fib #1" and run.prompt == "Build fib"
    assert run.schedule["run"] == 1 and run.unattended and run.limits == {"honor_no_limits": False}
    view = sc.view(sc.get(v["id"]))
    assert view["runs"] == 1 and view["last_status"] == "done" and view["last_task"] == run.id
    assert view["next_run"].startswith("2026-11-01T01:00")
    kinds = [e["content"] for e in sc.runs(v["id"])]
    assert any("run 1 started" in k for k in kinds) and any("run 1 done" in k for k in kinds)
    # a restart keeps the schedule, its state and the run's schedule link
    b2 = make(tmp_path)
    v2 = b2.scheduler.view(b2.scheduler.get(v["id"]))
    assert v2["runs"] == 1 and v2["last_task"] == run.id and v2["name"] == "Nightly fib"
    assert b2.tasks[run.id].schedule["id"] == v["id"] and b2.tasks[run.id].unattended


def test_overlap_skip_and_queue(tmp_path):
    b = make(tmp_path)
    b.attention.timeout = 30  # the first run waits on the user
    sc = b.scheduler
    sc.clock = clk = Clock(1_000_000.0)
    skip = sc.create({"name": "skipper", "prompt": "Please confirm, then build fib", "every": "1h",
                      "unattended": False})
    queue = sc.create({"name": "queuer", "prompt": "Please confirm, then build fib", "every": "1h",
                       "unattended": False, "overlap": "queue"})

    async def go():
        clk.t += 3600
        await sc.tick()
        await until(lambda: len(b.attention.pending()) == 2)
        clk.t += 3600
        await sc.tick()  # both still waiting on the user
        assert len(runs_of(b, skip["id"])) == 1 and len(runs_of(b, queue["id"])) == 1
        assert sc.st(queue["id"])["queued"]
        for a in b.attention.pending():
            b.attention.resolve(a.id, "yes")
        await until(lambda: not sc._jobs.get(skip["id"]) and not sc._jobs.get(queue["id"]))
        await sc.tick()  # the loop's next tick (woken when a run ends) starts the queued run
        assert len(runs_of(b, queue["id"])) == 2 and not sc.st(queue["id"])["queued"]
        for _ in range(50):
            await sc.tick()
            await asyncio.sleep(0.02)
            for a in b.attention.pending():
                b.attention.resolve(a.id, "yes")
            if not sc._jobs.get(queue["id"]):
                break

    asyncio.run(go())
    assert len(runs_of(b, skip["id"])) == 1
    assert any(e["meta"].get("outcome") == "skipped" for e in sc.runs(skip["id"]))
    assert any(e["meta"].get("outcome") == "queued" for e in sc.runs(queue["id"]))


def test_missed_runs_after_downtime(tmp_path):
    b = make(tmp_path)
    sc = b.scheduler
    sc.clock = Clock(1_000_000.0)
    once = sc.create({"name": "catch up", "prompt": "Build fib", "every": "1d"})
    skip = sc.create({"name": "no catch up", "prompt": "Build fib", "every": "1d", "missed": "skip"})
    late = sc.create({"name": "too late", "prompt": "Build fib", "every": "1d", "grace": "1h"})
    # the server was down when they were due
    b2 = make(tmp_path)
    sc2 = b2.scheduler
    sc2.clock = Clock(1_000_000.0 + 86400 + 1800)  # 30 min after the due time
    sc2.state[late["id"]]["next_run"] = 1_000_000.0 + 86400 - 3 * 3600  # 3.5 h ago for this one
    sc2.startup()

    async def go():
        await sc2.tick()
        await until(lambda: not any(sc2._jobs.values()))

    asyncio.run(go())
    assert [t.schedule["reason"] for t in runs_of(b2, once["id"])] == ["missed"]
    assert not runs_of(b2, skip["id"]) and not runs_of(b2, late["id"])
    assert sc2.runs(skip["id"])[0]["kind"] == "schedule_missed"
    nxt = sc2.st(once["id"])["next_run"]
    assert nxt > sc2.clock()  # one catch-up run, not one per missed day


def test_unattended_questions_take_the_default_at_once(tmp_path):
    b = make(tmp_path)
    b.attention.timeout = 60
    sc = b.scheduler
    v = sc.create({"name": "confirm", "prompt": "Please confirm, then build fib", "every": "1h"})

    async def go():
        t0 = time.monotonic()
        r = await sc.run_now(v["id"])
        await until(lambda: not sc._jobs.get(v["id"]))
        return r, time.monotonic() - t0

    r, took = asyncio.run(go())
    assert took < 20
    att = [a for a in b.attention.items.values() if b.tasks[a.task_id].root_id == r["task_id"]]
    assert att and all(a.status == "auto_confirmed" and a.resolved_by == "unattended" for a in att)
    assert b.tasks[r["task_id"]].schedule["reason"] == "manual"


def test_run_limits_and_no_limits_hour(tmp_path):
    b = make(tmp_path)
    sc = b.scheduler
    v = sc.create({"name": "lim", "prompt": "Build fib", "every": "1h",
                   "limits": {"max_turns": 3, "max_tokens": 1234}})
    task = asyncio.run(b.create("Build fib", meta={"limits": sc.get(v["id"]).limits | {"honor_no_limits": False}}))
    agent = b.agents[task.assignee]
    other = asyncio.run(b.create("Build fib"))
    assert agent.turn_limit(task) == 3 and agent.output_limit(task) == 1234
    b.limits.enable()  # the dashboard's No limits hour
    assert agent.turn_limit(task) == 3 and agent.output_limit(task) == 1234  # scheduled run: not affected
    assert agent.turn_limit(other) == float("inf")  # normal tasks are
    honor = asyncio.run(b.create("Build fib", meta={"limits": {"max_turns": 3, "honor_no_limits": True}}))
    assert agent.turn_limit(honor) == float("inf")


def test_cost_cap_cancels_and_failures_pause(tmp_path, monkeypatch):
    b = make(tmp_path)
    b.attention.timeout = 30
    sc = b.scheduler
    monkeypatch.setattr("xqo_schedules.scheduler.COST_CHECK", 0.05)
    v = sc.create({"name": "pricey", "prompt": "Please confirm, then build fib", "every": "1h",
                   "unattended": False, "limits": {"max_cost_usd": 0.5}, "max_failures": 2})
    monkeypatch.setattr(sc, "tree_cost", lambda root: 1.0)

    async def go():
        for _ in range(2):
            await sc.run_now(v["id"])
            await until(lambda: not sc._jobs.get(v["id"]))

    asyncio.run(go())
    runs = runs_of(b, v["id"])
    assert len(runs) == 2 and all(t.status == "cancelled" and "cost cap" in t.result for t in runs)
    view = sc.view(sc.get(v["id"]))
    assert view["last_status"] == "cost_cap" and not view["enabled"]
    assert "2 failed runs in a row" in view["paused_reason"]
    assert sc.runs(v["id"])[0]["kind"] == "schedule_paused"
    view = sc.set_enabled(v["id"], True)  # resuming clears the failure count
    assert view["enabled"] and view["failures_in_row"] == 0 and view["next_run"]


def test_missing_agent_counts_as_a_failure(tmp_path):
    b = make(tmp_path)
    sc = b.scheduler
    v = sc.create({"name": "gone", "prompt": "Build fib", "every": "1h", "agent": next(iter(b.agents)),
                   "max_failures": 1})
    b.agents.pop(sc.get(v["id"]).agent)
    r = asyncio.run(sc.run_now(v["id"]))
    assert r["task_id"] is None and not sc.view(sc.get(v["id"]))["enabled"]
    assert sc.view(sc.get(v["id"]))["agent_missing"]


def test_one_off_and_end_conditions(tmp_path):
    b = make(tmp_path)
    sc = b.scheduler
    sc.clock = clk = Clock(ts("2026-10-30T08:00"))
    o = sc.create({"name": "once", "prompt": "Build fib", "at": "2026-10-30T09:00", "tz": "Europe/Berlin"})
    n = sc.create({"name": "twice", "prompt": "Build fib", "every": "1h", "end": {"after_runs": 2}})

    async def go():
        for _ in range(4):
            clk.t += 3600
            await sc.tick()
            await until(lambda: not any(sc._jobs.values()))

    asyncio.run(go())
    assert len(runs_of(b, o["id"])) == 1 and len(runs_of(b, n["id"])) == 2
    for sid in (o["id"], n["id"]):
        view = sc.view(sc.get(sid))
        assert view["finished"] and not view["enabled"] and view["next_run"] is None


def test_config_schedules_are_read_only_except_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_STRICT", "1")  # xqo: plugin config errors stop startup only in strict mode
    cfg = {"schedules": [{"name": "Daily report", "prompt": "Build fib", "cron": "0 7 * * *"}]}
    b = make(tmp_path, cfg)
    sc = b.scheduler
    (v,) = sc.list()
    assert v["id"] == "cfg-daily-report" and v["source"] == "config" and v["enabled"]
    with pytest.raises(PermissionError):
        sc.update(v["id"], {"prompt": "something else"})
    with pytest.raises(PermissionError):
        sc.delete(v["id"])
    assert not sc.update(v["id"], {"enabled": False})["enabled"]
    b2 = make(tmp_path, cfg)  # the pause is kept in the data dir, the file is untouched
    assert not b2.scheduler.view(b2.scheduler.get(v["id"]))["enabled"]
    with pytest.raises(ValueError):
        make(tmp_path, {"schedules": [{"name": "bad", "prompt": "x", "cron": "nope"}]})


def test_only_one_process_runs_the_schedules(tmp_path):
    a, b = make(tmp_path), make(tmp_path)

    async def go():
        ok1 = a.scheduler.start()
        ok2 = b.scheduler.start()
        await a.scheduler.stop()
        return ok1, ok2

    ok1, ok2 = asyncio.run(go())
    assert ok1 and not ok2 and "another xqo process" in b.scheduler.lock_error


def test_schedules_api_and_attachments(tmp_path):
    b = make(tmp_path)
    with TestClient(create_app(b)) as c:
        r = c.post("/api/schedules", json={"name": "Weekly", "prompt": "Summarize the file", "cron": "0 9 * * mon",
                                           "attachments": [{"name": "notes.txt", "data": "aGVsbG8="}]})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert r.json()["attachments"][0]["name"] == "notes.txt"
        assert c.get("/api/schedules").json()["running"]
        assert c.post("/api/schedules", json={"name": "x", "prompt": "y", "cron": "bad"}).status_code == 422
        assert c.post("/api/schedules", json={"name": "x", "prompt": "y", "every": "1h", "agent": "nobody"}).status_code == 422
        assert c.post("/api/schedules", json={"name": "x", "prompt": "y", "every": "1h", "bogus": 1}).status_code == 422
        r = c.patch(f"/api/schedules/{sid}", json={"every": "2h", "overlap": "queue"})
        assert r.json()["summary"] == "every 2h" and r.json()["overlap"] == "queue"
        assert not c.post(f"/api/schedules/{sid}/pause").json()["enabled"]
        assert c.post(f"/api/schedules/{sid}/resume").json()["enabled"]
        r = c.post(f"/api/schedules/{sid}/run").json()
        tid = r["task_id"]
        for _ in range(200):
            if b.tasks[tid].status == "done":
                break
            time.sleep(0.05)
        assert b.tasks[tid].attachments[0]["name"] == "notes.txt"
        assert any(e["kind"] == "schedule_run" for e in c.get(f"/api/schedules/{sid}/runs").json())
        assert c.delete(f"/api/schedules/{sid}").json()["deleted"]
        assert c.get(f"/api/schedules/{sid}").status_code == 404
        # the server's own loop: a one-off time that has already passed runs at once
        r = c.post("/api/schedules", json={"name": "now", "prompt": "Build fib", "at": "2020-01-01T00:00"}).json()
        for _ in range(200):
            if runs_of(b, r["id"]) and runs_of(b, r["id"])[0].status == "done":
                break
            time.sleep(0.05)
        assert runs_of(b, r["id"])[0].status == "done"
        assert c.get(f"/api/schedules/{r['id']}").json()["finished"]


# ------------------------------------------------------------------ archive
def test_archive_and_unarchive(tmp_path):
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib"))
    with TestClient(create_app(b)) as c:
        assert c.post(f"/api/tasks/{t.id}/archive").json()["archived"]
        assert t.id not in {x["id"] for x in c.get("/api/tasks?archived=false").json()}
        assert t.id in {x["id"] for x in c.get("/api/tasks?archived=true").json()}
        assert next(x for x in c.get("/api/tasks").json() if x["id"] == t.id)["archived"]
        hist = c.get("/api/history").json()
        assert not [e for e in hist if e.get("root_task_id") == t.id and e["kind"] != "task_archived"]
        assert [e["root_id"] for e in c.get("/api/archive").json()["items"]] == [t.id]
        # restart keeps it archived
        b2 = make(tmp_path)
        assert b2.tasks[t.id].archived
        # trash and back: still archived; asking it brings it back
        assert c.delete(f"/api/tasks/{t.id}").status_code == 200
        r = c.post(f"/api/trash/{t.id}/restore").json()
        assert r["archived"] and b.tasks[t.id].archived
        r = c.post("/api/tasks/archive", json={"ids": [t.id, "task_nope"], "archived": False}).json()
        assert r[0]["ok"] and not r[0]["archived"] and not r[1]["ok"]
        c.post(f"/api/tasks/{t.id}/archive")
        assert c.post(f"/api/tasks/{t.id}/chat", json={"text": "and now?", "kind": "ask"}).status_code == 200
        assert not b.tasks[t.id].archived
        # permanent delete forgets it
        for _ in range(200):
            if b.tasks[t.id].status not in ("running", "pending", "needs_attention"):
                break
            time.sleep(0.05)
        c.post(f"/api/tasks/{t.id}/archive")
        assert c.delete(f"/api/tasks/{t.id}?permanent=true").status_code == 200
        assert t.id not in b.lifecycle.archived


def test_running_tasks_cannot_be_archived(tmp_path):
    b = make(tmp_path)
    b.attention.timeout = 30

    async def go():
        job = asyncio.create_task(b.submit("Please confirm, then build fib"))
        await until(lambda: b.attention.pending())
        root = next(t for t in b.tasks.values() if t.parent_id is None)
        with pytest.raises(ValueError):
            b.lifecycle.archive(root.id)
        sub = next((t for t in b.tasks.values() if t.parent_id), None)
        if sub:
            with pytest.raises(ValueError):
                b.lifecycle.archive(sub.id)
        for a in b.attention.pending():
            b.attention.resolve(a.id, "yes")
        await job

    asyncio.run(go())


def test_schedule_archives_its_old_runs(tmp_path):
    b = make(tmp_path)
    sc = b.scheduler
    v = sc.create({"name": "tidy", "prompt": "Build fib", "every": "1h", "archive_after": "1d"})

    async def go():
        r = await sc.run_now(v["id"])
        await until(lambda: not sc._jobs.get(v["id"]))
        return r["task_id"]

    tid = asyncio.run(go())
    assert sc.archive_old(time.time()) == []  # too new
    assert sc.archive_old(time.time() + 2 * 86400) == [tid] and b.tasks[tid].archived
