"""Dynamic V3 node classes — one per scanned blueprint."""

from __future__ import annotations

import logging
import os

from comfy_api.latest import io

from . import cache, executor
from .cloud_client import CloudError
from .converter import CONVERTER_VERSION, ConvertedWorkflow
from .converter.model import BoundInput
from .scanner import BlueprintSource, category_group

log = logging.getLogger("ComfyCloudHybrid")


def _local_input_files() -> list[str]:
    try:
        import folder_paths
        files = folder_paths.filter_files_content_types(
            os.listdir(folder_paths.get_input_directory()), ["image"])
        return sorted(files)
    except Exception:
        return []


def _build_input(bi: BoundInput):
    common = dict(display_name=bi.name if bi.name != bi.safe_id else None,
                  optional=bi.optional)
    if bi.type == "IMAGE":
        return io.Image.Input(bi.safe_id, **common)
    if bi.type == "MASK":
        return io.Mask.Input(bi.safe_id, **common)
    if bi.type == "INT":
        default = bi.default if isinstance(bi.default, int) else 0
        # honour the cloud's declared range (seed: 0..2^64-1) so frontend
        # randomize never produces a value the cloud rejects
        lo = int(bi.minimum) if bi.minimum is not None else -2**63
        hi = int(bi.maximum) if bi.maximum is not None else 2**63 - 1
        default = max(lo, min(hi, default))
        return io.Int.Input(bi.safe_id, default=default, min=lo, max=hi,
                            control_after_generate=bi.control_after_generate or None,
                            **common)
    if bi.type == "FLOAT":
        default = float(bi.default) if isinstance(bi.default, (int, float)) else 0.0
        lo = bi.minimum if bi.minimum is not None else None
        hi = bi.maximum if bi.maximum is not None else None
        if lo is not None:
            default = max(lo, default)
        if hi is not None:
            default = min(hi, default)
        extra = {}
        if lo is not None:
            extra["min"] = lo
        if hi is not None:
            extra["max"] = hi
        return io.Float.Input(bi.safe_id, default=default, **extra, **common)
    if bi.type == "BOOLEAN":
        return io.Boolean.Input(bi.safe_id, default=bool(bi.default), **common)
    if bi.type == "COMBO":
        options = list(bi.combo_options or [])
        # the blueprint's baked value must always be offered and stay default —
        # otherwise the frontend silently picks options[0]
        if bi.default is not None:
            options = [bi.default] + [o for o in options if o != bi.default]
        if not options:
            options = [""]
        return io.Combo.Input(bi.safe_id, options=options, default=options[0], **common)
    if bi.type == "UPLOAD_COMBO":
        options = _local_input_files()
        default = bi.default if bi.default in options else None
        if bi.default is not None and bi.default not in options:
            options = [bi.default] + options
            default = bi.default
        return io.Combo.Input(bi.safe_id, options=options or [""], default=default,
                              tooltip="Lokale Datei aus dem input-Ordner — wird vor dem "
                                      "Cloud-Job automatisch hochgeladen.", **common)
    # STRING and anything left
    default = bi.default if isinstance(bi.default, str) else ""
    multiline = len(default) > 80 or "\n" in default
    return io.String.Input(bi.safe_id, default=default, multiline=multiline, **common)


def _build_output(index: int, bo):
    oid = f"out_{index}_{bo.name}"
    if bo.type == "VIDEO":
        return io.Video.Output(id=oid, display_name=bo.name)
    return io.Image.Output(id=oid, display_name=bo.name)


def _availability_suffix(cw: ConvertedWorkflow) -> str:
    parts = []
    if cw.missing_classes:
        parts.append("⚠ NICHT in der Comfy Cloud ausführbar — fehlende Nodes: "
                     + ", ".join(cw.missing_classes))
    if cw.warnings:
        parts.append("Hinweise: " + "; ".join(cw.warnings[:4]))
    return ("\n\n" + "\n".join(parts)) if parts else ""


def make_blueprint_node(bp: BlueprintSource, cw: ConvertedWorkflow) -> type[io.ComfyNode]:
    node_id = f"CloudHybrid_{bp.slug}"
    # the subgraph definition carries the official taxonomy ("Image Tools/
    # Color adjust", …) — mirror it 1:1; blueprints without a category
    # (typically user-published) fall back to the name-prefix heuristic
    group = bp.category or category_group(bp.name)
    category = f"cloud hybrid/{group}" if group else "cloud hybrid"
    description = (cw.description or f"Führt den Subgraph Blueprint „{bp.name}“ "
                                     "auf Comfy Cloud aus.") + _availability_suffix(cw)
    inputs = [_build_input(bi) for bi in cw.inputs]
    outputs = [_build_output(i, bo) for i, bo in enumerate(cw.outputs)]

    class _CloudBlueprintNode(io.ComfyNode):
        _bp = bp
        _schema_inputs = inputs
        _schema_outputs = outputs

        @classmethod
        def define_schema(cls) -> io.Schema:
            return io.Schema(
                node_id=node_id,
                display_name=f"☁ {bp.name}",
                category=category,
                description=description,
                inputs=cls._schema_inputs,
                outputs=cls._schema_outputs,
                hidden=[io.Hidden.unique_id],
                not_idempotent=True,
            )

        @classmethod
        async def execute(cls, **kwargs) -> io.NodeOutput:
            # hot-reloadable: prefer the freshest cache written by /rescan
            current = cache.load_converted(cls._bp.slug,
                                           converter_version=CONVERTER_VERSION) or cw
            if current.missing_classes:
                raise CloudError(
                    f"Blueprint '{cls._bp.name}' ist in der Comfy Cloud nicht ausführbar — "
                    f"fehlende Nodes: {', '.join(current.missing_classes)}")
            result = await executor.run(current, kwargs,
                                        node_id=cls.hidden.unique_id)
            return io.NodeOutput(*result)

    _CloudBlueprintNode.__name__ = node_id
    return _CloudBlueprintNode
