"""Scan + convert pipeline, shared by startup and the /cloudhybrid/rescan route."""

from __future__ import annotations

import asyncio
import logging

from . import cache, config
from .cloud_client import ComfyCloudClient
from .converter import CONVERTER_VERSION, SchemaSource, convert
from .converter.model import ConvertedWorkflow
from .scanner import BlueprintSource, load_blueprint, scan

log = logging.getLogger("ComfyCloudHybrid")


def convert_cached(bp: BlueprintSource, schemas: SchemaSource) -> ConvertedWorkflow:
    cached = cache.load_converted(bp.slug, source_sha256=bp.sha256,
                                  converter_version=CONVERTER_VERSION)
    if cached is not None:
        return cached
    cw = convert(load_blueprint(bp.path), schemas, fallback_name=bp.name)
    cache.save_converted(bp.slug, bp.sha256, CONVERTER_VERSION, cw,
                         had_cloud_catalog=schemas.has_cloud_catalog)
    return cw


def build_schema_source() -> SchemaSource:
    return SchemaSource(cache.load_object_info(), use_local=True)


async def refresh_object_info() -> bool:
    """Fetch the cloud node catalog into the cache. False when no key/offline."""
    api_key = config.get_api_key()
    if not api_key:
        return False
    try:
        async with ComfyCloudClient(api_key) as client:
            info = await client.object_info()
        cache.save_object_info(info)
        log.info("cloud object_info cached (%d node classes)", len(info))
        return True
    except Exception as e:
        log.warning("could not refresh cloud object_info: %s", e)
        return False


async def rescan(known_slugs: set[str] | None = None) -> dict:
    """Re-run scan+convert. Returns a summary for the route/frontend."""
    if cache.load_object_info() is None:
        await refresh_object_info()
    schemas = build_schema_source()
    blueprints = await asyncio.to_thread(scan)
    updated, new, failed = [], [], []
    for bp in blueprints:
        try:
            fresh = cache.load_converted(bp.slug, source_sha256=bp.sha256,
                                         converter_version=CONVERTER_VERSION) is None
            await asyncio.to_thread(convert_cached, bp, schemas)
            if known_slugs is not None and bp.slug not in known_slugs:
                new.append(bp.slug)
            elif fresh:
                updated.append(bp.slug)
        except Exception as e:
            failed.append({"slug": bp.slug, "path": bp.path, "error": str(e)})
            log.warning("blueprint %s failed to convert: %s", bp.path, e)
    restart_required = bool(new)
    return {
        "found": [bp.slug for bp in blueprints],
        "updated": updated,
        "new": new,
        "failed": failed,
        "restart_required": restart_required,
        "message": ("Neue Blueprints gefunden — ComfyUI-Neustart nötig, damit die "
                    "neuen Nodes erscheinen." if restart_required
                    else "Blueprints aktualisiert — bestehende Nodes nutzen die "
                         "neuen Konvertierungen sofort."),
    }
