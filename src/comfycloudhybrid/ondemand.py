"""On-demand conversion of a canvas subgraph into a Comfy Cloud node.

Backs the right-click "Convert to Cloud API Node" entry. Two modes, both
funnelling through the existing converter (converter/flatten.py):

  * "test" — validate and return a generic-runner-ready prompt so the frontend
    can drop a pre-filled  ☁ Cloud Workflow ausführen  node immediately. No new
    node class is registered, so no ComfyUI restart is needed.
  * "save" — persist the subgraph as a blueprint under saved_blueprints/. A
    named cloud node then appears after the next ComfyUI restart (a new node
    class cannot be registered into a running session).

Guiding rule for the error management: a subgraph that would yield a
DYSFUNCTIONAL node is never converted. preflight() classifies every problem as
either a hard *error* (blocks generation) or a *warning* (works, but read it),
and returns one structured report the frontend renders as hints + error list.
"""

from __future__ import annotations

import copy
import json
import logging
import traceback

from . import config
from .converter import convert
from .converter.model import (BlueprintFormatError, UnsupportedTypeError,
                              ConvertedWorkflow)
from .converter.schema_source import SchemaSource

log = logging.getLogger("ComfyCloudHybrid")

# the generic runner exposes exactly four image inputs (see nodes_generic.py)
GENERIC_TOKENS = ["%CCH_IMAGE_1%", "%CCH_IMAGE_2%", "%CCH_IMAGE_3%", "%CCH_IMAGE_4%"]
MAX_GENERIC_IMAGES = len(GENERIC_TOKENS)

# on an unexpected conversion crash the exact payload is preserved here so a
# bug report carries the real live-frontend structure, not a guess
DEBUG_DUMP = config.CACHE_DIR / "convert_debug.json"

_LINK_KEYS = ("id", "origin_id", "origin_slot", "target_id", "target_slot", "type")


def _normalize_blueprint(bp: dict) -> dict:
    """Accept the live frontend's serialisation, not only blueprint files.

    LiteGraph's Graph.serialize() emits links as positional arrays
    [id, origin_id, origin_slot, target_id, target_slot, type]; blueprint
    files (and the converter) use dicts with those keys. Normalise every
    links list — root and all subgraph definitions, recursively (nested
    subgraphs carry their own definitions block)."""
    def fix_links(container: dict) -> None:
        links = container.get("links")
        if isinstance(links, list):
            container["links"] = [
                dict(zip(_LINK_KEYS, l)) if isinstance(l, (list, tuple)) and len(l) >= 6
                else l
                for l in links]

    def walk(container: dict) -> None:
        fix_links(container)
        for sg in (container.get("definitions") or {}).get("subgraphs") or []:
            if isinstance(sg, dict):
                walk(sg)

    bp = copy.deepcopy(bp)
    walk(bp)
    _reconstruct_boundaries(bp)
    return bp


def _reconstruct_boundaries(bp: dict) -> None:
    """Fill in def.inputs/def.outputs when the live serialisation omits them.

    Blueprint files declare the subgraph boundary on the definition
    (inputs/outputs arrays with name+type); the desktop frontend's live
    serialize() leaves those null. The same information lives on the instance
    node: its first N inputs mirror the boundary slots 0..N-1 (N = highest
    -10 origin_slot in the def links + 1; the remaining instance inputs are
    promoted proxy widgets, which must NOT become slots), and its outputs
    mirror the boundary outputs 1:1."""
    defs = {sg.get("id"): sg
            for sg in (bp.get("definitions") or {}).get("subgraphs") or []
            if isinstance(sg, dict)}

    def instances(container: dict):
        for n in container.get("nodes") or []:
            if isinstance(n, dict) and n.get("type") in defs:
                yield n

    containers = [bp] + list(defs.values())
    for container in containers:
        for inst in instances(container):
            sg = defs[inst["type"]]
            links = [l for l in sg.get("links") or [] if isinstance(l, dict)]
            if sg.get("inputs") is None:
                n_slots = 1 + max((l.get("origin_slot", -1) for l in links
                                   if l.get("origin_id") == -10), default=-1)
                sg["inputs"] = [
                    {"id": f"cch_in_{k}",
                     "name": i.get("name") or f"input_{k}",
                     **({"label": i["label"]} if i.get("label") else {}),
                     "type": i.get("type") or "*"}
                    for k, i in enumerate((inst.get("inputs") or [])[:n_slots])]
            if sg.get("outputs") is None:
                sg["outputs"] = [
                    {"id": f"cch_out_{k}",
                     "name": o.get("name") or f"output_{k}",
                     **({"label": o["label"]} if o.get("label") else {}),
                     "type": o.get("type") or "*"}
                    for k, o in enumerate(inst.get("outputs") or [])]
            if not sg.get("inputNode"):
                sg["inputNode"] = {"id": -10}
            if not sg.get("outputNode"):
                sg["outputNode"] = {"id": -20}


