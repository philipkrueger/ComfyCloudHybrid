import copy
import json
import unittest
from pathlib import Path

import _path  # noqa: F401
from schemas import CLOUD_OBJECT_INFO

from comfycloudhybrid.converter import SchemaSource, convert
from comfycloudhybrid.converter.model import SENTINEL

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def schemas():
    return SchemaSource(CLOUD_OBJECT_INFO, use_local=False)


class TestChangeStyle(unittest.TestCase):
    """Golden test against the real published blueprint."""

    @classmethod
    def setUpClass(cls):
        cls.cw = convert(load("change_style.json"), schemas())

    def test_emitted_nodes_and_stripping(self):
        classes = {e["class_type"] for e in self.cw.prompt.values()}
        # rgthree comparer is display-only and must be stripped silently
        self.assertNotIn("Image Comparer (rgthree)", classes)
        self.assertEqual(
            classes,
            {"PrimitiveStringMultiline", "GeminiImageNode", "ImageBatch",
             "LoadImage", "SaveImage"})
        # blueprint stays available — the comparer feeds nothing executable
        self.assertEqual(self.cw.missing_classes, [])

    def test_prompt_keys_use_instance_path(self):
        self.assertIn("35:31", self.cw.prompt)  # GeminiImageNode
        self.assertIn("35:34", self.cw.prompt)  # LoadImage

    def test_boundary_input_bound_to_imagebatch(self):
        slot = [i for i in self.cw.inputs if i.kind == "slot"]
        self.assertEqual(len(slot), 1)
        self.assertEqual(slot[0].name, "image2")
        self.assertEqual(slot[0].type, "IMAGE")
        self.assertIn(("35:32", "image2"), slot[0].targets)
        self.assertEqual(self.cw.prompt["35:32"]["inputs"]["image2"],
                         [SENTINEL, slot[0].safe_id])

    def test_links_resolved(self):
        gemini = self.cw.prompt["35:31"]["inputs"]
        self.assertEqual(gemini["images"], ["35:32", 0])
        self.assertEqual(gemini["prompt"], ["35:30", 0])  # link beats widget value
        self.assertEqual(gemini["seed"], 249510346527630)
        batch = self.cw.prompt["35:32"]["inputs"]
        self.assertEqual(batch["image1"], ["35:34", 0])

    def test_save_node_appended(self):
        self.assertEqual(len(self.cw.outputs), 1)
        save = self.cw.prompt[self.cw.outputs[0].save_node_key]
        self.assertEqual(save["class_type"], "SaveImage")
        self.assertEqual(save["inputs"]["images"], ["35:31", 0])

    def test_proxy_widgets(self):
        proxies = {i.name: i for i in self.cw.inputs if i.kind == "proxy"}
        # pseudo entries ("upload", "$$canvas-image-preview") filtered out
        self.assertEqual(set(proxies), {"seed", "image"})
        self.assertEqual(proxies["seed"].type, "INT")
        self.assertEqual(proxies["seed"].default, 249510346527630)
        self.assertIn(("35:31", "seed"), proxies["seed"].targets)
        self.assertEqual(proxies["image"].type, "UPLOAD_COMBO")
        self.assertEqual(proxies["image"].default, "Reference_9x16_2.png")

    def test_seed_gets_cloud_range(self):
        # the noise-seed trap: cloud requires 0..2^64-1, so a randomized
        # negative seed must be impossible by construction
        seed = {i.name: i for i in self.cw.inputs if i.kind == "proxy"}["seed"]
        self.assertEqual(seed.minimum, 0)
        self.assertEqual(seed.maximum, 18446744073709551615)
        self.assertTrue(seed.control_after_generate)

    def test_fixed_file_covered_by_proxy(self):
        # LoadImage.image is proxied → runtime upload path, not required_uploads
        self.assertEqual(self.cw.required_uploads, [])

    def test_serialization_roundtrip(self):
        from comfycloudhybrid.converter.model import ConvertedWorkflow
        again = ConvertedWorkflow.from_dict(
            json.loads(json.dumps(self.cw.to_dict())))
        self.assertEqual(again.prompt, self.cw.prompt)
        self.assertEqual([i.safe_id for i in again.inputs],
                         [i.safe_id for i in self.cw.inputs])


