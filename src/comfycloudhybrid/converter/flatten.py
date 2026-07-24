"""Core flattening: blueprint JSON → flat API-format prompt.

The ComfyUI backend never resolves `definitions.subgraphs` — the frontend
flattens before POST /prompt. This module reimplements that flattening for
headless use. Prompt keys use the frontend's colon path convention
("<instanceId>:<innerId>"), which the backend treats as opaque strings.
"""

from __future__ import annotations

import logging
import re

from .model import (
    SENTINEL,
    TENSOR_TYPES,
    VALUE_TYPES,
    BlueprintFormatError,
    BoundInput,
    BoundOutput,
    ConvertedWorkflow,
    UnsupportedTypeError,
    sanitize_id,
)
from .schema_source import SchemaSource
from .widgets import (SEED_NAMES, combo_options, map_widgets,
                      required_widget_defaults, widget_inputs_of)

log = logging.getLogger("ComfyCloudHybrid")

# Classes whose "image" widget references a file in the input directory that
# must be uploaded to the cloud before the job runs.
UPLOAD_CLASSES = {"LoadImage": "image", "LoadImageMask": "image"}

# Pure pass-through nodes the cloud does not expose as executable classes —
# collapsed during flattening (link resolution follows through them). Only
# genuine forwarders belong here (a PrimitiveNode carries a value and must NOT
# be collapsed, or its widget value would be lost).
PASSTHROUGH_CLASSES = {"Reroute", "Reroute (rgthree)"}

# Classes that imply AI weights — loader/sampler machinery plus classes that
# load models internally without "Loader" in the name. Validated against the
# shipped blueprint collection (matches exactly the 62 model-dependent ones).
_MODEL_CLASS_RE = re.compile(
    r"Loader|Checkpoint|UNET|UNet|Diffusion|CLIP|VAE|Lora|LoRA|Sampler|Sigmas"
    r"|Guider|LatentImage|ModelSampling|TorchCompile|Load\w*Model|Model\w*Load",
    re.IGNORECASE)
_IMPLICIT_MODEL_RE = re.compile(
    r"DepthAnything|BiRefNet|SAM\d|SDPose|MoGe|Mediapipe|RMBG|Interpolat|GIMM"
    r"|GAN|Lotus|Hunyuan|Tripo|VOID|TextGenerate|RemoveBackground|Gemini|LLM",
    re.IGNORECASE)


def _is_local_capable(prompt: dict, schemas: SchemaSource) -> bool:
    """True when no emitted node needs AI weights or a partner API."""
    for entry in prompt.values():
        cls = entry.get("class_type", "")
        if cls in ("SaveImage", "SaveVideo"):
            continue
        if (schemas.get_cloud(cls) or {}).get("api_node"):
            return False
        if _MODEL_CLASS_RE.search(cls) or _IMPLICIT_MODEL_RE.search(cls):
            return False
    return True

_STRIPPED_MODES = (2, 4)  # mute, bypass

# blueprint authors mark optional boundary inputs via the label, e.g.
# "image2 (optional)" — detected here, stripped from the display name
_OPTIONAL_RE = re.compile(r"\s*\(optional\)\s*$", re.IGNORECASE)


def _clean_label(name) -> str:
    return _OPTIONAL_RE.sub("", str(name)).strip() or "input"


def _target_is_optional(prompt: dict, schemas: SchemaSource,
                        key: str, iname: str) -> bool:
    """True when the targeted input sits in the optional section of its
    class schema — the cloud runs the node fine without it."""
    cls = (prompt.get(key) or {}).get("class_type")
    entry = schemas.get(cls) if cls else None
    if not entry:
        return False
    return iname in ((entry.get("input") or {}).get("optional") or {})


class _Ctx:
    def __init__(self):
        self.prompt: dict = {}
        self.warnings: list[str] = []
        self.missing: set[str] = set()
        self.taken_ids: set[str] = set()
        self.mapped_widgets: dict[str, dict] = {}  # prompt_key -> widget dict


def _collect_defs(container: dict, defs: dict) -> None:
    for d in (container.get("definitions") or {}).get("subgraphs") or []:
        defs[d["id"]] = d
        _collect_defs(d, defs)


