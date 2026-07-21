"""Positional widgets_values → named inputs, using object_info-style schemas.

The frontend serializes UI nodes with a flat positional list of widget values.
Two known extra positional slots must be skipped while mapping:
- control_after_generate: appended after seed-like INT widgets
  (verified: GeminiImageNode → [..., 249510346527630, 'randomize', ...])
- upload button of image-upload combos
  (verified: LoadImage → ['Reference_9x16_2.png', 'image'])
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ComfyCloudHybrid")

CONTROL_VALUES = {"fixed", "increment", "decrement", "randomize"}
SEED_NAMES = {"seed", "noise_seed"}
UPLOAD_BUTTON_VALUES = {"image", "file", "video", "audio"}

# Per-class overrides for widget decoding, keyed by class_type. Value is a
# callable (widgets_values, widget_inputs) -> dict. Extend as drift is found.
QUIRKS: dict[str, Any] = {}


def widget_inputs_of(schema_entry: dict) -> list[tuple[str, Any, dict]]:
    """Ordered widget-backed inputs [(name, type, opts)] of an object_info entry.

    Connection-typed inputs (IMAGE, MODEL, ...) get no widget slot; neither do
    forceInput'ed value inputs.
    """
    inp = schema_entry.get("input") or {}
    order = schema_entry.get("input_order") or {}
    result = []
    for section in ("required", "optional"):
        entries = inp.get(section) or {}
        names = order.get(section) or list(entries.keys())
        for name in names:
            spec = entries.get(name)
            if not spec:
                continue
            typ = spec[0]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if _is_widget(typ, opts):
                result.append((name, typ, opts))
    return result


def required_widget_defaults(schema_entry: dict) -> dict[str, Any]:
    """{name: default} for REQUIRED widget-backed inputs that carry a schema
    default. Used to backfill inputs a blueprint's serialized node predates —
    e.g. a class gains a new required parameter after the blueprint was
    authored, so the old node's own `inputs`/`widgets_values` never mention
    it. The cloud validator does not fill defaults for inputs that are
    missing outright, so leaving them out is a hard submit-time rejection."""
    inp = schema_entry.get("input") or {}
    required = inp.get("required") or {}
    order = (schema_entry.get("input_order") or {}).get("required") or list(required.keys())
    out = {}
    for name in order:
        spec = required.get(name)
        if not spec:
            continue
        typ = spec[0]
        opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        if _is_widget(typ, opts) and "default" in opts:
            out[name] = opts["default"]
    return out


def _is_widget(typ, opts: dict) -> bool:
    if opts.get("forceInput"):
        return False
    if isinstance(typ, list):  # legacy COMBO: list of options
        return True
    if typ == "COMBO":
        return True
    return typ in ("INT", "FLOAT", "STRING", "BOOLEAN")


def combo_options(typ, opts: dict) -> list:
    if isinstance(typ, list):
        return typ
    return list(opts.get("options") or [])


def node_widget_names(node: dict) -> list[str] | None:
    """Widget names in serialized order from the node's own `inputs` array.

    Modern ComfyUI serializes widget-backed inputs as entries carrying a
    `widget: {name}` key — this is the authoritative widget order for THIS
    node, including dynamic-combo sub-widgets (e.g. `resize_type.width`) that
    the static object_info schema does not describe. Returns None if the node
    carries no such structure (fall back to the cloud schema)."""
    names = [inp.get("name") for inp in (node.get("inputs") or [])
             if isinstance(inp.get("widget"), dict) and inp.get("name")]
    return names or None


def map_widgets(node_or_class, widgets_values=None, schema_entry: dict | None = None) -> dict:
    """Map serialized widgets_values onto input names. Returns {} on empty.

    Prefers the node's own `inputs` widget order (robust for dynamic combos);
    falls back to the object_info schema order for nodes without it."""
    if isinstance(node_or_class, dict):
        node = node_or_class
        class_type = node.get("type", "")
        widgets_values = node.get("widgets_values")
        names = node_widget_names(node)
    else:  # legacy call form (class_type, values, schema)
        node = None
        class_type = node_or_class
        names = None

    if not widgets_values:
        return {}
    if isinstance(widgets_values, list) and all(v is None for v in widgets_values):
        return {}  # preview/display nodes serialize placeholder None widgets
    schema_entry = schema_entry or {}
    schema_widgets = widget_inputs_of(schema_entry)
    schema_opts = {nm: (t, o) for nm, t, o in schema_widgets}

    if class_type in QUIRKS:
        return QUIRKS[class_type](widgets_values, schema_widgets)
    if isinstance(widgets_values, dict):
        valid = set(names or []) | set(schema_opts)
        return {k: v for k, v in widgets_values.items() if k in valid}

    # widget order: node's own inputs array preferred, else schema order
    from_node = names is not None
    if names is None:
        names = [nm for nm, _, _ in schema_widgets]

    out: dict = {}
    i = 0
    total = len(widgets_values)
    for name in names:
        if i >= total:
            break
        out[name] = widgets_values[i]
        i += 1
        typ, opts = schema_opts.get(name, (None, {}))
        # control_after_generate slot: serialized right after any widget that
        # has the control (seeds, PrimitiveInt value, …) but absent from the
        # node's inputs array and from what the cloud expects
        has_control = (opts.get("control_after_generate")
                       or name in SEED_NAMES or name.endswith("seed"))
        if has_control and i < total and widgets_values[i] in CONTROL_VALUES:
            i += 1
        # upload-button slot after image-upload combos
        elif ((opts.get("image_upload") or opts.get("upload")
               or name == UPLOAD_HINT_NAME)
              and i < total and widgets_values[i] in UPLOAD_BUTTON_VALUES):
            i += 1
    # Trailing extras are expected when the node's inputs array drives the
    # mapping: dynamic/composite widgets (crop_region, custom combos) serialize
    # frontend-only sub-values after the real widget value, which the cloud
    # neither needs nor accepts. Only warn when we ran SHORT of the node's own
    # widgets (a real ordering problem) — not when values are simply left over.
    consumed_all_names = i >= len(names)
    if i != total and not (from_node and consumed_all_names):
        log.warning(
            "widget count mismatch for %s: consumed %d of %d values %r — "
            "conversion may be incomplete (add a QUIRKS entry)",
            class_type, i, total, widgets_values)
    return out


UPLOAD_HINT_NAME = "image"


def _is_combo(typ) -> bool:
    return isinstance(typ, list) or typ == "COMBO"