class TestNested(unittest.TestCase):
    def test_nested_expansion(self):
        cw = convert(load("nested_subgraph.json"), schemas())
        self.assertIn("99:1:5", cw.prompt)
        self.assertEqual(cw.prompt["99:1:5"]["class_type"], "FakeProc")
        self.assertEqual(cw.prompt["99:1:5"]["inputs"]["strength"], 0.5)
        slot = [i for i in cw.inputs if i.kind == "slot"][0]
        self.assertIn(("99:1:5", "image"), slot.targets)
        save = cw.prompt[cw.outputs[0].save_node_key]
        self.assertEqual(save["inputs"]["images"], ["99:1:5", 0])
        self.assertEqual(cw.description, "Nested test blueprint")


class TestBoundaryFixes(unittest.TestCase):
    """Regressions from the first live run (Flux.2 Dev blueprint)."""

    def _nested_with_value_slot(self):
        bp = load("nested_subgraph.json")
        outer = bp["definitions"]["subgraphs"][0]
        inner_def = bp["definitions"]["subgraphs"][1]
        # expose FakeProc.strength as a boundary input of the INNER subgraph
        inner_def["inputs"].append(
            {"id": "i3", "name": "strength", "type": "FLOAT", "linkIds": [22]})
        inner_def["links"].append(
            {"id": 22, "origin_id": -10, "origin_slot": 1,
             "target_id": 5, "target_slot": 1, "type": "FLOAT"})
        inner_def["nodes"][0]["inputs"][1]["link"] = 22
        # ...and pass it through the outer subgraph too
        outer["inputs"].append(
            {"id": "i4", "name": "strength", "type": "FLOAT", "linkIds": [12]})
        outer_inst = outer["nodes"][0]
        outer_inst["inputs"].append(
            {"name": "strength", "type": "FLOAT",
             "widget": {"name": "strength"}, "link": 12})
        outer["links"].append(
            {"id": 12, "origin_id": -10, "origin_slot": 1,
             "target_id": 1, "target_slot": 1, "type": "FLOAT"})
        return bp

    def test_multi_type_input_becomes_image(self):
        bp = load("nested_subgraph.json")
        for d in bp["definitions"]["subgraphs"]:
            for i in d["inputs"]:
                if i["type"] == "IMAGE":
                    i["type"] = "IMAGE,MASK"
        cw = convert(bp, schemas())
        slot = [i for i in cw.inputs if i.kind == "slot"][0]
        self.assertEqual(slot.type, "IMAGE")

    def test_value_slot_default_from_inner_widget(self):
        cw = convert(self._nested_with_value_slot(), schemas())
        strength = [i for i in cw.inputs if i.name == "strength"][0]
        # default must be the blueprint's baked 0.5, never 0/None —
        # otherwise the slot overrides the inner value in the cloud
        self.assertEqual(strength.default, 0.5)
        self.assertIn(("99:1:5", "strength"), strength.targets)

    def test_proxy_duplicate_of_slot_removed(self):
        # change_style already promotes GeminiImageNode.seed as a proxy;
        # additionally expose it as a boundary slot → only the slot survives
        bp = load("change_style.json")
        root_def = bp["definitions"]["subgraphs"][0]
        root_def["inputs"].append(
            {"id": "i9", "name": "seed", "type": "INT", "linkIds": [99]})
        root_def["links"].append(
            {"id": 99, "origin_id": -10, "origin_slot": 1,
             "target_id": 31, "target_slot": 4, "type": "INT"})
        gemini = [n for n in root_def["nodes"] if n["id"] == 31][0]
        assert gemini["inputs"][4]["name"] == "seed"
        gemini["inputs"][4]["link"] = 99
        cw = convert(bp, schemas())
        seeds = [i for i in cw.inputs if i.name == "seed"]
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0].kind, "slot")
        # slot default pulled from the inner baked widget value
        self.assertEqual(seeds[0].default, 249510346527630)


