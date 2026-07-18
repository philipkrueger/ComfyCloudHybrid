"""Generic escape-hatch node: run any API-format workflow JSON on Comfy Cloud."""

from __future__ import annotations

import json
import logging

from comfy_api.latest import io

from . import executor
from .cloud_client import CloudError

log = logging.getLogger("ComfyCloudHybrid")

TOKENS = ["%CCH_IMAGE_1%", "%CCH_IMAGE_2%", "%CCH_IMAGE_3%", "%CCH_IMAGE_4%"]


class CloudHybridRunWorkflow(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="CloudHybrid_RunWorkflow",
            display_name="☁ Cloud Workflow ausführen",
            category="cloud hybrid",
            description=(
                "Führt ein beliebiges Workflow-JSON im API-Format auf Comfy Cloud aus "
                "(File → Export (API)). Bild-Inputs: im JSON die Platzhalter "
                "%CCH_IMAGE_1%…%CCH_IMAGE_4% als Input-Wert eintragen (z. B. beim "
                "LoadImage-image-Feld) und hier die Bilder anschließen. "
                "Gibt alle Bild-Outputs des Cloud-Jobs als Batch zurück."),
            inputs=[
                io.String.Input("workflow_json", multiline=True, default="",
                                tooltip="API-Format Workflow-JSON (oder Pfad zu einer .json-Datei)"),
                io.Image.Input("image_1", optional=True),
                io.Image.Input("image_2", optional=True),
                io.Image.Input("image_3", optional=True),
                io.Image.Input("image_4", optional=True),
                io.Int.Input("timeout_s", default=600, min=30, max=3600,
                             tooltip="Maximale Wartezeit auf den Cloud-Job"),
            ],
            outputs=[io.Image.Output(display_name="IMAGE")],
            hidden=[io.Hidden.unique_id],
            not_idempotent=True,
        )

    @classmethod
    async def execute(cls, workflow_json="", image_1=None, image_2=None,
                      image_3=None, image_4=None, timeout_s=600) -> io.NodeOutput:
        text = (workflow_json or "").strip()
        if not text:
            raise CloudError("Kein Workflow-JSON angegeben.")
        if text.endswith(".json") and "\n" not in text and "{" not in text:
            try:
                with open(text, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError as e:
                raise CloudError(f"Workflow-Datei nicht lesbar: {e}")
        try:
            prompt = json.loads(text)
        except json.JSONDecodeError as e:
            raise CloudError(f"Workflow-JSON ungültig: {e}")
        if not isinstance(prompt, dict) or not prompt:
            raise CloudError("Workflow-JSON muss ein Objekt im API-Format sein "
                             "(File → Export (API), nicht das normale Workflow-Format).")
        if "nodes" in prompt and "links" in prompt:
            raise CloudError("Das ist das UI-Workflow-Format. Bitte im API-Format "
                             "exportieren (File → Export (API)).")
        images = dict(zip(TOKENS, [image_1, image_2, image_3, image_4]))
        result = await executor.run_raw_prompt(prompt, images, timeout_s=timeout_s,
                                               node_id=cls.hidden.unique_id)
        return io.NodeOutput(result)
