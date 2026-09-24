import unittest

import _path  # noqa: F401
from schemas import CLOUD_OBJECT_INFO

from comfycloudhybrid.converter.widgets import map_widgets, widget_inputs_of


class TestWidgetMapping(unittest.TestCase):
    def test_seed_control_slot_skipped(self):
        # verified real serialization of GeminiImageNode (Change Style blueprint)
        values = ["a prompt", "gemini-2.5-flash-image", 249510346527630,
                  "randomize", "auto", "IMAGE+TEXT", "sys prompt"]
        out = map_widgets("GeminiImageNode", values,
                          CLOUD_OBJECT_INFO["GeminiImageNode"])
        self.assertEqual(out["prompt"], "a prompt")
        self.assertEqual(out["seed"], 249510346527630)
        self.assertEqual(out["aspect_ratio"], "auto")
        self.assertEqual(out["response_modalities"], "IMAGE+TEXT")
        self.assertEqual(out["system_prompt"], "sys prompt")
        self.assertNotIn("randomize", out.values())

    def test_upload_button_slot_skipped(self):
        out = map_widgets("LoadImage", ["Reference_9x16_2.png", "image"],
                          CLOUD_OBJECT_INFO["LoadImage"])
        self.assertEqual(out, {"image": "Reference_9x16_2.png"})

    def test_connection_inputs_get_no_widget_slot(self):
        names = [n for n, _, _ in widget_inputs_of(CLOUD_OBJECT_INFO["GeminiImageNode"])]
        self.assertNotIn("images", names)
        self.assertNotIn("files", names)
        self.assertEqual(names[0], "prompt")

    def test_dict_widgets_values(self):
        out = map_widgets("FakeProc", {"strength": 0.7, "bogus": 1},
                          CLOUD_OBJECT_INFO["FakeProc"])
        self.assertEqual(out, {"strength": 0.7})

    def test_mismatch_logs_but_returns(self):
        with self.assertLogs("ComfyCloudHybrid", level="WARNING"):
            out = map_widgets("FakeProc", [0.5, "unexpected", "extra"],
                              CLOUD_OBJECT_INFO["FakeProc"])
        self.assertEqual(out["strength"], 0.5)

    def test_node_inputs_array_drives_order(self):
        # dynamic combo: the node's own inputs array carries the true widget
        # order (resize_type, .width, .height, .crop, scale_method); the cloud
        # schema only lists resize_type + scale_method
        node = {
            "type": "ResizeImageMaskNode",
            "widgets_values": ["scale dimensions", 1920, 1088, "center", "lanczos"],
            "inputs": [
                {"name": "input"},
                {"name": "resize_type", "widget": {"name": "resize_type"}},
                {"name": "resize_type.width", "widget": {"name": "resize_type.width"}},
                {"name": "resize_type.height", "widget": {"name": "resize_type.height"}},
                {"name": "resize_type.crop", "widget": {"name": "resize_type.crop"}},
                {"name": "scale_method", "widget": {"name": "scale_method"}},
            ],
        }
        schema = {"input": {"required": {
            "resize_type": ["COMFY_DYNAMICCOMBO_V3", {}],
            "scale_method": [["nearest-exact", "lanczos"], {}]}}}
        out = map_widgets(node, schema_entry=schema)
        self.assertEqual(out["resize_type"], "scale dimensions")
        self.assertEqual(out["scale_method"], "lanczos")  # NOT 'scale dimensions'
        self.assertEqual(out["resize_type.width"], 1920)

    def test_composite_widget_trailing_extras_no_warning(self):
        # crop_region carries a dict value; trailing scalar sub-widgets are
        # frontend-only and must be dropped WITHOUT a false-alarm warning
        node = {
            "type": "ImageCropV2",
            "widgets_values": [{"x": 0, "y": 0, "width": 512, "height": 512}, 0, 0, 512, 512],
            "inputs": [{"name": "image"},
                       {"name": "crop_region", "widget": {"name": "crop_region"}}],
        }
        schema = {"input": {"required": {"crop_region": ["CROP_REGION", {}]}}}
        import logging
        with self.assertNoLogs("ComfyCloudHybrid", level="WARNING"):
            out = map_widgets(node, schema_entry=schema)
        self.assertEqual(out["crop_region"], {"x": 0, "y": 0, "width": 512, "height": 512})

    def test_structured_value_after_seed_or_upload(self):
        for name in ("seed", "image"):
            with self.subTest(name=name):
                region = {"x": 1, "y": 2, "width": 64, "height": 64}
                node = {
                    "type": "Composite",
                    "inputs": [{"name": n, "widget": {"name": n}}
                               for n in (name, "crop_region")],
                    "widgets_values": [42 if name == "seed" else "in.png", region],
                }
                self.assertEqual(map_widgets(node)["crop_region"], region)


if __name__ == "__main__":
    unittest.main()


class TestNamedWidgetValues(unittest.TestCase):
    """Frontend >= 1.5 serialises widgets_values_named alongside the list."""

    def test_named_values_win_over_positional_guessing(self):
        # BlockSparseAttention-style: a dynamic-combo child sits between the
        # combo and the next widget, so positional mapping shifts every value
        node = {"type": "FakeDynamic", "inputs": [{"name": "model", "link": 1}],
                "widgets_values": ["sla", 10, 0.2, 5, "randomize"],
                "widgets_values_named": {"selection": "sla", "selection.keep_percent": 10,
                                         "start_percent": 0.2, "seed": 5,
                                         "control_after_generate": "randomize"}}
        out = map_widgets(node, schema_entry=CLOUD_OBJECT_INFO["FakeDynamic"])
        self.assertEqual(out, {"selection": "sla", "selection.keep_percent": 10,
                               "start_percent": 0.2, "seed": 5})

    def test_named_unknown_and_duplicate_keys_are_dropped(self):
        node = {"type": "FakeDynamic", "inputs": [],
                "widgets_values_named": {"selection": "sla", "bogus": 1,
                                         "seed#1": 7, "seed.control_after_generate": "fixed"}}
        out = map_widgets(node, schema_entry=CLOUD_OBJECT_INFO["FakeDynamic"])
        self.assertEqual(out, {"selection": "sla"})

    def test_named_values_without_schema_pass_through(self):
        node = {"type": "UnknownClass", "inputs": [],
                "widgets_values_named": {"a": 1, "control_after_generate": "fixed"}}
        self.assertEqual(map_widgets(node, schema_entry={}), {"a": 1})

    def test_named_values_used_even_when_list_is_empty(self):
        node = {"type": "FakeDynamic", "inputs": [], "widgets_values": [],
                "widgets_values_named": {"start_percent": 0.5}}
        self.assertEqual(map_widgets(node, schema_entry=CLOUD_OBJECT_INFO["FakeDynamic"]),
                         {"start_percent": 0.5})