def convert(blueprint: dict, schemas: SchemaSource, fallback_name: str = "") -> ConvertedWorkflow:
    defs: dict = {}
    _collect_defs(blueprint, defs)
    if not defs:
        raise BlueprintFormatError("keine Subgraph-Definitionen (definitions.subgraphs) gefunden")

    root_nodes = blueprint.get("nodes") or []
    instances = [n for n in root_nodes if n.get("type") in defs]
    if len(instances) != 1:
        raise BlueprintFormatError(
            f"Blueprint muss genau eine Subgraph-Instanz enthalten (gefunden: {len(instances)})")
    inst = instances[0]
    root_def = defs[inst["type"]]
    name = root_def.get("name") or inst.get("title") or fallback_name

    ctx = _Ctx()

    # -- boundary inputs -> BoundInputs ------------------------------------
    slot_inputs: list[BoundInput] = []
    for slot in root_def.get("inputs") or []:
        typ = slot.get("type", "*")
        raw_label = slot.get("label") or slot.get("name") or "input"
        disp = _clean_label(raw_label)
        label_optional = _OPTIONAL_RE.search(str(raw_label)) is not None
        # multi-type slots ("IMAGE,MASK") accept several types — pick the
        # strongest one we can ship across the boundary
        parts = [p.strip() for p in str(typ).split(",")]
        if "IMAGE" in parts:
            bi_type = "IMAGE"
        elif "MASK" in parts:
            bi_type = "MASK"
        elif any(p in VALUE_TYPES for p in parts):
            # COMBO slots (model selectors like unet_name) stay COMBO — their
            # options are resolved from the cloud schema of the target input
            # after expansion; without resolvable options they fall back to
            # a free-text STRING below
            bi_type = next(p for p in parts if p in VALUE_TYPES)
        else:
            # non-transferable type (BOUNDING_BOX, LATENT, …) — keep it for
            # now; after binding we know its targets and either drop it (all
            # targets optional in the cloud schema → cloud default applies)
            # or reject the blueprint (a required input depends on it)
            bi_type = parts[0]
        slot_inputs.append(BoundInput(
            name=disp, safe_id=sanitize_id(disp, ctx.taken_ids),
            type=bi_type, kind="slot", optional=label_optional))

    def resolve_root_boundary(k: int):
        if 0 <= k < len(slot_inputs):
            return ("bound", slot_inputs[k])
        return ("none", None)

    prefix = f"{inst['id']}:"
    omap = _expand(root_def, prefix, resolve_root_boundary, ctx, defs, schemas)

    # -- non-transferable boundary inputs ------------------------------------
    # a slot whose type cannot cross the cloud boundary is fine as long as
    # every input it feeds is optional in the cloud schema (SAM3's bboxes/
    # coords hints, say): drop the slot, the cloud default applies. If a
    # required input depends on it the node would be dysfunctional — reject.
    for bi in list(slot_inputs):
        if bi.type in TENSOR_TYPES or bi.type in VALUE_TYPES:
            continue
        if all(_target_is_optional(ctx.prompt, schemas, k, i) for k, i in bi.targets):
            for k, i in bi.targets:
                (ctx.prompt.get(k) or {}).get("inputs", {}).pop(i, None)
            ctx.warnings.append(
                f"Input '{bi.name}' ({bi.type}) cannot cross the cloud boundary "
                "and stays unconnected — the cloud node's default is used")
            slot_inputs.remove(bi)
        else:
            raise UnsupportedTypeError(
                f"Subgraph input '{bi.name}' has type {bi.type} — only IMAGE/MASK/"
                "STRING/INT/FLOAT/BOOLEAN cross the cloud boundary, and a required "
                "input depends on it. Rework the subgraph so only images or values "
                "pass the boundary (VAEDecode/Encode inside the subgraph).")

    # -- boundary outputs -> SaveImage nodes --------------------------------
    outputs: list[BoundOutput] = []
    for k, slot in enumerate(root_def.get("outputs") or []):
        typ = slot.get("type", "*")
        disp = slot.get("label") or slot.get("name") or f"output_{k}"
        res = omap.get(k, ("none", None))
        if res[0] == "stripped":
            ctx.missing.add(res[1].get("type", "?"))
            ctx.warnings.append(f"Output '{disp}' hängt an nicht ausführbarem "
                                f"Node '{res[1].get('type')}'")
            continue
        if res[0] != "link":
            ctx.warnings.append(f"Output '{disp}' is not connected to an executable "
                                "node and is skipped")
            continue
        save_key = f"cch_save_{k}"
        file_prefix = f"CloudHybrid/{sanitize_id(disp, set())}"
        if typ == "IMAGE":
            ctx.prompt[save_key] = {
                "class_type": "SaveImage",
                "inputs": {"images": res[1], "filename_prefix": file_prefix},
                "_meta": {"title": f"CloudHybrid Output {disp}"},
            }
        elif typ == "VIDEO":
            ctx.prompt[save_key] = {
                "class_type": "SaveVideo",
                "inputs": {"video": res[1], "filename_prefix": file_prefix,
                           "format": "auto", "codec": "auto"},
                "_meta": {"title": f"CloudHybrid Output {disp}"},
            }
        elif typ == "MASK":
            # a MASK cannot be saved directly — convert it to a grayscale image
            # first; the executor rebuilds the mask tensor on download
            # (executor.png_bytes_to_mask, inverse of mask_to_png_bytes)
            conv_key = f"cch_mask2img_{k}"
            ctx.prompt[conv_key] = {
                "class_type": "MaskToImage",
                "inputs": {"mask": res[1]},
                "_meta": {"title": f"CloudHybrid MaskToImage {disp}"},
            }
            ctx.prompt[save_key] = {
                "class_type": "SaveImage",
                "inputs": {"images": [conv_key, 0], "filename_prefix": file_prefix},
                "_meta": {"title": f"CloudHybrid Output {disp}"},
            }
        else:
            ctx.warnings.append(f"Output '{disp}' ({typ}) is skipped — only IMAGE, MASK "
                                "and VIDEO outputs are transferred back")
            continue
        outputs.append(BoundOutput(name=disp, type=typ, save_node_key=save_key))
    if not outputs and not ctx.missing:
        # surface WHY each output was skipped — without this the caller only
        # sees "no usable output" while the actual reason (unconnected slot,
        # unsupported type, stripped node) sits in the discarded warnings
        detail = "; ".join(ctx.warnings[-3:])
        raise BlueprintFormatError(
            "Blueprint has no usable IMAGE, MASK or VIDEO output"
            + (f" ({detail})" if detail else ""))

    # tensor slots whose targets are ALL optional in the cloud schema may stay
    # unconnected — the executor drops the sentinel and the cloud node falls
    # back to its default (e.g. TextEncodeQwenImageEditPlus.image2/image3)
    for bi in slot_inputs:
        if (not bi.optional and bi.type in TENSOR_TYPES and bi.targets
                and all(_target_is_optional(ctx.prompt, schemas, k, i)
                        for k, i in bi.targets)):
            bi.optional = True

    # value-typed slot inputs: default = what the inner widget held before the
    # sentinel replaced it — the node must run sensibly out of the box
    # (a 0/"" default would override the blueprint's baked values in the cloud)
    for bi in slot_inputs:
        if bi.type in TENSOR_TYPES or bi.default is not None:
            continue
        for key, iname in bi.targets:
            val = ctx.mapped_widgets.get(key, {}).get(iname)
            if val is not None:
                bi.default = val
                break

    # -- promoted widgets (root instance only) ------------------------------
    proxy_inputs = _build_proxies(inst, prefix, ctx, defs, schemas)

    # a promoted widget that feeds the same input as a boundary slot is a
    # duplicate — the slot wins (avoids width/seed appearing twice on the node)
    slot_targets = {t for bi in slot_inputs for t in bi.targets}
    proxy_inputs = [p for p in proxy_inputs
                    if not (set(p.targets) & slot_targets)]

    # numeric constraints from the cloud schema of each target input — so the
    # node's declared min/max match what the cloud will accept (seed 0..2^64-1
    # etc.); without this, frontend randomize can produce out-of-range values
    for bi in slot_inputs + proxy_inputs:
        if bi.type in ("INT", "FLOAT"):
            _apply_numeric_constraints(bi, ctx.prompt, schemas)

    # COMBO slots (model selectors): pull the option list from the cloud
    # schema of the target input, so the node offers the actual cloud models
    # as a dropdown instead of a copy-the-exact-name text field. Without a
    # cloud catalog (cold start) the slot degrades to free-text STRING.
    for bi in slot_inputs:
        if bi.type == "COMBO" and bi.combo_options is None:
            _apply_combo_options(bi, ctx.prompt, schemas)
            if not bi.combo_options:
                bi.type = "STRING"

    # -- interior fixed-file uploads ----------------------------------------
    proxied = {(k, i) for bi in proxy_inputs + slot_inputs for (k, i) in bi.targets}
    required_uploads: list[tuple[str, str, str]] = []
    for key, entry in ctx.prompt.items():
        input_name = UPLOAD_CLASSES.get(entry.get("class_type", ""))
        if not input_name or (key, input_name) in proxied:
            continue
        val = entry["inputs"].get(input_name)
        if isinstance(val, str) and val:
            required_uploads.append((key, input_name, val))

    return ConvertedWorkflow(
        name=name,
        prompt=ctx.prompt,
        inputs=slot_inputs + proxy_inputs,
        outputs=outputs,
        required_uploads=required_uploads,
        missing_classes=sorted(ctx.missing),
        warnings=ctx.warnings,
        description=(root_def.get("description")
                     or (blueprint.get("extra") or {}).get("BlueprintDescription", "")),
        local_capable=_is_local_capable(ctx.prompt, schemas),
    )


