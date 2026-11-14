"""The Office view: the HQ drawn as an office (3D with three.js, or the 2D miniverse pixel office), plus a shared planners world.

The office page only draws. Every action it offers (new task, Ask, Steer, pause, answering a request)
goes through this dashboard's own API. For the shared planners world the page also reads other HQs'
dashboards; it reaches them through a small relay here, which only talks to the dashboards listed in
"office.hqs" and only forwards the few endpoints the page uses.

The office is laid out automatically (a room per department, a desk per agent, a lounge). What someone changes
in the page's Edit mode is kept per HQ in <artifacts>/office.json: moved, turned or hidden props, added props,
room sizes, colours, each agent's character and desk, plus catalog entries for custom models, whose .glb files
live in <artifacts>/office-assets/. The page applies it on top of the automatic layout, so new agents and
departments still get a desk and a room. See validate_layout for the format.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from ...branding import DEFAULT_TITLE, hq_name, title  # noqa: F401  (kept importable from here)

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# what the office page may read and do on another HQ's dashboard (paths below /api/)
RELAY_GET = re.compile(r"^(agents|departments|tasks|tasks/[\w.:-]+|attention|alerts|agents/[\w.:-]+/reports|reports/[\w.-]+(?:/[\w.-]+)*|office|usage|schedules|office/layout|office/assets/[A-Za-z0-9_-][A-Za-z0-9_.-]*)$")
RELAY_POST = re.compile(r"^(tasks|tasks/[\w.:-]+/(chat|pause|resume)|attention/[\w.:-]+|alerts/ack)$")
RELAY_MAX_BODY = 1_000_000
VIEWS = ("3d", "2d")  # the office page's renderers: 3D (three.js) and the 2D pixel office


@dataclass
class OfficeHQ:
    name: str
    url: str        # where this server reaches that dashboard (the relay)
    link: str = ""  # where the browser opens it ("open in dashboard"); default url


@dataclass
class OfficeConfig:
    name: str = ""                                      # this HQ's label in the planners world
    planners: list[str] = field(default_factory=list)   # agents shown in the planners world ([] = auto)
    hqs: list[OfficeHQ] = field(default_factory=list)  # other HQ dashboards in the planners world
    views: list[str] = field(default_factory=lambda: list(VIEWS))  # renderers the page offers, first = default
    idle: dict = field(default_factory=lambda: {"enabled": True, "activities": [*ACTS], "chat": True, "max_away": 0.6})
    edit: bool = False                                  # allow editing the office layout, like `serve --edit` (no login: keep 127.0.0.1)

    @classmethod
    def from_dict(cls, d: dict | None) -> "OfficeConfig":
        d = dict(d or {})
        if "XQO_OFFICE_HQS" in os.environ:  # "hq-b=http://127.0.0.1:8002;hq-c=http://10.0.0.5:8003"
            pairs = [p.split("=", 1) for p in os.environ["XQO_OFFICE_HQS"].split(";") if p.strip()]
            if any(len(p) != 2 for p in pairs):
                raise ValueError("XQO_OFFICE_HQS: expected 'name=url;name=url'")
            d["hqs"] = [{"name": a.strip(), "url": b.strip()} for a, b in pairs]
        unknown = set(d) - {"name", "planners", "hqs", "views", "idle", "edit"}
        if unknown:
            raise ValueError(f"office: unknown setting(s) {', '.join(sorted(unknown))}")
        name = str(d.get("name") or "").strip()
        if name and not NAME.match(name):
            raise ValueError("office.name: letters, digits, '_', '.', '-' only")
        planners = d.get("planners") or []
        if not isinstance(planners, list) or not all(isinstance(p, str) and p for p in planners):
            raise ValueError("office.planners must be a list of agent names")
        hqs = []
        for h in d.get("hqs") or []:
            if not isinstance(h, dict) or not NAME.match(str(h.get("name", ""))):
                raise ValueError("office.hqs: each entry needs a name (letters, digits, '_', '.', '-')")
            url = str(h.get("url") or "").rstrip("/")
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.query:
                raise ValueError(f"office.hqs[{h['name']}].url must be an http(s) dashboard address")
            if any(x.name == h["name"] for x in hqs):
                raise ValueError(f"office.hqs: {h['name']} is listed twice")
            link = str(h.get("link") or "").rstrip("/")
            if link and urlsplit(link).scheme not in ("http", "https"):
                raise ValueError(f"office.hqs[{h['name']}].link must be an http(s) address")
            unknown = set(h) - {"name", "url", "link"}
            if unknown:
                raise ValueError(f"office.hqs[{h['name']}]: unknown setting(s) {', '.join(sorted(unknown))}")
            hqs.append(OfficeHQ(h["name"], url, link or url))
        views = d.get("views", list(VIEWS))
        if not isinstance(views, list) or not views or any(v not in VIEWS for v in views) or len(set(views)) != len(views):
            raise ValueError(f"office.views must be a non-empty list of {', '.join(VIEWS)} (first = default)")
        if not isinstance(d.get("edit", False), bool):
            raise ValueError("office.edit must be true or false")
        return cls(name, planners, hqs, views, _idle(d.get("idle")), d.get("edit", False))

    def find(self, name: str) -> OfficeHQ | None:
        return next((h for h in self.hqs if h.name == name), None)


def _idle(v) -> dict:
    """office.idle: what idle agents do (walk to a bookshelf and read, sit on the sofa, chat, ...). On by default.
    `true` / `false`, or {"enabled", "activities": [...], "chat", "max_away": share of the agents away from their desks}."""
    out = {"enabled": True, "activities": [*ACTS], "chat": True, "max_away": 0.6}
    if v is None:
        return out
    if isinstance(v, bool):
        return {**out, "enabled": v}
    if not isinstance(v, dict) or set(v) - IDLE_KEYS:
        raise ValueError(f"office.idle: true, false or an object with {', '.join(sorted(IDLE_KEYS))}")
    if "enabled" in v:
        if not isinstance(v["enabled"], bool):
            raise ValueError("office.idle.enabled must be true or false")
        out["enabled"] = v["enabled"]
    if "chat" in v:
        if not isinstance(v["chat"], bool):
            raise ValueError("office.idle.chat must be true or false")
        out["chat"] = v["chat"]
    if "activities" in v:
        a = v["activities"]
        if not isinstance(a, list) or any(x not in ACTS for x in a) or len(set(a)) != len(a):
            raise ValueError(f"office.idle.activities must be a list of {', '.join(ACTS)}")
        out["activities"] = a
    if "max_away" in v:
        m = v["max_away"]
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m <= 1:
            raise ValueError("office.idle.max_away must be a number from 0 to 1")
        out["max_away"] = float(m)
    return out


def own_name(bus) -> str:
    cfg = getattr(bus, "office", None) or OfficeConfig()
    plugins = getattr(bus, "plugins", None)
    peers = plugins.service("peering") if plugins else None  # xqo-peering, when installed and configured
    return cfg.name or (peers.name if peers else "hq")


def planners(bus) -> list[str]:
    """Agents shown in the planners world: office.planners, else headquarters agents that plan."""
    cfg = getattr(bus, "office", None) or OfficeConfig()
    if cfg.planners:
        return [p for p in cfg.planners if p in bus.agents]
    hq = [a for a in bus.agents.values() if getattr(a, "department", "hq") == "hq"]
    picked = [a.name for a in hq if a.name == "planner" or "planning" in (a.skills or [])]
    return picked or [a.name for a in hq[:1]]


def _avatar(agent):
    from ...avatars import public
    return public(agent.name, getattr(agent, "avatar", None))


def describe(bus) -> dict:
    cfg = getattr(bus, "office", None) or OfficeConfig()
    plan = planners(bus)
    nicks = {n: bus.agents[n].nickname for n in plan if getattr(bus.agents[n], "nickname", None)}  # id -> nickname
    avs = {n: av for n in plan if (av := _avatar(bus.agents[n]))}  # id -> avatar (what the dashboard gets)
    return {"name": own_name(bus), "title": title(bus), "hq_name": getattr(bus, "hq_name", ""), "planners": plan,
            "nicknames": nicks, "avatars": avs,
            "hqs": [{"name": h.name, "link": h.link} for h in cfg.hqs], "editable": bool(getattr(bus, "office_edit", False)),
            "views": list(cfg.views), "idle": dict(cfg.idle)}


def relay_target(cfg: OfficeConfig, hq: str, path: str, method: str) -> str:
    """URL on another HQ's dashboard for an allowed office call; KeyError / PermissionError otherwise."""
    h = cfg.find(hq)
    if not h:
        raise KeyError(f"unknown HQ {hq} (office.hqs)")
    allowed = RELAY_GET if method == "GET" else RELAY_POST if method == "POST" else None
    if not allowed or not allowed.match(path):
        raise PermissionError(f"the office relay does not forward {method} /api/{path}")
    return f"{h.url}/api/{quote(path, safe='/:')}"