class TestLocalCapable(unittest.TestCase):
    def test_model_free_blueprint_flagged(self):
        # FakeProc / CreateVideo / PrimitiveInt need no AI weights
        cw = convert(load("nested_subgraph.json"), schemas())
        self.assertTrue(cw.local_capable)
        cw = convert(load("video_blueprint.json"), schemas())
        self.assertTrue(cw.local_capable)

    def test_api_node_blueprint_not_local(self):
        # change_style contains GeminiImageNode (api_node: true in catalog)
        cw = convert(load("change_style.json"), schemas())
        self.assertFalse(cw.local_capable)

    def test_loader_class_not_local(self):
        bp = load("nested_subgraph.json")
        bp["definitions"]["subgraphs"][1]["nodes"][0]["type"] = "FakeLoader"
        import copy
        from schemas import CLOUD_OBJECT_INFO
        info = copy.deepcopy(CLOUD_OBJECT_INFO)
        info["FakeLoader"] = info["FakeProc"]
        cw = convert(bp, SchemaSource(info, use_local=False))
        self.assertFalse(cw.local_capable)


class TestOptionalInputs(unittest.TestCase):
    """Qwen-2509-style optional boundary images (image2/image3)."""

    def _with_optional_input(self, label):
        bp = load("nested_subgraph.json")
        outer = bp["definitions"]["subgraphs"][0]
        inner_def = bp["definitions"]["subgraphs"][1]
        # expose FakeProc.image_b (optional in cloud schema) through both levels
        inner_def["inputs"].append(
            {"id": "io1", "name": "image_b", "label": label,
             "type": "IMAGE", "linkIds": [23]})
        inner_def["nodes"][0]["inputs"].append(
            {"name": "image_b", "type": "IMAGE", "link": 23})
        inner_def["links"].append(
            {"id": 23, "origin_id": -10, "origin_slot": 1,
             "target_id": 5, "target_slot": 2, "type": "IMAGE"})
        outer["inputs"].append(
            {"id": "io2", "name": "image_b", "label": label,
             "type": "IMAGE", "linkIds": [13]})
        outer["nodes"][0]["inputs"].append(
            {"name": "image_b", "type": "IMAGE", "link": 13})
        outer["links"].append(
            {"id": 13, "origin_id": -10, "origin_slot": 1,
             "target_id": 1, "target_slot": 1, "type": "IMAGE"})
        return bp

    def test_label_marks_optional_and_is_cleaned(self):
        cw = convert(self._with_optional_input("image_b (optional)"), schemas())
        bi = [i for i in cw.inputs if i.safe_id == "image_b"][0]
        self.assertTrue(bi.optional)
        self.assertEqual(bi.name, "image_b")  # "(optional)" stripped

    def test_cloud_schema_marks_optional_without_label(self):
        cw = convert(self._with_optional_input(None), schemas())
        bi = [i for i in cw.inputs if i.safe_id == "image_b"][0]
        # FakeProc.image_b sits in the optional section of the cloud schema
        self.assertTrue(bi.optional)
        # the required image input must stay required
        img = [i for i in cw.inputs if i.safe_id == "image"][0]
        self.assertFalse(img.optional)


class TestVideoOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cw = convert(load("video_blueprint.json"), schemas())

    def test_video_output_gets_save_video(self):
        self.assertEqual(len(self.cw.outputs), 1)
        bo = self.cw.outputs[0]
        self.assertEqual(bo.type, "VIDEO")
        save = self.cw.prompt[bo.save_node_key]
        self.assertEqual(save["class_type"], "SaveVideo")
        self.assertEqual(save["inputs"]["video"], ["50:20", 0])
        self.assertEqual(save["inputs"]["format"], "auto")

    def test_reroute_collapsed(self):
        # insert a Reroute between CreateVideo and the video output boundary
        bp = load("video_blueprint.json")
        root = bp["definitions"]["subgraphs"][0]
        root["nodes"].append({
            "id": 40, "type": "Reroute",
            "inputs": [{"name": "", "type": "*", "link": 3}],
            "outputs": [{"name": "", "type": "*", "links": [4]}],
            "properties": {}, "widgets_values": []})
        # rewire: CreateVideo(20) → Reroute(40) → -20
        for l in root["links"]:
            if l["id"] == 3:
                l["target_id"], l["target_slot"] = 40, 0
        root["links"].append({"id": 4, "origin_id": 40, "origin_slot": 0,
                              "target_id": -20, "target_slot": 0, "type": "VIDEO"})
        cw = convert(bp, schemas())
        # Reroute is missing from the cloud catalog but must NOT make the
        # blueprint unavailable — it is collapsed, output wired to CreateVideo
        self.assertEqual(cw.missing_classes, [])
        self.assertNotIn("40", str(cw.prompt.keys()))
        self.assertEqual(cw.prompt[cw.outputs[0].save_node_key]["inputs"]["video"],
                         ["50:20", 0])

    def test_primitive_int_not_randomized(self):
        # width proxies a PrimitiveInt 'value' whose cloud schema allows
        # control_after_generate — but only seeds may randomize
        value = {i.name: i for i in self.cw.inputs if i.kind == "proxy"}["value"]
        self.assertEqual(value.type, "INT")
        self.assertFalse(value.control_after_generate)
        self.assertEqual(value.default, 768)
        # min/max still captured for validation
        self.assertEqual(value.minimum, -2147483648)


class TestMaskOutput(unittest.TestCase):
    """A subgraph MASK output must survive conversion: saved via
    MaskToImage → SaveImage, declared as a MASK output on the node."""

    @classmethod
    def setUpClass(cls):
        cls.cw = convert(load("mask_blueprint.json"), schemas())

    def test_mask_output_is_kept(self):
        self.assertEqual(len(self.cw.outputs), 1)
        self.assertEqual(self.cw.outputs[0].type, "MASK")

    def test_mask_saved_via_masktoimage(self):
        bo = self.cw.outputs[0]
        save = self.cw.prompt[bo.save_node_key]
        self.assertEqual(save["class_type"], "SaveImage")
        # SaveImage pulls from an injected MaskToImage node, not the mask link
        conv_key = save["inputs"]["images"][0]
        conv = self.cw.prompt[conv_key]
        self.assertEqual(conv["class_type"], "MaskToImage")
        # MaskToImage is fed by the real mask producer (ImageToMask)
        self.assertEqual(self.cw.prompt[conv["inputs"]["mask"][0]]["class_type"],
                         "ImageToMask")

    def test_mask_output_does_not_flag_blueprint_unavailable(self):
        self.assertEqual(self.cw.missing_classes, [])