def _dump_debug(blueprint: dict, err_text: str) -> str | None:
    """Write the failing payload + traceback next to the other caches so it
    can be attached to a bug report. Fail-soft: never raises."""
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_DUMP, "w", encoding="utf-8") as f:
            json.dump({"error": err_text, "blueprint": blueprint}, f, indent=2)
        return str(DEBUG_DUMP)
    except Exception as e:
        log.warning("could not write convert debug dump: %s", e)
        return None


def preflight(blueprint: dict, schemas: SchemaSource) -> dict:
    """Validate a subgraph for cloud conversion and return a structured report.

    Report shape (always JSON-serialisable):
        ok                 bool  — a functional node CAN be generated
        name               str
        errors             [str] — hard blockers; when non-empty ok is False
        warnings           [str] — non-blocking hints
        missing_classes    [str]
        inputs / outputs   [{name, type, ...}]
        instant_testable   bool  — the "test" mode can build a generic node
        generic_json       str   — API prompt for the generic node (if testable)
        image_inputs       [{token, name}]
        baked_inputs       [{name, value}]
        generic_reason     str   — why instant_testable is False (if so)
    """
    report: dict = {
        "ok": False, "name": "", "errors": [], "warnings": [],
        "missing_classes": [], "inputs": [], "outputs": [],
        "instant_testable": False, "image_inputs": [], "baked_inputs": [],
    }

    # 1) run the converter, turning its exceptions into structured errors.
    # every conversion-level failure dumps the exact payload — live-frontend
    # serialisation quirks can only be diagnosed from the real structure
    try:
        cw = convert(_normalize_blueprint(blueprint), schemas)
    except (BlueprintFormatError, UnsupportedTypeError) as e:
        _dump_debug(blueprint, str(e))
        report["errors"].append(str(e))
        return report
    except Exception as e:  # defensive: a converter bug must not 500 the route
        tb = traceback.format_exc()
        log.warning("preflight conversion failed:\n%s", tb)
        path = _dump_debug(blueprint, tb)
        msg = f"Conversion failed: {e}"
        if path:
            msg += (f" — the payload was saved to {path}; attach that file "
                    "to a bug report.")
        report["errors"].append(msg)
        return report

    report["name"] = cw.name
    report["missing_classes"] = list(cw.missing_classes)
    report["inputs"] = [{"name": i.name, "type": i.type, "kind": i.kind}
                        for i in cw.inputs]
    report["outputs"] = [{"name": o.name, "type": o.type} for o in cw.outputs]
    report["warnings"].extend(cw.warnings)

    # 2) hard blockers → the node would be dysfunctional, refuse to generate it
    if cw.missing_classes:
        report["errors"].append(
            "These node classes do not exist on Comfy Cloud, so the node could "
            "not run: " + ", ".join(cw.missing_classes)
            + ". Replace them with cloud-available nodes inside the subgraph.")
    if not cw.outputs:
        report["errors"].append(
            "The subgraph exposes no transferable output (IMAGE / MASK / VIDEO).")

    # 3) non-blocking hints
    if not schemas.has_cloud_catalog:
        report["warnings"].append(
            "No cloud catalog is cached, so node availability and model names "
            "could not be verified. Set your API key and rescan for a reliable "
            "check — any model referenced here must exist on Comfy Cloud.")
    if cw.local_capable:
        report["warnings"].append(
            "This subgraph uses no AI model — it runs faster and free locally; "
            "cloud offloading only adds latency and GPU cost.")
    for bi in cw.inputs:
        # a model-selector COMBO with no resolvable cloud options degrades to a
        # free-text field (converter sets .type STRING) — the user must type an
        # exact cloud model name, which is easy to get wrong
        if bi.type == "STRING" and bi.kind == "slot":
            report["warnings"].append(
                f"Input '{bi.name}' has no cloud option list and became a free "
                "text field — its value must exactly match a Comfy Cloud model "
                "name.")

    report["ok"] = not report["errors"]
    if report["ok"]:
        report.update(_to_generic(cw))
    return report