# ---------------- the edited layout: <artifacts>/office.json and <artifacts>/office-assets/ ----------------
LAYOUT_MAX = 1_000_000          # bytes of office.json
ASSET_MAX = 20_000_000          # bytes of one uploaded model or texture
ASSETS_MAX = 200                # files in office-assets/
ASSET_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}\.(glb|png|jpe?g)$")
ASSET_TYPES = {"glb": "model/gltf-binary", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
ASSET_MAGIC = {"glb": (b"glTF",), "png": (b"\x89PNG",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",)}
KEY = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")       # prop uids, zone keys (department names), agent names
TYPE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")          # catalog ids
GLB = re.compile(r"^(assets/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}\.glb|[A-Za-z0-9_-]+(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*\.glb)$")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ROTS = (0, 90, 180, 270)
ANCHORS = ("work", "rest", "social", "utility", "wander")
ACTS = ("read", "drink", "sit", "write", "tend")      # what an idle agent pretends to do at a spot ("act" on an anchor)
IDLE_KEYS = {"enabled", "activities", "chat", "max_away"}
LIMITS = {"props": 5000, "added": 2000, "rooms": 500, "agents": 2000, "catalog": 300, "anchors": 8, "paint": 20000}
PAINTS = ("block", "path")  # floor paint: no-go tiles, and corridors agents prefer


def _check_keys(d, allowed, where):
    if not isinstance(d, dict):
        raise ValueError(f"{where} must be an object")
    unknown = set(d) - set(allowed)
    if unknown:
        raise ValueError(f"{where}: unknown setting(s) {', '.join(sorted(map(str, unknown)))}")


def _num(v, lo, hi, where):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{where} must be a number from {lo} to {hi}")
    return round(float(v), 3) if v != int(v) else int(v)


def _key(v, where, pattern=KEY):
    if not isinstance(v, str) or not pattern.match(v):
        raise ValueError(f"{where}: invalid name {v!r}")
    return v


def _map(d, where, limit):
    if not isinstance(d, dict):
        raise ValueError(f"{where} must be an object")
    if len(d) > LIMITS[limit]:
        raise ValueError(f"{where}: at most {LIMITS[limit]} entries")
    return d


def _place(d, where, keys):
    """x / y (tiles, from the top-left of the prop's zone), rot (degrees clockwise, seen from above), hide."""
    _check_keys(d, keys, where)
    out = {}
    for k in ("x", "y"):
        if k in d:
            out[k] = _num(d[k], -100, 1000, f"{where}.{k}")
    if "rot" in d:
        if d["rot"] not in ROTS:
            raise ValueError(f"{where}.rot must be 0, 90, 180 or 270")
        out["rot"] = int(d["rot"])
    if "hide" in d:
        if not isinstance(d["hide"], bool):
            raise ValueError(f"{where}.hide must be true or false")
        out["hide"] = d["hide"]
    return out


def _catalog_entry(e, where):
    _check_keys(e, {"label", "glb", "builtin", "size", "blocks", "scale", "yscale", "y", "rot", "anchors", "sprite2d", "color", "tags", "layer",
                    "marker"}, where)
    out = {}
    if "label" in e:
        if not isinstance(e["label"], str) or not 0 < len(e["label"]) <= 60:
            raise ValueError(f"{where}.label: 1 to 60 characters")
        out["label"] = e["label"]
    if ("glb" in e) == ("builtin" in e):
        raise ValueError(f"{where}: needs either glb (a model file) or builtin")
    if "glb" in e:
        g = e["glb"]
        if not isinstance(g, str) or not GLB.match(g) or ".." in g:
            raise ValueError(f"{where}.glb: a .glb under /static/office3d/ or assets/<file>.glb")
        out["glb"] = g
    else:
        out["builtin"] = _key(e["builtin"], f"{where}.builtin", TYPE)
    size = e.get("size", [1, 1])
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError(f"{where}.size must be [width, depth] in tiles")
    out["size"] = [_num(v, 0.25, 40, f"{where}.size") for v in size]
    for k in ("blocks", "marker"):  # marker: drawn in Edit mode only (a waypoint)
        if k in e:
            if not isinstance(e[k], bool):
                raise ValueError(f"{where}.{k} must be true or false")
            out[k] = e[k]
    if "scale" in e:
        out["scale"] = _num(e["scale"], 0.001, 1000, f"{where}.scale")
    if "yscale" in e:
        out["yscale"] = _num(e["yscale"], 0.05, 20, f"{where}.yscale")
    if "y" in e:
        out["y"] = _num(e["y"], -10, 10, f"{where}.y")
    if "rot" in e:
        if e["rot"] not in ROTS:
            raise ValueError(f"{where}.rot must be 0, 90, 180 or 270")
        out["rot"] = int(e["rot"])
    if "anchors" in e:
        if not isinstance(e["anchors"], list) or len(e["anchors"]) > LIMITS["anchors"]:
            raise ValueError(f"{where}.anchors: a list of at most {LIMITS['anchors']}")
        out["anchors"] = []
        for i, a in enumerate(e["anchors"]):
            _check_keys(a, {"type", "ox", "oy", "act"}, f"{where}.anchors[{i}]")
            if a.get("type") not in ANCHORS:
                raise ValueError(f"{where}.anchors[{i}].type must be one of {', '.join(ANCHORS)}")
            if "act" in a and a["act"] not in ACTS:
                raise ValueError(f"{where}.anchors[{i}].act must be one of {', '.join(ACTS)}")
            out["anchors"].append({**({"act": a["act"]} if "act" in a else {}), "type": a["type"], "ox": _num(a.get("ox", 0), -5, 45, f"{where}.anchors[{i}].ox"),
                                   "oy": _num(a.get("oy", 0), -5, 45, f"{where}.anchors[{i}].oy")})
    if "sprite2d" in e:
        out["sprite2d"] = _key(e["sprite2d"], f"{where}.sprite2d", TYPE)
    if "color" in e:
        if not isinstance(e["color"], str) or not HEX.match(e["color"]):
            raise ValueError(f"{where}.color must be #rrggbb")
        out["color"] = e["color"]
    if "layer" in e:
        if e["layer"] not in ("below", "above"):
            raise ValueError(f"{where}.layer must be below or above")
        out["layer"] = e["layer"]
    if "tags" in e:
        if not isinstance(e["tags"], list) or len(e["tags"]) > 8 or not all(isinstance(t, str) and TYPE.match(t) for t in e["tags"]):
            raise ValueError(f"{where}.tags: up to 8 short words")
        out["tags"] = list(e["tags"])
    return out


def validate_layout(d) -> dict:
    """Check an office.json document and return it normalised. ValueError says what is wrong.

    {"version": 1,
     "theme":   {"floor": "#rrggbb", "wall": "#rrggbb", "background": "#rrggbb"},
     "rooms":   {"<department>": {"size": [w, h]}},                 # a room bigger or smaller than automatic
     "props":   {"<uid>": {"x": 2, "y": 1, "rot": 90, "hide": false}},   # changes to automatic props, by uid
     "added":   [{"uid": "u-1", "type": "<catalog id>", "zone": "<department>|@lounge|@building", "x": 3, "y": 2, "rot": 0}],
     "agents":  {"<agent>": {"character": "<character id>", "desk": "<uid of a prop with a work spot>"}},
     "paint":   {"<department>|@lounge|@building": {"block": [[x, y], ...], "path": [[x, y], ...]}},  # no-go and corridor tiles
     "catalog": {"props": {"<id>": {"label", "glb" | "builtin", "size": [w, d], "blocks", "scale", "yscale", "y", "rot",
                                    "anchors": [{"type": "work|rest|social|utility|wander", "ox", "oy", "act": "read|drink|sit|write|tend"}], "sprite2d", "color", "tags"}},
                 "characters": {"<id>": {"label", "glb"}}}}

    x / y (of props and paint) are tiles from the top-left corner of the prop's zone (its department's room, the lounge along the
    bottom, or the whole building), so a room keeps its furniture when the automatic layout moves it.
    """
    if d is None:
        d = {}
    _check_keys(d, {"version", "theme", "rooms", "props", "added", "agents", "paint", "catalog"}, "office layout")
    if d.get("version", 1) != 1:
        raise ValueError("office layout: only version 1 is understood")
    out: dict = {"version": 1}
    if "theme" in d:
        _check_keys(d["theme"], {"floor", "wall", "background"}, "theme")
        out["theme"] = {}
        for k, v in d["theme"].items():
            if not isinstance(v, str) or not HEX.match(v):
                raise ValueError(f"theme.{k} must be #rrggbb")
            out["theme"][k] = v
    if "rooms" in d:
        out["rooms"] = {}
        for k, r in _map(d["rooms"], "rooms", "rooms").items():
            _key(k, "rooms")
            _check_keys(r, {"size"}, f"rooms[{k}]")
            room = {}
            if "size" in r:
                if not isinstance(r["size"], list) or len(r["size"]) != 2:
                    raise ValueError(f"rooms[{k}].size must be [width, height] in tiles")
                room["size"] = [int(_num(v, 4, 200, f"rooms[{k}].size")) for v in r["size"]]
            out["rooms"][k] = room
    if "props" in d:
        out["props"] = {_key(k, "props"): _place(v, f"props[{k}]", {"x", "y", "rot", "hide"})
                        for k, v in _map(d["props"], "props", "props").items()}
    if "added" in d:
        if not isinstance(d["added"], list) or len(d["added"]) > LIMITS["added"]:
            raise ValueError(f"added: a list of at most {LIMITS['added']} props")
        out["added"], seen = [], set()
        for i, a in enumerate(d["added"]):
            where = f"added[{i}]"
            _check_keys(a, {"uid", "type", "zone", "x", "y", "rot"}, where)
            uid = _key(a.get("uid"), f"{where}.uid")
            if uid in seen:
                raise ValueError(f"{where}: uid {uid} is used twice")
            seen.add(uid)
            p = {"uid": uid, "type": _key(a.get("type"), f"{where}.type", TYPE), "zone": _key(a.get("zone", "@building"), f"{where}.zone"),
                 **_place({k: v for k, v in a.items() if k in ("x", "y", "rot")}, where, {"x", "y", "rot"})}
            p.setdefault("x", 0)
            p.setdefault("y", 0)
            p.setdefault("rot", 0)
            out["added"].append(p)
    if "agents" in d:
        out["agents"] = {}
        for k, v in _map(d["agents"], "agents", "agents").items():
            _key(k, "agents")
            _check_keys(v, {"character", "desk"}, f"agents[{k}]")
            out["agents"][k] = {}
            if "character" in v:
                out["agents"][k]["character"] = _key(v["character"], f"agents[{k}].character", TYPE)
            if "desk" in v:
                out["agents"][k]["desk"] = _key(v["desk"], f"agents[{k}].desk")
    if "paint" in d:
        out["paint"], n = {}, 0
        for k, z in _map(d["paint"], "paint", "rooms").items():
            _key(k, "paint")
            _check_keys(z, set(PAINTS), f"paint[{k}]")
            zone = {}
            for kind in PAINTS:
                if kind not in z:
                    continue
                tiles = z[kind]
                if not isinstance(tiles, list):
                    raise ValueError(f"paint[{k}].{kind} must be a list of [x, y] tiles")
                n += len(tiles)
                if n > LIMITS["paint"]:
                    raise ValueError(f"paint: at most {LIMITS['paint']} tiles")
                seen = set()
                for i, t in enumerate(tiles):
                    if not isinstance(t, list) or len(t) != 2:
                        raise ValueError(f"paint[{k}].{kind}[{i}] must be [x, y]")
                    xy = tuple(int(_num(v, -100, 1000, f"paint[{k}].{kind}[{i}]")) for v in t)
                    if xy != tuple(t):
                        raise ValueError(f"paint[{k}].{kind}[{i}] must be whole tiles")
                    seen.add(xy)
                zone[kind] = sorted([list(t) for t in seen])
            if zone:
                out["paint"][k] = zone
    if "catalog" in d:
        _check_keys(d["catalog"], {"props", "characters"}, "catalog")
        cat = {}
        if "props" in d["catalog"]:
            cat["props"] = {_key(k, "catalog.props", TYPE): _catalog_entry(v, f"catalog.props[{k}]")
                            for k, v in _map(d["catalog"]["props"], "catalog.props", "catalog").items()}
        if "characters" in d["catalog"]:
            cat["characters"] = {}
            for k, v in _map(d["catalog"]["characters"], "catalog.characters", "catalog").items():
                _key(k, "catalog.characters", TYPE)
                _check_keys(v, {"label", "glb"}, f"catalog.characters[{k}]")
                g = v.get("glb")
                if not isinstance(g, str) or not GLB.match(g) or ".." in g:
                    raise ValueError(f"catalog.characters[{k}].glb: a .glb under /static/office3d/ or assets/<file>.glb")
                c = {"glb": g}
                if "label" in v:
                    if not isinstance(v["label"], str) or not 0 < len(v["label"]) <= 60:
                        raise ValueError(f"catalog.characters[{k}].label: 1 to 60 characters")
                    c["label"] = v["label"]
                cat["characters"][k] = c
        out["catalog"] = cat
    return out


class LayoutStore:
    """This HQ's edited office: office.json and the custom models in office-assets/, next to the artifacts."""

    def __init__(self, root: str | os.PathLike):
        from pathlib import Path
        self.root = Path(root)
        self.path = self.root / "office.json"
        self.assets = self.root / "office-assets"

    def rev(self) -> str:
        try:
            return str(self.path.stat().st_mtime_ns)
        except OSError:
            return ""

    def load(self) -> dict:
        if not self.path.is_file():
            return {"version": 1}
        try:
            return validate_layout(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:  # a hand-edited file with a mistake: the office still opens, automatic
            return {"version": 1, "error": f"office.json is ignored: {e}"}

    def save(self, doc) -> dict:
        doc = validate_layout(doc)
        text = json.dumps(doc, indent=1, ensure_ascii=False)
        if len(text.encode()) > LAYOUT_MAX:
            raise ValueError(f"office layout: larger than {LAYOUT_MAX // 1000} KB")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        return doc

    def list_assets(self) -> list[dict]:
        if not self.assets.is_dir():
            return []
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(self.assets.iterdir())
                if p.is_file() and ASSET_NAME.match(p.name)]

    def asset(self, name: str):
        """Path of an uploaded file, or None."""
        if not ASSET_NAME.match(name):
            return None
        p = self.assets / name
        return p if p.is_file() else None

    def save_asset(self, name: str, data: bytes) -> dict:
        if not ASSET_NAME.match(name):
            raise ValueError("model name: letters, digits, '_', '.', '-', ending in .glb, .png or .jpg")
        if len(data) > ASSET_MAX:
            raise ValueError(f"{name}: larger than {ASSET_MAX // 1_000_000} MB")
        ext = name.rsplit(".", 1)[1].lower()
        if not data.startswith(ASSET_MAGIC[ext]):
            raise ValueError(f"{name}: not a {'binary glTF (.glb)' if ext == 'glb' else ext.upper()} file")
        if not (self.assets / name).exists() and len(self.list_assets()) >= ASSETS_MAX:
            raise ValueError(f"office-assets: at most {ASSETS_MAX} files")
        self.assets.mkdir(parents=True, exist_ok=True)
        tmp = self.assets / f".{name}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, self.assets / name)
        return {"name": name, "size": len(data)}

    def delete_asset(self, name: str) -> bool:
        p = self.asset(name)
        if not p:
            return False
        p.unlink()
        return True