def _expand(defn: dict, prefix: str, resolve_boundary, ctx: _Ctx,
            defs: dict, schemas: SchemaSource) -> dict:
    """Emit one subgraph definition into ctx.prompt.

    Returns output map: boundary output slot -> resolution tuple.
    """
    nodes_by_id = {n["id"]: n for n in defn.get("nodes") or []}
    link_by_id = {l["id"]: l for l in defn.get("links") or []}
    instance_outputs: dict = {}

    def is_instance(n) -> bool:
        return n.get("type") in defs

    def is_stripped(n) -> bool:
        return n.get("mode") in _STRIPPED_MODES or not schemas.known(n.get("type", ""))

    def resolve_origin(origin_id, origin_slot, _seen=None):
        if origin_id == -10:
            return resolve_boundary(origin_slot)
        node = nodes_by_id.get(origin_id)
        if node is None:
            return ("none", None)
        # Reroute and similar are pure pass-through nodes the cloud doesn't
        # expose — collapse them by following their (single) input upstream
        if node.get("type") in PASSTHROUGH_CLASSES:
            seen = _seen or set()
            if origin_id in seen:
                return ("none", None)
            seen.add(origin_id)
            for inp in node.get("inputs") or []:
                link = link_by_id.get(inp.get("link"))
                if link is not None:
                    return resolve_origin(link["origin_id"], link["origin_slot"], seen)
            return ("none", None)
        if is_instance(node):
            return expand_instance(node).get(origin_slot, ("none", None))
        if is_stripped(node):
            return ("stripped", node)
        return ("link", [f"{prefix}{origin_id}", origin_slot])

    def input_feed(node, slot_index):
        inputs = node.get("inputs") or []
        if slot_index >= len(inputs):
            return ("none", None)
        link_id = inputs[slot_index].get("link")
        link = link_by_id.get(link_id) if link_id is not None else None
        if link is None:
            return ("none", None)
        return resolve_origin(link["origin_id"], link["origin_slot"])

    def expand_instance(inst_node) -> dict:
        nid = inst_node["id"]
        if nid in instance_outputs:
            return instance_outputs[nid]
        instance_outputs[nid] = {}  # break cycles defensively
        child_prefix = f"{prefix}{nid}:"
        child_omap = _expand(defs[inst_node["type"]], child_prefix,
                             lambda k, _n=inst_node: input_feed(_n, k),
                             ctx, defs, schemas)
        instance_outputs[nid] = child_omap
        _apply_nested_proxy_values(inst_node, child_prefix, ctx, defs, schemas)
        return child_omap

    # emit executable nodes
    for n in defn.get("nodes") or []:
        if is_instance(n):
            continue
        if n.get("type") in PASSTHROUGH_CLASSES:
            continue  # collapsed via resolve_origin, never emitted
        if is_stripped(n):
            if n.get("mode") not in _STRIPPED_MODES:
                ctx.warnings.append(f"Node '{n.get('type')}' removed (not executable)")
            continue
        key = f"{prefix}{n['id']}"
        schema_entry = schemas.get(n["type"]) or {}
        inputs = map_widgets(n, schema_entry=schema_entry)
        ctx.mapped_widgets[key] = dict(inputs)
        for inp in n.get("inputs") or []:
            link_id = inp.get("link")
            link = link_by_id.get(link_id) if link_id is not None else None
            if link is None:
                continue
            kind, val = resolve_origin(link["origin_id"], link["origin_slot"])
            iname = inp.get("name")
            if kind == "link":
                inputs[iname] = val
            elif kind == "bound":
                val.targets.append((key, iname))
                inputs[iname] = [SENTINEL, val.safe_id]
            elif kind == "stripped":
                ctx.missing.add(val.get("type", "?"))
                ctx.warnings.append(
                    f"'{n['type']}' benötigt Input von nicht ausführbarem "
                    f"Node '{val.get('type')}'")
        if not schemas.in_cloud(n["type"]):
            ctx.missing.add(n["type"])
        # backfill required inputs the cloud schema gained after this
        # blueprint was authored (see SDPoseDrawKeypoints.draw_head, added
        # post-authoring) — the cloud rejects the submit if they're absent,
        # it does not apply its own defaults for missing inputs
        for rname, rdefault in required_widget_defaults(schema_entry).items():
            if rname not in inputs:
                inputs[rname] = rdefault
                ctx.warnings.append(
                    f"'{n['type']}' is missing newer required input '{rname}' "
                    f"in this blueprint — using the cloud default ({rdefault!r})")
        ctx.prompt[key] = {"class_type": n["type"], "inputs": inputs,
                           "_meta": {"title": n.get("title") or n["type"]}}

    # force-expand instances not pulled in via links (side-effect outputs)
    for n in defn.get("nodes") or []:
        if is_instance(n):
            expand_instance(n)

    omap: dict = {}
    for link in defn.get("links") or []:
        if link.get("target_id") == -20:
            omap[link.get("target_slot", 0)] = resolve_origin(
                link["origin_id"], link["origin_slot"])
    return omap


