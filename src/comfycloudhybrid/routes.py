"""Custom server routes under /cloudhybrid/* (also served under /api/…).

register() is idempotent and must be called at import time of extension.py —
PromptServer binds its route table when the app starts.
"""

from __future__ import annotations

import logging

from . import cache, config, rescan as rescan_mod
from .cloud_client import CloudError, ComfyCloudClient
from .converter import CONVERTER_VERSION

log = logging.getLogger("ComfyCloudHybrid")

_registered = False
KNOWN_SLUGS: set[str] = set()  # filled by extension.py after node generation


def register() -> None:
    global _registered
    if _registered:
        return
    try:
        from aiohttp import web
        from server import PromptServer
        routes = PromptServer.instance.routes
    except (ImportError, AttributeError):
        log.info("PromptServer not available — routes skipped (offline/test mode)")
        return

    @routes.get("/cloudhybrid/status")
    async def status(request):
        return web.json_response({
            "key_source": config.get_api_key_source(),
            "masked": config.masked_key(),
            "poll_interval_s": config.get("poll_interval_s"),
            "job_timeout_s": config.get("job_timeout_s"),
            "queue_timeout_s": config.get("queue_timeout_s"),
        })

    @routes.post("/cloudhybrid/api_key")
    async def set_key(request):
        data = await request.json()
        key = (data.get("api_key") or "").strip()
        config.set_api_key(key or None)
        return web.json_response({"ok": True, "masked": config.masked_key(),
                                  "key_source": config.get_api_key_source()})

    @routes.delete("/cloudhybrid/api_key")
    async def delete_key(request):
        config.set_api_key(None)
        return web.json_response({"ok": True})

    @routes.post("/cloudhybrid/config")
    async def set_config(request):
        data = await request.json()
        applied = {}
        for name in ("poll_interval_s", "job_timeout_s", "queue_timeout_s",
                     "register_unavailable", "skip_local_capable"):
            if name in data:
                config.set_option(name, data[name])
                applied[name] = data[name]
        return web.json_response({"ok": True, "applied": applied})

    @routes.post("/cloudhybrid/test")
    async def test_connection(request):
        api_key = config.get_api_key()
        if not api_key:
            return web.json_response({"ok": False,
                                      "message": "Kein API-Key konfiguriert."})
        try:
            async with ComfyCloudClient(api_key) as client:
                user = await client.user_status()
            return web.json_response({
                "ok": True,
                "message": f"Verbunden — Account-Status: {user.get('status', '?')}."})
        except CloudError as e:
            return web.json_response({"ok": False, "message": e.user_message})
        except Exception as e:
            return web.json_response({"ok": False,
                                      "message": f"Verbindung fehlgeschlagen: {e}"})

    @routes.post("/cloudhybrid/rescan")
    async def do_rescan(request):
        # KNOWN_SLUGS is authoritative after startup — an empty set means
        # "no blueprint nodes registered", so every found slug counts as new
        result = await rescan_mod.rescan(known_slugs=KNOWN_SLUGS)
        return web.json_response(result)

    @routes.get("/cloudhybrid/blueprints")
    async def list_blueprints(request):
        from .scanner import scan
        entries = []
        for bp in scan():
            cw = cache.load_converted(bp.slug, source_sha256=bp.sha256,
                                      converter_version=CONVERTER_VERSION)
            entries.append({
                "slug": bp.slug, "name": bp.name, "source": bp.source_kind,
                "path": bp.path, "category": bp.category,
                "available": bool(cw and not cw.missing_classes) if cw else None,
                "local_capable": cw.local_capable if cw else None,
                "missing_classes": cw.missing_classes if cw else None,
                "inputs": [{"name": i.name, "type": i.type, "kind": i.kind}
                           for i in cw.inputs] if cw else None,
                "outputs": [{"name": o.name, "type": o.type}
                            for o in cw.outputs] if cw else None,
            })
        return web.json_response(entries)

    _registered = True
    log.info("routes registered under /cloudhybrid/*")
