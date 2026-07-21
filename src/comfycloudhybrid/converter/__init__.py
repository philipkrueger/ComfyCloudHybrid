"""Blueprint (UI-format workflow with subgraph definitions) → flat API-format
prompt plus binding metadata for runtime input/output injection."""

from .model import (
    BlueprintFormatError,
    BoundInput,
    BoundOutput,
    ConvertedWorkflow,
    UnsupportedNodeError,
    UnsupportedTypeError,
)
from .flatten import convert
from .schema_source import SchemaSource

__all__ = [
    "convert",
    "ConvertedWorkflow",
    "BoundInput",
    "BoundOutput",
    "SchemaSource",
    "BlueprintFormatError",
    "UnsupportedNodeError",
    "UnsupportedTypeError",
]

CONVERTER_VERSION = 12
"""Bump on any change to conversion output — busts the converted-workflow cache."""
