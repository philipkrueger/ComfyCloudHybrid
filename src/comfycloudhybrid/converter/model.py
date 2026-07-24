from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SENTINEL = "__CCH_INPUT__"
"""Placeholder origin id written into prompt links for subgraph inputs;
replaced by the executor with an injected loader node or a literal."""


class BlueprintFormatError(Exception):
    """File is not a valid single-instance subgraph blueprint."""


class UnsupportedNodeError(Exception):
    """A node class required for execution is unknown/not runnable in cloud."""

    def __init__(self, class_type: str):
        super().__init__(f"unsupported node class: {class_type}")
        self.class_type = class_type


class UnsupportedTypeError(Exception):
    """A subgraph boundary uses a type we cannot ship across (LATENT etc.)."""


VALUE_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"}
# boundary types shipped as uploaded/downloaded files with a tensor(-dict)
# representation locally: IMAGE/MASK as PNG, AUDIO as FLAC
TENSOR_TYPES = {"IMAGE", "MASK", "AUDIO"}

# output types transferred back through PreviewAny's text channel: the cloud
# job's history carries {"text": [...]} per node — strings verbatim, numbers
# via str(), structured JSON-safe data (BOUNDING_BOX dicts/lists) via
# json.dumps. The executor parses them back (see executor._parse_value_output).
VALUE_OUTPUT_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN", "BOUNDING_BOX"}


@dataclass
class BoundInput:
    name: str                       # display name (original)
    safe_id: str                    # python-identifier-safe unique id (execute kwarg)
    type: str                       # IMAGE|MASK|STRING|INT|FLOAT|BOOLEAN|COMBO|UPLOAD_COMBO
    kind: str                       # "slot" (subgraph boundary) | "proxy" (promoted widget)
    targets: list[tuple[str, str]] = field(default_factory=list)  # (prompt_key, input_name)
    default: Any = None
    combo_options: list | None = None
    optional: bool = False
    # numeric constraints pulled from the CLOUD schema of the target input —
    # so randomized seeds etc. stay inside the range the cloud will accept
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    control_after_generate: bool = False


@dataclass
class BoundOutput:
    name: str
    type: str
    save_node_key: str


@dataclass
class ConvertedWorkflow:
    name: str
    prompt: dict
    inputs: list[BoundInput]
    outputs: list[BoundOutput]
    required_uploads: list[tuple[str, str, str]]  # (prompt_key, input_name, local filename)
    missing_classes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    description: str = ""
    local_capable: bool = False
    """True when no node needs AI weights or a partner API — the blueprint
    runs locally on any machine, so cloud offloading is pointless."""

    @property
    def available(self) -> bool:
        return not self.missing_classes

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "inputs": [vars(i) for i in self.inputs],
            "outputs": [vars(o) for o in self.outputs],
            "required_uploads": [list(u) for u in self.required_uploads],
            "missing_classes": self.missing_classes,
            "warnings": self.warnings,
            "description": self.description,
            "local_capable": self.local_capable,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConvertedWorkflow":
        return cls(
            name=d["name"],
            prompt=d["prompt"],
            inputs=[BoundInput(**{**i, "targets": [tuple(t) for t in i["targets"]]})
                    for i in d["inputs"]],
            outputs=[BoundOutput(**o) for o in d["outputs"]],
            required_uploads=[tuple(u) for u in d["required_uploads"]],
            missing_classes=d.get("missing_classes", []),
            warnings=d.get("warnings", []),
            description=d.get("description", ""),
            local_capable=d.get("local_capable", False),
        )


def sanitize_id(name: str, taken: set[str]) -> str:
    safe = re.sub(r"\W", "_", str(name)).strip("_") or "input"
    if safe[0].isdigit():
        safe = "in_" + safe
    base, n = safe, 2
    while safe in taken:
        safe = f"{base}_{n}"
        n += 1
    taken.add(safe)
    return safe
