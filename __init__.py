"""ComfyCloudHybrid — run Subgraph Blueprints on Comfy Cloud.

ComfyUI imports this file. V3 extension entrypoint only (no V1 mappings:
ComfyUI would ignore comfy_entrypoint if NODE_CLASS_MAPPINGS existed).
"""

from .src.comfycloudhybrid.extension import comfy_entrypoint

WEB_DIRECTORY = "./web/js"

__all__ = ["comfy_entrypoint", "WEB_DIRECTORY"]
