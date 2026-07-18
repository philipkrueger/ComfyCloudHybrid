"""Blueprint discovery across all sources.

Sources (precedence high → low, dedupe by subgraph definition UUID):
1. user      — <ComfyUI>/user/<id>/subgraphs/*.json (published blueprints)
2. custom    — <ComfyUI>/custom_nodes/*/subgraphs/*.json
3. shipped   — <ComfyUI>/blueprints/*.json (newer ComfyUI versions)
4. curated   — <pack>/blueprints_curated/**/*.json (synced Comfy-Org collection;
               blocked/ excluded at sync time, cloud_only/ is the point of this pack)
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass

from . import config

log = logging.getLogger("ComfyCloudHybrid")


@dataclass
class BlueprintSource:
    path: str
    source_kind: str        # user | custom_nodes | shipped | curated
    name: str               # subgraph definition name (or filename stem)
    def_uuid: str           # root subgraph definition id
    sha256: str             # file content hash
    slug: str               # stable node identity
    subcategory: str = ""   # curated subfolder, e.g. "cloud_only"
    category: str = ""      # taxonomy from the subgraph definition itself,
                            # e.g. "Image Tools/Color adjust"


def _slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "blueprint"
    return s[:48]


def category_group(name: str) -> str:
    """Task-style folder derived from the blueprint naming convention.

    The blueprint collection encodes its taxonomy in the name prefix before
    the parenthesized variant — "Text to Image (Flux.2 Dev)" → "Text to
    Image". Names without a variant suffix ("Sharpen") stay at the root."""
    base = re.split(r"\s*[\(\[]", str(name), 1)[0].strip()
    if base and base != str(name).strip():
        return base
    return ""


def _probe(path: str, source_kind: str, subcategory: str = "") -> BlueprintSource | None:
    try:
        with open(path, "rb") as f:
            raw = f.read()
        data = json.loads(raw)
        defs = {d["id"]: d for d in (data.get("definitions") or {}).get("subgraphs") or []}
        instances = [n for n in data.get("nodes") or [] if n.get("type") in defs]
        if len(instances) != 1:
            log.debug("skipping %s: not a single-instance blueprint", path)
            return None
        def_uuid = instances[0]["type"]
        root_def = defs[def_uuid]
        name = root_def.get("name") or os.path.splitext(os.path.basename(path))[0]
        slug = f"{_slugify(name)}_{hashlib.sha1(def_uuid.encode()).hexdigest()[:6]}"
        return BlueprintSource(
            path=path, source_kind=source_kind, name=name, def_uuid=def_uuid,
            sha256=hashlib.sha256(raw).hexdigest(), slug=slug,
            subcategory=subcategory,
            category=str(root_def.get("category") or "").strip("/ "))
    except Exception as e:
        log.warning("skipping unreadable blueprint %s: %s", path, e)
        return None


def _comfy_paths():
    """(user_subgraph_dirs, custom_node_dirs, shipped_dir) — empty when not
    running inside ComfyUI."""
    try:
        import folder_paths
    except ImportError:
        return [], [], None
    user_dir = folder_paths.get_user_directory()
    user_dirs = glob.glob(os.path.join(user_dir, "*", "subgraphs"))
    custom_dirs = list(folder_paths.get_folder_paths("custom_nodes"))
    shipped = os.path.join(folder_paths.base_path, "blueprints")
    return user_dirs, custom_dirs, shipped if os.path.isdir(shipped) else None


def scan() -> list[BlueprintSource]:
    sources_cfg = config.get("sources")
    found: list[BlueprintSource] = []
    user_dirs, custom_dirs, shipped = _comfy_paths()

    if sources_cfg.get("user", True):
        for d in user_dirs:
            for p in sorted(glob.glob(os.path.join(d, "*.json"))):
                bp = _probe(p, "user")
                if bp:
                    found.append(bp)

    if sources_cfg.get("custom_nodes", True):
        for base in custom_dirs:
            for p in sorted(glob.glob(os.path.join(base, "*", "subgraphs", "*.json"))):
                bp = _probe(p, "custom_nodes")
                if bp:
                    found.append(bp)

    if sources_cfg.get("comfyui_blueprints", True) and shipped:
        for p in sorted(glob.glob(os.path.join(shipped, "*.json"))):
            bp = _probe(p, "shipped")
            if bp:
                found.append(bp)

    if sources_cfg.get("curated", True) and config.CURATED_DIR.is_dir():
        for p in sorted(glob.glob(str(config.CURATED_DIR / "**" / "*.json"),
                                  recursive=True)):
            rel = os.path.relpath(p, config.CURATED_DIR)
            sub = os.path.dirname(rel)
            if sub.split(os.sep)[0] == "blocked":
                continue
            bp = _probe(p, "curated", subcategory=sub)
            if bp:
                found.append(bp)

    # dedupe by definition UUID — first occurrence wins (list order == precedence)
    seen: set[str] = set()
    result = []
    for bp in found:
        if bp.def_uuid in seen:
            log.info("blueprint %s (%s) shadowed by higher-precedence source",
                     bp.name, bp.source_kind)
            continue
        seen.add(bp.def_uuid)
        result.append(bp)
    log.info("blueprint scan: %d found (%s)", len(result),
             ", ".join(f"{bp.name}[{bp.source_kind}]" for bp in result) or "none")
    return result


def load_blueprint(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
