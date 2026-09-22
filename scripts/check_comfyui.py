"""Check registration and async execution against an installed ComfyUI checkout.

Run with ComfyUI's Python: python scripts/check_comfyui.py /path/to/ComfyUI
CPU only; temporary pack state; cloud execution is mocked, with network calls
blocked. Unsupported blueprint boundaries are reported separately from failures.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

PACK_ROOT = Path(__file__).resolve().parents[1]


async def check() -> dict:
    import nodes
    from server import PromptServer
    from comfy_api.latest import io, ui
    from execution import _async_map_node_over_list, get_input_data

    try:
        from app.assets.manager import default_asset_manager  # ComfyUI >= 0.36
    except ImportError:
        PromptServer(asyncio.get_running_loop())
    else:
        PromptServer(asyncio.get_running_loop(), default_asset_manager())
    await nodes.init_extra_nodes(init_custom_nodes=False, init_api_nodes=True)

    # Import exactly as ComfyUI's directory loader does, then isolate all pack
    # state before the real loader invokes comfy_entrypoint/get_node_list.
    module_name = str(PACK_ROOT).replace(".", "_x_")
    spec = importlib.util.spec_from_file_location(module_name, PACK_ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    prefix = module_name + ".src.comfycloudhybrid."
    config = sys.modules[prefix + "config"]
    cache = sys.modules[prefix + "cache"]
    executor = sys.modules[prefix + "executor"]
    extension = sys.modules[prefix + "extension"]
    converter = sys.modules[prefix + "converter"]
    scanner = sys.modules[prefix + "scanner"]

    report = {"registered": 0, "checked_blueprints": 0, "executed": 0,
              "unsupported": [], "unavailable": []}
    with tempfile.TemporaryDirectory(prefix="cch-compat-") as tmp, ExitStack() as stack:
        state = Path(tmp)
        for obj, name, value in (
            (config, "CONFIG_PATH", state / "config.json"),
            (config, "SAVED_DIR", state / "saved"),
            (config, "CACHE_DIR", state),
            (cache, "CONVERTED_DIR", state / "converted"),
            (cache, "OBJECT_INFO_PATH", state / "catalog.json"),
            (cache, "UPLOADS_PATH", state / "uploads.json"),
        ):
            stack.enter_context(patch.object(obj, name, value))
        stack.enter_context(patch.object(config, "get_api_key", return_value=None))
        stack.enter_context(patch.object(
            executor.ComfyCloudClient, "_get_session",
            side_effect=AssertionError("Compatibility checks must not contact the cloud")))
        assert not hasattr(module, "NODE_CLASS_MAPPINGS"), "V3 entrypoint would be ignored"
        assert await nodes.load_custom_node(str(PACK_ROOT)), "Pack registration failed"
        registered = {key for key in nodes.NODE_CLASS_MAPPINGS if key.startswith("CloudHybrid_")}
        report["registered"] = len(registered)
        assert "CloudHybrid_RunWorkflow" in registered and len(registered) > 1
        assert str(PACK_ROOT / "web/js") in nodes.EXTENSION_WEB_DIRS.values()
        assert any(route.path == "/cloudhybrid/convert"
                   for route in PromptServer.instance.routes)

        async def execute_node(cls, inputs, method):
            info = cls.GET_NODE_INFO_V1()
            json.dumps(info)  # /object_info must be JSON serializable
            assert cls.NOT_IDEMPOTENT, "Paid cloud calls must not be reused from cache"
            expected = tuple(range(len(cls.RETURN_TYPES)))
            args, missing, v3_data = get_input_data(inputs, cls, "compat-node")
            assert not missing, missing
            with patch.object(executor, method, new=AsyncMock(return_value=expected)) as mock:
                results = await _async_map_node_over_list(
                    "compat-prompt", "compat-node", cls, args, cls.FUNCTION,
                    v3_data=v3_data)
                result = results[0]
                if isinstance(result, asyncio.Task):
                    result = await result
                assert isinstance(result, io.NodeOutput)
                assert result.result == expected
                assert mock.await_args.kwargs["node_id"] == "compat-node"
            report["executed"] += 1

        await execute_node(nodes.NODE_CLASS_MAPPINGS["CloudHybrid_RunWorkflow"],
                           {"workflow_json": '{"1":{"class_type":"SaveImage","inputs":{}}}'},
                           "run_raw_prompt")
        schemas = converter.SchemaSource(use_local=True)
        representative = None
        for bp in scanner.scan():
            try:
                cw = converter.convert(scanner.load_blueprint(bp.path), schemas)
            except converter.UnsupportedTypeError as exc:
                report["unsupported"].append({"name": bp.name, "reason": str(exc)})
                continue
            except converter.BlueprintFormatError as exc:
                if not str(exc).startswith("Blueprint has no transferable output"):
                    raise
                report["unsupported"].append({"name": bp.name, "reason": str(exc)})
                continue
            cls = extension.make_blueprint_node(bp, cw)
            cls.GET_SCHEMA()
            report["checked_blueprints"] += 1
            if not cw.local_capable:
                representative = bp
                assert cls.SCHEMA.node_id in registered, f"Missing registration: {bp.name}"
            if cw.missing_classes:
                report["unavailable"].append({"name": bp.name, "classes": cw.missing_classes})
            else:
                await execute_node(cls, {}, "run")

        # A malformed generated schema must be caught inside the per-blueprint
        # loop, before ComfyUI's loader can abort the rest of the extension.
        assert representative is not None
        class InvalidNode(io.ComfyNode):
            @classmethod
            def define_schema(cls):
                return io.Schema(node_id="Invalid", inputs=[io.Int.Input("x"), io.Int.Input("x")])

            @classmethod
            def execute(cls, **kwargs):
                return io.NodeOutput()

        with patch.object(extension, "scan", return_value=[representative]), \
                patch.object(extension, "make_blueprint_node", return_value=InvalidNode):
            classes = await (await extension.comfy_entrypoint()).get_node_list()
            assert classes == [extension.CloudHybridRunWorkflow]

        # Assert the actual core VIDEO history format behind the regression.
        assert "images" in ui.PreviewVideo([]).as_dict()
        await asyncio.sleep(0)  # let no-key catalog refresh tasks finish
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comfyui", type=Path)
    parser.add_argument("--report", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    root = args.comfyui.resolve()
    if not (root / "comfy_api/latest").is_dir():
        parser.error("Expected a ComfyUI checkout with the V3 API")
    sys.path.insert(0, str(root))
    os.environ["HF_HUB_OFFLINE"] = "1"
    import comfy.options
    comfy.options.enable_args_parsing()
    sys.argv = ["check_comfyui", "--cpu"]
    logging.basicConfig(level=logging.ERROR)
    report = asyncio.run(check())
    from comfyui_version import __version__
    report["comfyui"] = __version__
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"ComfyUI {__version__}: {report['registered']} nodes registered; "
          f"{report['checked_blueprints']} blueprint schemas checked; "
          f"{report['executed']} async node executions passed (cloud mocked).")
    print(f"Unsupported boundaries: {len(report['unsupported'])}; "
          f"unavailable node classes: {len(report['unavailable'])}.")


if __name__ == "__main__":
    main()
