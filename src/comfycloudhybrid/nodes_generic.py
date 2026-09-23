"""Generic escape-hatch node: run any API-format workflow JSON on Comfy Cloud."""

from __future__ import annotations

import json
import logging

from comfy_api.latest import io

from . import executor
from .cloud_client import CloudError
from .ondemand import GENERIC_TOKENS as TOKENS

log = logging.getLogger("ComfyCloudHybrid")


class CloudHybridRunWorkflow(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="CloudHybrid_RunWorkflow",
            display_name="☁ Run Cloud Workflow",
            category="cloud hybrid",
            description=(
                "Runs any API-format workflow JSON on Comfy Cloud "
                "(File → Export (API)). Image inputs: put the placeholders "
                f"%CCH_IMAGE_1%…{TOKENS[-1]} as input values in the JSON (e.g. in "
                "a LoadImage 'image' field) and connect the images here. "
                "Returns whatever the cloud job produced: image outputs as one "
                "batch, plus the first VIDEO / AUDIO output and any preview "
                "text (unused outputs stay empty)."),
            inputs=[
                io.String.Input("workflow_json", multiline=True, default="",
                                tooltip="API-format workflow JSON (or a path to a .json file)"),
                *[io.Image.Input(f"image_{n}", optional=True)
                  for n in range(1, len(TOKENS) + 1)],
                io.Int.Input("timeout_s", default=600, min=30, max=3600,
                             tooltip="Maximum time to wait for the cloud job"),
            ],
            outputs=[
                io.Image.Output(display_name="IMAGE"),
                io.Video.Output(display_name="VIDEO"),
                io.Audio.Output(display_name="AUDIO"),
                io.String.Output(display_name="TEXT"),
            ],
            hidden=[io.Hidden.unique_id],
            not_idempotent=True,
        )

    @classmethod
    async def execute(cls, workflow_json="", timeout_s=600, **images) -> io.NodeOutput:
        text = (workflow_json or "").strip()
        if not text:
            raise CloudError("No workflow JSON provided.")
        if text.endswith(".json") and "\n" not in text and "{" not in text:
            try:
                with open(text, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError as e:
                raise CloudError(f"Workflow file not readable: {e}")
        try:
            prompt = json.loads(text)
        except json.JSONDecodeError as e:
            raise CloudError(f"Workflow JSON is invalid: {e}")
        if not isinstance(prompt, dict) or not prompt:
            raise CloudError("Workflow JSON must be an API-format object "
                             "(File → Export (API), not the normal workflow format).")
        if "nodes" in prompt and "links" in prompt:
            raise CloudError("This is the UI workflow format. Please export in "
                             "API format (File → Export (API)).")
        tokens = {token: images.get(f"image_{n}")
                  for n, token in enumerate(TOKENS, start=1)}
        image, video, audio, text = await executor.run_raw_prompt(
            prompt, tokens, timeout_s=timeout_s, node_id=cls.hidden.unique_id)
        return io.NodeOutput(image, video, audio, text)
