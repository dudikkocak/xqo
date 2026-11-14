"""Office plugin: the /office page, the HQ drawn as an office (2D pixel office and/or 3D three.js), its editor
(`serve --edit`) and the planners world across HQs. On by default; "office": {"views": ["2d"]} keeps it 2D only
(three.js is then never loaded), and "plugins": {"disable": ["office"]} removes the page and its 4 MB of assets."""
from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from ...branding import named_page
from ...plugin import Plugin
from ...web import AttachmentIn
from . import view as office_view
from .view import OfficeConfig

STATIC = Path(__file__).parent / "static"


class OfficePlugin(Plugin):
    name = "office"
    description = "The /office page: 2D / 3D office view, editor, planners world"
    service = "office"
    config_keys = ("office",)
    static = ("/plugins/office", STATIC)
    nav = {"id": "officelink", "label": "🏢 office", "href": "/office",
           "title": "The HQ as an office: desks and agents at work; click an agent to talk to it"}

    def configure(self, ctx, cfg):
        return OfficeConfig.from_dict(cfg.get("office"))

    def describe(self) -> dict:
        return office_view.describe(self.ctx.bus)

    def status(self) -> dict:
        return {"views": list(self.obj.views), "hqs": [h.name for h in self.obj.hqs]}

    def routes(self, app, bus) -> None:
        @app.get("/office", response_class=HTMLResponse)
        async def office_page():
            return named_page(STATIC / "office.html", bus)

        layouts = office_view.LayoutStore(bus.artifacts.root)

        def office_editing() -> None:
            """Changing the office layout (and its models) needs `serve --edit`; reading it does not."""
            if not getattr(bus, "office_edit", False):
                raise HTTPException(403, "office editing is off: start the server with `serve --edit`")

        @app.get("/api/office")
        async def office_info():
            return {**office_view.describe(bus), "layout_rev": layouts.rev()}

        @app.get("/api/office/layout")
        async def office_layout():
            """This HQ's edited office (office.json), applied by the page on top of the automatic layout."""
            return {"layout": layouts.load(), "rev": layouts.rev(), "assets": layouts.list_assets()}

        @app.put("/api/office/layout")
        async def office_layout_save(request: Request, rev: str | None = None, check: bool = False):
            """Save the office from the page's Edit mode. ?rev= is the version the page started from: 409 if it changed.
            ?check=true only validates (an imported file) and returns it normalised."""
            office_editing()
            body = await request.body()
            if len(body) > office_view.LAYOUT_MAX:
                raise HTTPException(413, f"the office layout is larger than {office_view.LAYOUT_MAX // 1000} KB")
            if check:
                try:
                    return {"layout": office_view.validate_layout(json.loads(body or b"{}")), "rev": layouts.rev()}
                except ValueError as e:
                    raise HTTPException(400, str(e))
            if rev is not None and rev != layouts.rev():
                raise HTTPException(409, "the office was changed meanwhile (another tab or person): reload it, then edit again")
            try:
                doc = layouts.save(json.loads(body or b"{}"))
            except ValueError as e:  # json.JSONDecodeError is a ValueError too
                raise HTTPException(400, str(e))
            return {"layout": doc, "rev": layouts.rev()}

        @app.get("/api/office/assets")
        async def office_assets():
            return layouts.list_assets()

        @app.get("/api/office/assets/{name}")
        async def office_asset(name: str):
            p = layouts.asset(name)
            if not p:
                raise HTTPException(404)
            return FileResponse(p, media_type=office_view.ASSET_TYPES[name.rsplit(".", 1)[1].lower()],
                                headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
                                         "Cache-Control": "no-cache"})

        @app.post("/api/office/assets")
        async def office_asset_upload(a: AttachmentIn):
            """A custom model (.glb) or texture for the office, kept in <artifacts>/office-assets/."""
            office_editing()
            if len(a.data) > office_view.ASSET_MAX * 4 // 3 + 8:
                raise HTTPException(413, f"larger than {office_view.ASSET_MAX // 1_000_000} MB")
            try:
                data = base64.b64decode(a.data.split(",", 1)[-1], validate=True)
                return layouts.save_asset(a.name, data)
            except (binascii.Error, ValueError) as e:
                raise HTTPException(400, str(e))

        @app.delete("/api/office/assets/{name}")
        async def office_asset_delete(name: str):
            office_editing()
            if not layouts.delete_asset(name):
                raise HTTPException(404)
            return {"deleted": name}

        def office_url(hq: str, path: str, method: str) -> str:
            try:
                return office_view.relay_target(getattr(bus, "office", None) or office_view.OfficeConfig(), hq, path, method)
            except KeyError as e:
                raise HTTPException(404, str(e.args[0]))
            except PermissionError as e:
                raise HTTPException(403, str(e))

        @app.get("/api/office/hq/{hq}/events")
        async def office_events(hq: str):
            """Another HQ's live events, relayed for the planners world."""
            url = office_url(hq, "office", "GET").rsplit("/", 1)[0] + "/events"

            async def gen():
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=60)) as client:
                        async with client.stream("GET", url) as r:
                            if r.status_code != 200:
                                raise httpx.HTTPError(f"HTTP {r.status_code}")
                            yield "event: relay\ndata: {\"up\": true}\n\n"
                            async for line in r.aiter_lines():
                                yield line + "\n"
                except Exception as e:  # the browser reconnects by itself
                    yield f"event: relay\ndata: {json.dumps({'up': False, 'error': f'{type(e).__name__}: {e}'})}\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        @app.api_route("/api/office/hq/{hq}/{path:path}", methods=["GET", "POST"])
        async def office_relay(hq: str, path: str, request: Request):
            """The few dashboard calls the office page makes, forwarded to another HQ listed in office.hqs."""
            url = office_url(hq, path, request.method)
            body = await request.body() if request.method == "POST" else None
            if body and len(body) > office_view.RELAY_MAX_BODY:
                raise HTTPException(413, "too large for the office relay")
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.request(request.method, url, params=request.query_params, content=body,
                                             headers={"Content-Type": "application/json"} if body else None)
            except httpx.HTTPError as e:
                raise HTTPException(502, f"{hq} is not reachable: {type(e).__name__}")
            return Response(r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type", "application/json"))