_PSEUDO_WIDGET_PREFIX = "$$"
_CONTROL_WIDGETS = {"control_after_generate"}

# cloud max for a seed-like INT (2^64-1); some cloud INT specs use it
_UINT64_MAX = 18446744073709551615


def _apply_numeric_constraints(bi: BoundInput, prompt: dict,
                               schemas: SchemaSource) -> None:
    """Read min/max/step/control_after_generate for a numeric input from the
    cloud schema of its target(s). Uses the tightest bounds (max of mins, min
    of maxes) across all targets so every target stays satisfied."""
    lo, hi = None, None
    for key, iname in bi.targets:
        cls = (prompt.get(key) or {}).get("class_type")
        entry = schemas.get_cloud(cls) if cls else None
        if entry is None:
            continue
        opts = _widget_opts(entry, iname)
        if opts is None:
            continue
        if opts.get("min") is not None:
            lo = opts["min"] if lo is None else max(lo, opts["min"])
        if opts.get("max") is not None:
            hi = opts["max"] if hi is None else min(hi, opts["max"])
        # only seeds should randomize each run — a cloud schema marking a
        # PrimitiveInt 'value' as control_after_generate must NOT turn width/
        # height/duration into randomized (and thus possibly invalid) widgets
        if opts.get("control_after_generate") and bi.name in SEED_NAMES:
            bi.control_after_generate = True
        if bi.step is None and opts.get("step") is not None:
            bi.step = opts["step"]
    if lo is not None:
        bi.minimum = lo
    if hi is not None:
        bi.maximum = hi


