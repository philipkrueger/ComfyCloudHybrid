"""V3 extension entrypoint: scan blueprints, generate nodes, register routes.

Rules honored here:
- No network at import/startup — conversion uses the cached cloud catalog
  (refresh happens in a background task after startup).
- Fail-soft per blueprint: one bad file must never break the pack.
"""

from __future__ import annotations

import asyncio
import logging

from comfy_api.latest import ComfyExtension, io

from . import config, rescan, routes
from .node_factory import make_blueprint_node
from .nodes_generic import CloudHybridRunWorkflow
from .scanner import scan

log = logging.getLogger("ComfyCloudHybrid")

routes.register()


class CloudHybridExtension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        node_classes: list[type[io.ComfyNode]] = [CloudHybridRunWorkflow]
        schemas = rescan.build_schema_source()
        register_unavailable = bool(config.get("register_unavailable"))
        skip_local = bool(config.get("skip_local_capable"))
        for bp in scan():
            try:
                cw = rescan.convert_cached(bp, schemas)
                if skip_local and cw.local_capable:
                    log.info("blueprint %s skipped (model-free — runs locally, "
                             "cloud offload is pointless)", bp.name)
                    continue
                if cw.missing_classes and not register_unavailable:
                    log.info("blueprint %s skipped (missing in cloud: %s)",
                             bp.name, ", ".join(cw.missing_classes))
                    continue
                node_classes.append(make_blueprint_node(bp, cw))
                routes.KNOWN_SLUGS.add(bp.slug)
            except Exception as e:
                log.warning("blueprint %s (%s) skipped: %s", bp.name, bp.path, e)
        log.info("ComfyCloudHybrid: %d cloud node(s) registered", len(node_classes) - 1)

        # refresh the cloud node catalog in the background (never blocks startup)
        try:
            asyncio.create_task(rescan.refresh_object_info())
        except RuntimeError:
            pass
        return node_classes


async def comfy_entrypoint() -> CloudHybridExtension:
    return CloudHybridExtension()
