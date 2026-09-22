"""Frontend 1.51: promoted inputs carry per-instance widgets_values."""

import json
import unittest
from pathlib import Path

import _path  # noqa: F401
from schemas import CLOUD_OBJECT_INFO
from comfycloudhybrid.converter import SchemaSource, convert
from comfycloudhybrid.converter.model import SENTINEL


def blueprint():
    bp = json.loads((Path(__file__).parent / "fixtures/nested_subgraph.json").read_text())
    outer, inner = bp["definitions"]["subgraphs"]
    # Promote the inner strength widget to a real boundary input, as current
    # ComfyUI does. The shared definition retains 0.5 as its original value.
    inner["inputs"].append({"name": "strength", "type": "FLOAT", "linkIds": [22]})
    inner["links"].append({"id": 22, "origin_id": -10, "origin_slot": 1,
                           "target_id": 5, "target_slot": 1, "type": "FLOAT"})
    inner["nodes"][0]["inputs"][1]["link"] = 22
    nested = outer["nodes"][0]
    nested["inputs"].append({"name": "strength", "type": "FLOAT", "link": None,
                             "widget": {"name": "strength"}})
    nested["widgets_values"] = [0.8]
    return bp


class CurrentSubgraphTest(unittest.TestCase):
    def convert(self, bp):
        return convert(bp, SchemaSource(CLOUD_OBJECT_INFO, use_local=False))

    def test_nested_instance_value_overrides_shared_definition(self):
        cw = self.convert(blueprint())
        self.assertEqual(cw.prompt["99:1:5"]["inputs"]["strength"], 0.8)

    def test_root_widget_default_and_link_take_precedence(self):
        bp = blueprint()
        outer = bp["definitions"]["subgraphs"][0]
        outer["inputs"].append({"name": "amount", "type": "FLOAT", "linkIds": [12]})
        outer["links"].append({"id": 12, "origin_id": -10, "origin_slot": 1,
                               "target_id": 1, "target_slot": 1, "type": "FLOAT"})
        outer["nodes"][0]["inputs"][1]["link"] = 12
        bp["nodes"][0]["inputs"].append({"name": "amount", "type": "FLOAT",
                                          "widget": {"name": "amount"}})
        for value in (0.0, 0.9):
            with self.subTest(value=value):
                bp["nodes"][0]["widgets_values"] = [value]
                cw = self.convert(bp)
                amount = next(i for i in cw.inputs if i.name == "amount")
                self.assertEqual(amount.default, value)
                self.assertEqual(amount.targets, [("99:1:5", "strength")])
                self.assertEqual(cw.prompt["99:1:5"]["inputs"]["strength"],
                                 [SENTINEL, amount.safe_id])

    def test_missing_nested_value_keeps_inner_default(self):
        bp = blueprint()
        bp["definitions"]["subgraphs"][0]["nodes"][0]["widgets_values"] = []
        self.assertEqual(self.convert(bp).prompt["99:1:5"]["inputs"]["strength"], 0.5)

    def test_modern_promoted_upload_stays_an_upload(self):
        bp = blueprint()
        inner = bp["definitions"]["subgraphs"][1]
        bp["nodes"] = [{"id": 99, "type": inner["id"],
                        "inputs": [{"name": "image", "type": "COMBO",
                                    "widget": {"name": "image"}}],
                        "widgets_values": ["edited.png"]}]
        inner["inputs"] = [{"name": "image", "type": "COMBO"}]
        inner["nodes"] = [{"id": 5, "type": "LoadImage",
                           "inputs": [{"name": "image", "link": 20,
                                       "widget": {"name": "image"}}],
                           "widgets_values": ["original.png", "image"]}]
        inner["links"] = inner["links"][:2]
        cw = self.convert(bp)
        self.assertEqual(cw.inputs[0].type, "UPLOAD_COMBO")
        self.assertEqual(cw.inputs[0].default, "edited.png")
        self.assertEqual(cw.required_uploads, [])

    def test_bypassed_unknown_class_forwards_matching_input(self):
        bp = blueprint()
        inner = bp["definitions"]["subgraphs"][1]
        inner["nodes"].append({"id": 6, "type": "LocalOnlyNode", "mode": 4,
                               "inputs": [{"name": "count", "type": "INT"},
                                          {"name": "image", "type": "IMAGE", "link": 23}],
                               "outputs": [{"name": "image", "type": "IMAGE"}]})
        inner["links"][1]["origin_id"] = 6
        inner["links"].append({"id": 23, "origin_id": 5, "origin_slot": 0,
                               "target_id": 6, "target_slot": 1, "type": "IMAGE"})
        cw = self.convert(bp)
        self.assertNotIn("99:1:6", cw.prompt)
        self.assertEqual(cw.missing_classes, [])
        self.assertEqual(cw.prompt["cch_save_0"]["inputs"]["images"], ["99:1:5", 0])

    def test_virtual_primitive_injects_value_without_cloud_class(self):
        bp = blueprint()
        inner = bp["definitions"]["subgraphs"][1]
        inner["nodes"].append({"id": 6, "type": "PrimitiveNode",
                               "outputs": [{"type": "FLOAT"}],
                               "widgets_values": [0.25, "fixed"]})
        inner["links"][-1]["origin_id"] = 6
        inner["links"][-1]["origin_slot"] = 0
        cw = self.convert(bp)
        self.assertNotIn("99:1:6", cw.prompt)
        self.assertEqual(cw.missing_classes, [])
        self.assertEqual(cw.prompt["99:1:5"]["inputs"]["strength"], 0.25)