def _widget_opts(schema_entry: dict, input_name: str) -> dict | None:
    for nm, _typ, opts in widget_inputs_of(schema_entry):
        if nm == input_name:
            return opts
    return None


def _apply_combo_options(bi: BoundInput, prompt: dict,
                         schemas: SchemaSource) -> None:
    """Resolve a COMBO slot's option list from the CLOUD schema of its
    target input — e.g. unet_name → UNETLoader.unet_name lists every model
    installed in the cloud. Cloud-only on purpose: local option lists would
    offer models the cloud then rejects with value_not_in_list."""
    for key, iname in bi.targets:
        cls = (prompt.get(key) or {}).get("class_type")
        entry = schemas.get_cloud(cls) if cls else None
        if entry is None:
            continue
        for nm, typ, opts in widget_inputs_of(entry):
            if nm == iname:
                options = combo_options(typ, opts)
                if options:
                    bi.combo_options = options
                    return


def _real_proxy_entries(inst_node: dict, defs: dict, schemas: SchemaSource):
    """Yield (inner_node, widget_name, widget_type, opts) for genuine proxy
    widgets, skipping frontend pseudo widgets and control widgets."""
    root_def = defs.get(inst_node.get("type"))
    if root_def is None:
        return
    nodes_by_id = {n["id"]: n for n in root_def.get("nodes") or []}
    for entry in (inst_node.get("properties") or {}).get("proxyWidgets") or []:
        try:
            inner_id, widget_name = entry
        except (TypeError, ValueError):
            continue
        if widget_name.startswith(_PSEUDO_WIDGET_PREFIX) or widget_name in _CONTROL_WIDGETS:
            continue
        inner = nodes_by_id.get(int(inner_id) if str(inner_id).isdigit() else inner_id)
        if inner is None or inner.get("type") in defs:
            continue  # nested-instance proxies unsupported in v1
        schema_entry = schemas.get(inner.get("type", ""))
        if schema_entry is None:
            continue
        match = [(nm, t, o) for nm, t, o in widget_inputs_of(schema_entry) if nm == widget_name]
        if not match:
            continue  # pseudo widget (e.g. "upload")
        yield inner, match[0][0], match[0][1], match[0][2]


