"""Revision 1 web API (R2): JSON endpoints and Server-Sent Events on the stream server's app.

LAN only, no authentication (decided for revision 1). Endpoints:

  GET  /api/status        controller state, speaker, signal, level, codec, encoder, clients
  GET  /api/devices       live Cast devices and groups (continuous discovery)
  POST /api/play          {"target": "<name>"}? -> manual play (sets the last speaker)
  POST /api/stop          manual stop (holds auto-cast until the record ends)
  POST /api/target        {"target": "<name>"} -> speaker for the next needle drop (no play)
  POST /api/volume        {"level": 0..1} -> speaker volume
  GET  /api/settings      settings the UI may change, with current values
  POST /api/settings      {key: value, ...} -> validated, saved to state.json, applied
  GET  /api/diagnostics   recent events, Pi health (Wi-Fi, throttling), CPU, notifications
  GET  /api/events        Server-Sent Events: `status` on change, `level` at 4 Hz
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from importlib import resources
from typing import Any

from aiohttp import web

from vinylcast.cast import Target
from vinylcast.config import UI_SETTABLE, ConfigError
from vinylcast.driver import Driver
from vinylcast.server import stream_url

LEVEL_INTERVAL_S = 0.25  # 4 Hz for the meter
INDEX_HTML = (resources.files("vinylcast") / "webui" / "index.html").read_bytes()
HEARTBEAT_S = 15.0


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, default=str))


def _error(message: str, status: int = 400) -> web.Response:
    return _json({"error": message}, status)


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise web.HTTPBadRequest(text=json.dumps({"error": f"invalid JSON: {e}"}),
                                 content_type="application/json") from e  # fmt: skip
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "expected a JSON object"}),
                                 content_type="application/json")  # fmt: skip
    return data


class WebApi:
    def __init__(self, service: Any, driver: Driver, devices: Callable[[], list[Target]]) -> None:
        self.service = service
        self.driver = driver
        self.devices = devices
        self.started = time.monotonic()

    def install(self, app: web.Application) -> None:
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/api/status", self.status)
        r.add_get("/api/devices", self.list_devices)
        r.add_post("/api/play", self.play)
        r.add_post("/api/stop", self.stop)
        r.add_post("/api/target", self.target)
        r.add_post("/api/volume", self.volume)
        r.add_get("/api/settings", self.get_settings)
        r.add_post("/api/settings", self.post_settings)
        r.add_get("/api/diagnostics", self.diagnostics)
        r.add_get("/api/events", self.events)

    # ------------------------------------------------------------- snapshots

    def snapshot(self) -> dict[str, Any]:
        cfg = self.service.cfg
        hub = self.service.hub.stats()
        return {
            **self.driver.status(),
            "codec": cfg.codec,
            "stream_url": stream_url(cfg),
            "encoder": self.service.encoder.status(),
            "clients": hub["clients"],
            "uptime_s": round(time.monotonic() - self.started, 1),
        }

    def settings(self) -> dict[str, Any]:
        cfg = self.service.cfg
        return {k: getattr(cfg, k) for k in sorted(UI_SETTABLE)}

    # ------------------------------------------------------------- handlers

    async def index(self, request: web.Request) -> web.Response:
        """The phone UI: one self-contained HTML file (no external resources)."""
        return web.Response(body=INDEX_HTML, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-cache"})  # fmt: skip

    async def status(self, request: web.Request) -> web.Response:
        return _json(self.snapshot())

    async def list_devices(self, request: web.Request) -> web.Response:
        current = self.driver.controller.target
        return _json([
            {"name": t.name, "kind": t.kind, "model": t.model, "host": t.host,
             "port": t.port, "selected": t.name == current}
            for t in self.devices()
        ])  # fmt: skip

    async def play(self, request: web.Request) -> web.Response:
        body = await _body(request)
        target = body.get("target")
        if target is not None and not isinstance(target, str):
            return _error("target must be a string")
        self.driver.play(target)
        return _json(self.snapshot(), 202)

    async def target(self, request: web.Request) -> web.Response:
        name = (await _body(request)).get("target")
        if not isinstance(name, str) or not name.strip():
            return _error("target must be a speaker name")
        self.driver.set_target(name)
        return _json(self.snapshot())

    async def stop(self, request: web.Request) -> web.Response:
        self.driver.stop()
        return _json(self.snapshot(), 202)

    async def volume(self, request: web.Request) -> web.Response:
        level = (await _body(request)).get("level")
        if isinstance(level, bool) or not isinstance(level, int | float) or not 0 <= level <= 1:
            return _error("level must be a number between 0 and 1")
        result = await self.driver.set_volume(float(level))
        if result is None:
            return _error("nothing is playing; volume can only be set on the active speaker", 409)
        return _json({"level": result})

    async def get_settings(self, request: web.Request) -> web.Response:
        return _json(self.settings())

    async def post_settings(self, request: web.Request) -> web.Response:
        body = await _body(request)
        try:
            await self.driver.apply_settings(body)
        except ConfigError as e:
            return _error(str(e))
        return _json(self.settings())

    async def diagnostics(self, request: web.Request) -> web.Response:
        m = self.driver.metrics
        return _json({
            "health": m.latest.get("health"),
            "sample": m.latest.get("sample"),
            "notifications": list(self.driver.notifications),
            "events": [e for e in list(m.recent) if e.get("event") != "http_request"][-50:],
        })  # fmt: skip

    async def events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache"})  # fmt: skip
        await resp.prepare(request)
        last_status: str | None = None
        last_level_t: float | None = None
        last_beat = time.monotonic()
        with contextlib.suppress(ConnectionError, asyncio.CancelledError):
            while True:
                snap = self.snapshot()
                level = snap.pop("level")
                snap.pop("uptime_s")
                status = json.dumps(snap, default=str, sort_keys=True)
                if status != last_status:
                    await resp.write(f"event: status\ndata: {status}\n\n".encode())
                    last_status = status
                latest = self.driver.detector.latest
                if latest is not None and latest.t != last_level_t and level is not None:
                    await resp.write(f"event: level\ndata: {json.dumps(level)}\n\n".encode())
                    last_level_t = latest.t
                if time.monotonic() - last_beat >= HEARTBEAT_S:
                    await resp.write(b": keep-alive\n\n")
                    last_beat = time.monotonic()
                await asyncio.sleep(LEVEL_INTERVAL_S)
        return resp