def _set_input(prompt: dict, key: str, iname: str, value) -> None:
    node = prompt.get(key)
    if node is not None:
        node.setdefault("inputs", {})[iname] = value


def _remaining_sentinel_ids(prompt: dict) -> set[str]:
    from .converter.model import SENTINEL
    left = set()
    for node in prompt.values():
        for val in (node.get("inputs") or {}).values():
            if isinstance(val, list) and val and val[0] == SENTINEL:
                left.add(str(val[1]) if len(val) > 1 else "?")
    return left


def _to_generic(cw: ConvertedWorkflow) -> dict:
    """Rewrite the converted prompt so the generic runner can execute it:
    IMAGE slot inputs become %CCH_IMAGE_N% tokens (max 4), value inputs are
    baked to their default. instant_testable is False (with a reason) when the
    subgraph needs an input the generic node cannot supply — a MASK input, a
    fifth image, or a value input without a default."""
    prompt = copy.deepcopy(cw.prompt)
    image_inputs: list[dict] = []
    baked: list[dict] = []
    reasons: list[str] = []
    img_n = 0

    for bi in cw.inputs:
        if bi.type == "IMAGE":
            if img_n >= MAX_GENERIC_IMAGES:
                reasons.append(f"more than {MAX_GENERIC_IMAGES} image inputs "
                               f"(e.g. '{bi.name}')")
                continue
            token = GENERIC_TOKENS[img_n]
            img_n += 1
            for key, iname in bi.targets:
                _set_input(prompt, key, iname, token)
            image_inputs.append({"token": token, "name": bi.name})
        elif bi.type in ("STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"):
            if bi.default is None:
                reasons.append(f"value input '{bi.name}' has no default to bake in")
                continue
            for key, iname in bi.targets:
                _set_input(prompt, key, iname, bi.default)
            baked.append({"name": bi.name, "value": bi.default})
        else:  # MASK, UPLOAD_COMBO, anything else
            reasons.append(f"input '{bi.name}' ({bi.type}) — the instant node "
                           "only accepts up to four image inputs")

    leftover = _remaining_sentinel_ids(prompt)
    if leftover:
        reasons.append("unfilled inputs remain: " + ", ".join(sorted(leftover)))

    if reasons:
        return {"instant_testable": False, "generic_reason": "; ".join(reasons),
                "image_inputs": image_inputs, "baked_inputs": baked}
    return {"instant_testable": True,
            "generic_json": json.dumps(prompt, indent=2),
            "image_inputs": image_inputs, "baked_inputs": baked}


def save_blueprint(blueprint: dict, name: str) -> dict:
    """Persist a validated subgraph under saved_blueprints/ as a blueprint file.
    Returns {path, filename}. The caller is expected to have run preflight()
    first — save refuses obviously malformed input but does not re-validate."""
    from .scanner import _slugify

    if not isinstance(blueprint, dict) or not blueprint.get("nodes"):
        raise ValueError("blueprint payload is empty or malformed")

    # persist in blueprint-file form (dict links) so the startup scanner's
    # convert() accepts the file regardless of the live serialisation format
    blueprint = _normalize_blueprint(blueprint)
    config.SAVED_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{_slugify(name or 'blueprint')}.json"
    path = config.SAVED_DIR / fname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blueprint, f, ensure_ascii=False, indent=2)
    log.info("saved blueprint %r -> %s", name, path)
    return {"path": str(path), "filename": fname}