def _build_proxies(inst: dict, prefix: str, ctx: _Ctx, defs: dict,
                   schemas: SchemaSource) -> list[BoundInput]:
    proxies: list[BoundInput] = []
    overrides = list(inst.get("widgets_values") or [])
    idx = 0
    for inner, widget_name, wtyp, opts in _real_proxy_entries(inst, defs, schemas):
        key = f"{prefix}{inner['id']}"
        default = ctx.mapped_widgets.get(key, {}).get(widget_name)
        if idx < len(overrides):
            default = overrides[idx]
        idx += 1
        cls = inner.get("type", "")
        if UPLOAD_CLASSES.get(cls) == widget_name:
            bi_type, options = "UPLOAD_COMBO", None
        elif isinstance(wtyp, list) or wtyp == "COMBO":
            bi_type = "COMBO"
            # options must be valid IN THE CLOUD — never offer local model
            # lists on a cloud node; without cloud catalog only the baked
            # default is offered
            cloud_entry = schemas.get_cloud(cls)
            options = None
            if cloud_entry is not None:
                match = [(t, o) for nm, t, o in widget_inputs_of(cloud_entry)
                         if nm == widget_name]
                if match:
                    options = combo_options(*match[0])
        else:
            bi_type, options = wtyp, None
        bi = BoundInput(
            name=widget_name, safe_id=sanitize_id(widget_name, ctx.taken_ids),
            type=bi_type, kind="proxy", default=default,
            combo_options=options, optional=True)
        if ctx.prompt.get(key) is not None:
            bi.targets.append((key, widget_name))
            proxies.append(bi)
    return proxies


def _apply_nested_proxy_values(inst_node: dict, child_prefix: str, ctx: _Ctx,
                               defs: dict, schemas: SchemaSource) -> None:
    """Nested instances: apply their widgets_values onto inner nodes' widgets
    (positional, in proxyWidgets order). No BoundInputs — values are baked."""
    overrides = list(inst_node.get("widgets_values") or [])
    if not overrides:
        return
    idx = 0
    for inner, widget_name, _t, _o in _real_proxy_entries(inst_node, defs, schemas):
        if idx >= len(overrides):
            break
        key = f"{child_prefix}{inner['id']}"
        entry = ctx.prompt.get(key)
        if entry is not None and not isinstance(entry["inputs"].get(widget_name), list):
            entry["inputs"][widget_name] = overrides[idx]
        idx += 1