class TestComboSlots(unittest.TestCase):
    """Model-selector boundary slots (unet_name etc.) must become dropdowns
    fed by the cloud catalog, not copy-the-exact-name text fields."""

    def _with_combo_slot(self):
        bp = load("nested_subgraph.json")
        outer = bp["definitions"]["subgraphs"][0]
        inner_def = bp["definitions"]["subgraphs"][1]
        inner_def["inputs"].append(
            {"id": "ic1", "name": "preset", "type": "COMBO", "linkIds": [25]})
        inner_def["nodes"][0]["inputs"].append(
            {"name": "preset", "type": "COMBO",
             "widget": {"name": "preset"}, "link": 25})
        inner_def["links"].append(
            {"id": 25, "origin_id": -10, "origin_slot": 1,
             "target_id": 5, "target_slot": 2, "type": "COMBO"})
        outer["inputs"].append(
            {"id": "oc1", "name": "preset", "type": "COMBO", "linkIds": [15]})
        outer["nodes"][0]["inputs"].append(
            {"name": "preset", "type": "COMBO",
             "widget": {"name": "preset"}, "link": 15})
        outer["links"].append(
            {"id": 15, "origin_id": -10, "origin_slot": 1,
             "target_id": 1, "target_slot": 1, "type": "COMBO"})
        return bp

    def test_combo_slot_resolves_cloud_options(self):
        import copy
        from schemas import CLOUD_OBJECT_INFO
        info = copy.deepcopy(CLOUD_OBJECT_INFO)
        info["FakeProc"]["input"]["required"]["preset"] = (
            [["fast", "quality"], {"default": "fast"}])
        cw = convert(self._with_combo_slot(), SchemaSource(info, use_local=False))
        preset = [i for i in cw.inputs if i.safe_id == "preset"][0]
        self.assertEqual(preset.type, "COMBO")
        self.assertEqual(preset.combo_options, ["fast", "quality"])
        self.assertIn(("99:1:5", "preset"), preset.targets)

    def test_combo_slot_without_cloud_options_falls_back_to_string(self):
        # target input unknown to the cloud catalog → free-text stays usable
        cw = convert(self._with_combo_slot(), schemas())
        preset = [i for i in cw.inputs if i.safe_id == "preset"][0]
        self.assertEqual(preset.type, "STRING")


class TestSchemaDrift(unittest.TestCase):
    """A class gains a new required input after a blueprint was authored
    (real case: SDPoseDrawKeypoints.draw_head, added post-authoring — the
    node's serialized inputs/widgets_values in the blueprint never mention
    it, and the cloud rejects a submit missing a required input outright)."""

    def test_missing_required_input_backfilled_from_cloud_default(self):
        import copy
        from schemas import CLOUD_OBJECT_INFO
        info = copy.deepcopy(CLOUD_OBJECT_INFO)
        info["FakeProc"]["input"]["required"]["use_gpu"] = (
            ["BOOLEAN", {"default": True}])
        cw = convert(load("nested_subgraph.json"), SchemaSource(info, use_local=False))
        self.assertEqual(cw.prompt["99:1:5"]["inputs"]["use_gpu"], True)
        self.assertTrue(any("use_gpu" in w and "cloud default" in w
                            for w in cw.warnings))
        # the blueprint stays available — a backfilled default is not a
        # missing-node situation
        self.assertEqual(cw.missing_classes, [])

    def test_required_input_without_default_not_backfilled(self):
        # a required input with NO schema default can't be safely invented —
        # must be left for the existing missing-class/validation path, not
        # silently filled with None
        import copy
        from schemas import CLOUD_OBJECT_INFO
        info = copy.deepcopy(CLOUD_OBJECT_INFO)
        info["FakeProc"]["input"]["required"]["mode"] = (
            [["a", "b"], {}])  # no "default" key
        cw = convert(load("nested_subgraph.json"), SchemaSource(info, use_local=False))
        self.assertNotIn("mode", cw.prompt["99:1:5"]["inputs"])


class TestUnavailable(unittest.TestCase):
    def test_missing_class_feeding_executable_node(self):
        bp = load("nested_subgraph.json")
        # rename the inner class to something unknown
        bp["definitions"]["subgraphs"][1]["nodes"][0]["type"] = "TotallyUnknownNode"
        cw = convert(bp, schemas())
        self.assertEqual(cw.missing_classes, ["TotallyUnknownNode"])
        self.assertFalse(cw.available)

    def test_known_locally_but_not_in_cloud(self):
        info = copy.deepcopy(CLOUD_OBJECT_INFO)
        local_only = info.pop("FakeProc")

        class LocalFallback(SchemaSource):
            def _get_local(self, class_type):
                return local_only if class_type == "FakeProc" else None

        cw = convert(load("nested_subgraph.json"), LocalFallback(info, use_local=True))
        self.assertEqual(cw.missing_classes, ["FakeProc"])


if __name__ == "__main__":
    unittest.main()
