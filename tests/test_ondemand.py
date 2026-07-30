import copy
import json
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from schemas import CLOUD_OBJECT_INFO

from comfycloudhybrid import config, ondemand
from comfycloudhybrid.converter import SchemaSource, convert
from comfycloudhybrid.converter.model import SENTINEL

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def schemas():
    return SchemaSource(CLOUD_OBJECT_INFO, use_local=False)


class TestPreflightValid(unittest.TestCase):
    def test_image_to_mask_is_ok_and_instant_testable(self):
        r = ondemand.preflight(load("mask_blueprint.json"), schemas())
        self.assertTrue(r["ok"])
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["instant_testable"])
        self.assertEqual([o["type"] for o in r["outputs"]], ["MASK"])
        self.assertEqual([i["token"] for i in r["image_inputs"]], ["%CCH_IMAGE_1%"])

    def test_generic_json_has_token_and_no_sentinel(self):
        r = ondemand.preflight(load("video_blueprint.json"), schemas())
        self.assertTrue(r["instant_testable"])
        prompt = json.loads(r["generic_json"])
        blob = json.dumps(prompt)
        self.assertNotIn(SENTINEL, blob)            # every input was substituted
        self.assertIn("%CCH_IMAGE_1%", blob)        # image slot became a token
        # promoted widgets were baked to their defaults
        names = {b["name"] for b in r["baked_inputs"]}
        self.assertIn("fps", names)

    def test_baked_inputs_carry_widget_metadata(self):
        # the frontend builds editable widgets from these entries: they must
        # name the type and the JSON targets the value is written to
        r = ondemand.preflight(load("video_blueprint.json"), schemas())
        baked = {b["name"]: b for b in r["baked_inputs"]}
        fps = baked["fps"]
        self.assertEqual(fps["type"], "FLOAT")
        self.assertTrue(fps["targets"], "targets missing")
        key, iname = fps["targets"][0]
        prompt = json.loads(r["generic_json"])
        self.assertEqual(prompt[key]["inputs"][iname], fps["value"])

    def test_upload_combo_blocks_instant_but_not_generation(self):
        # change_style carries a fixed LoadImage file (UPLOAD_COMBO) the generic
        # runner cannot upload — valid node, but not instant-testable
        r = ondemand.preflight(load("change_style.json"), schemas())
        self.assertTrue(r["ok"])
        self.assertFalse(r["instant_testable"])
        self.assertIn("UPLOAD_COMBO", r["generic_reason"])


class TestPreflightBlocks(unittest.TestCase):
    def test_missing_cloud_class_is_an_error(self):
        bp = load("mask_blueprint.json")
        # rename the only executable node to a class the cloud does not have
        bp["definitions"]["subgraphs"][0]["nodes"][0]["type"] = "TotallyUnknownNode"
        r = ondemand.preflight(bp, schemas())
        self.assertFalse(r["ok"])
        self.assertFalse(r["instant_testable"])
        self.assertIn("TotallyUnknownNode", r["missing_classes"])
        self.assertTrue(any("could not run" in e for e in r["errors"]))

    def test_unsupported_boundary_type_is_an_error(self):
        bp = load("mask_blueprint.json")
        bp["definitions"]["subgraphs"][0]["inputs"][0]["type"] = "LATENT"
        r = ondemand.preflight(bp, schemas())
        self.assertFalse(r["ok"])
        self.assertTrue(r["errors"])

    def test_malformed_blueprint_is_an_error_not_a_crash(self):
        r = ondemand.preflight({}, schemas())
        self.assertFalse(r["ok"])
        self.assertTrue(r["errors"])

    def test_no_cloud_catalog_adds_a_warning(self):
        r = ondemand.preflight(load("mask_blueprint.json"),
                               SchemaSource({}, use_local=True))
        # local fallback lacks the class → blocked, and the catalog hint is present
        self.assertTrue(any("cloud catalog" in w for w in r["warnings"]))


def _arrayify_links(sg):
    """Simulate LiteGraph's live serialize(): links as positional arrays."""
    sg["links"] = [[l["id"], l["origin_id"], l["origin_slot"],
                    l["target_id"], l["target_slot"], l["type"]]
                   for l in sg["links"]]


class TestLiveSerializationFormat(unittest.TestCase):
    """The canvas right-click sends LiteGraph's live serialisation, where
    links are positional arrays — not the dict form of blueprint files."""

    def test_array_form_links_convert_fine(self):
        bp = load("mask_blueprint.json")
        _arrayify_links(bp["definitions"]["subgraphs"][0])
        r = ondemand.preflight(bp, schemas())
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(r["instant_testable"])
        self.assertEqual([o["type"] for o in r["outputs"]], ["MASK"])

    def test_save_normalizes_to_dict_links(self):
        # a saved file must be scanner/convert-compatible on the next startup,
        # so array links are persisted in dict form
        bp = load("mask_blueprint.json")
        _arrayify_links(bp["definitions"]["subgraphs"][0])
        with tempfile.TemporaryDirectory() as tmp:
            orig = config.SAVED_DIR
            config.SAVED_DIR = Path(tmp)
            try:
                info = ondemand.save_blueprint(bp, "arr")
                saved = json.loads(Path(info["path"]).read_text())
                for l in saved["definitions"]["subgraphs"][0]["links"]:
                    self.assertIsInstance(l, dict)
                    self.assertIn("origin_id", l)
            finally:
                config.SAVED_DIR = orig

    def test_no_output_error_names_the_skip_reason(self):
        # disconnect the MASK boundary: the error must say WHY (unconnected),
        # not just "no usable output"
        bp = load("mask_blueprint.json")
        sg = bp["definitions"]["subgraphs"][0]
        sg["links"] = [l for l in sg["links"] if l["target_id"] != -20]
        r = ondemand.preflight(bp, schemas())
        self.assertFalse(r["ok"])
        self.assertTrue(any("not connected" in e for e in r["errors"]), r["errors"])

    def test_crash_dump_written_on_unexpected_failure(self):
        # generic converter crashes must produce the debug dump for bug reports
        bp = {"definitions": {"subgraphs": [{"id": "x", "nodes": []}]},
              "nodes": "boom"}  # str instead of list → unexpected TypeError
        with tempfile.TemporaryDirectory() as tmp:
            orig_cache, orig_dump = config.CACHE_DIR, ondemand.DEBUG_DUMP
            config.CACHE_DIR = Path(tmp)
            ondemand.DEBUG_DUMP = Path(tmp) / "convert_debug.json"
            try:
                r = ondemand.preflight(bp, schemas())
                self.assertFalse(r["ok"])
                self.assertTrue(any("convert_debug.json" in e for e in r["errors"]))
                dump = json.loads(ondemand.DEBUG_DUMP.read_text())
                self.assertIn("Traceback", dump["error"])
                self.assertEqual(dump["blueprint"]["nodes"], "boom")
            finally:
                config.CACHE_DIR, ondemand.DEBUG_DUMP = orig_cache, orig_dump


def _live_ify(bp):
    """Mimic Comfy Desktop's live serialize(): the def carries no boundary
    declaration (inputs/outputs/inputNode/outputNode null) and array links;
    the instance node carries the slot names/types."""
    sg = bp["definitions"]["subgraphs"][0]
    _arrayify_links(sg)
    sg["inputs"] = None
    sg["outputs"] = None
    sg["inputNode"] = None
    sg["outputNode"] = None
    return bp


class TestLiveDesktopFormat(unittest.TestCase):
    """Comfy Desktop sends defs without boundary declarations — they must be
    reconstructed from the instance node (observed live payload 2026-07)."""

    def test_reconstructed_boundaries_convert(self):
        r = ondemand.preflight(_live_ify(load("mask_blueprint.json")), schemas())
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(r["instant_testable"])
        self.assertEqual([o["type"] for o in r["outputs"]], ["MASK"])
        self.assertEqual([i["name"] for i in r["inputs"]], ["image"])

    def test_proxy_widget_inputs_do_not_become_slots(self):
        # instance inputs beyond the highest -10 slot are promoted widgets,
        # not boundary slots — reconstruction must cut them off
        bp = _live_ify(load("mask_blueprint.json"))
        bp["nodes"][0]["inputs"].append(
            {"name": "channel", "type": "COMBO", "widget": {"name": "channel"},
             "link": None})
        r = ondemand.preflight(bp, schemas())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual([i["name"] for i in r["inputs"] if i["kind"] == "slot"],
                         ["image"])


class TestUnsupportedInputPolicy(unittest.TestCase):
    """Non-transferable boundary input types: dropped when every target is
    optional in the cloud schema, hard error when a required input depends."""

    def _with_bbox_input(self, target_slot, target_name):
        bp = load("mask_blueprint.json")
        sg = bp["definitions"]["subgraphs"][0]
        sg["inputs"].append({"id": "mi2", "name": "bboxes",
                             "type": "BOUNDING_BOX", "linkIds": [9]})
        node = sg["nodes"][0]  # ImageToMask
        while len(node["inputs"]) <= target_slot:
            node["inputs"].append({"name": target_name, "type": "BOUNDING_BOX",
                                   "link": None})
        node["inputs"][target_slot]["link"] = 9
        sg["links"].append({"id": 9, "origin_id": -10, "origin_slot": 1,
                            "target_id": 20, "target_slot": target_slot,
                            "type": "BOUNDING_BOX"})
        return bp

    def test_optional_target_drops_input_with_hint(self):
        sc = copy.deepcopy(CLOUD_OBJECT_INFO)
        sc["ImageToMask"]["input"].setdefault("optional", {})["bboxes"] = \
            ["BOUNDING_BOX", {}]
        r = ondemand.preflight(self._with_bbox_input(2, "bboxes"),
                               SchemaSource(sc, use_local=False))
        self.assertTrue(r["ok"], r["errors"])
        self.assertNotIn("bboxes", [i["name"] for i in r["inputs"]])
        self.assertTrue(any("cannot cross" in w for w in r["warnings"]))

    def test_required_target_rejects_blueprint(self):
        # wire the BOUNDING_BOX boundary into required 'channel' → dysfunctional
        r = ondemand.preflight(self._with_bbox_input(1, "channel"), schemas())
        self.assertFalse(r["ok"])
        self.assertTrue(any("required input depends" in e for e in r["errors"]),
                        r["errors"])


def _string_blueprint():
    """Minimal subgraph whose only output is a STRING (caption-style)."""
    uid = "dddddddd-0000-0000-0000-000000000001"
    return {
        "version": 0.4, "links": [], "extra": {},
        "nodes": [{"id": 50, "type": uid, "inputs": [],
                   "outputs": [{"name": "RESPONSE", "type": "STRING", "links": []}],
                   "properties": {}, "widgets_values": []}],
        "definitions": {"subgraphs": [{
            "id": uid, "name": "Tiny Text", "version": 1,
            "inputNode": {"id": -10}, "outputNode": {"id": -20},
            "inputs": [], "widgets": [], "groups": [], "extra": {},
            "outputs": [{"id": "to1", "name": "RESPONSE", "type": "STRING",
                         "linkIds": [3]}],
            "nodes": [{"id": 10, "type": "PrimitiveStringMultiline",
                       "inputs": [{"name": "value", "type": "STRING",
                                   "widget": {"name": "value"}, "link": None}],
                       "outputs": [{"name": "STRING", "type": "STRING",
                                    "links": [3]}],
                       "properties": {}, "widgets_values": ["hello"]}],
            "links": [{"id": 3, "origin_id": 10, "origin_slot": 0,
                       "target_id": -20, "target_slot": 0, "type": "STRING"}],
        }]},
    }


class TestValueOutputs(unittest.TestCase):
    """STRING/INT/FLOAT/BOOLEAN/BOUNDING_BOX outputs travel back through the
    PreviewAny text channel instead of a saved file."""

    def test_string_output_is_transferable(self):
        r = ondemand.preflight(_string_blueprint(), schemas())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["outputs"], [{"name": "RESPONSE", "type": "STRING"}])

    def test_string_output_uses_previewany(self):
        cw = convert(_string_blueprint(), schemas())
        save = cw.prompt[cw.outputs[0].save_node_key]
        self.assertEqual(save["class_type"], "PreviewAny")
        self.assertEqual(save["inputs"]["source"], ["50:10", 0])

    def test_value_only_subgraph_not_instant_testable(self):
        # the generic runner returns images — value outputs need the full node
        r = ondemand.preflight(_string_blueprint(), schemas())
        self.assertFalse(r["instant_testable"])
        self.assertIn("image/video output", r["generic_reason"])


def _audio_blueprint():
    """AUDIO in → FakeAudioProc → AUDIO out."""
    uid = "eeeeeeee-0000-0000-0000-000000000001"
    return {
        "version": 0.4, "links": [], "extra": {},
        "nodes": [{"id": 50, "type": uid,
                   "inputs": [{"name": "audio", "type": "AUDIO", "link": None}],
                   "outputs": [{"name": "AUDIO", "type": "AUDIO", "links": []}],
                   "properties": {}, "widgets_values": []}],
        "definitions": {"subgraphs": [{
            "id": uid, "name": "Tiny Audio", "version": 1,
            "inputNode": {"id": -10}, "outputNode": {"id": -20},
            "widgets": [], "groups": [], "extra": {},
            "inputs": [{"id": "ai1", "name": "audio", "type": "AUDIO",
                        "linkIds": [1]}],
            "outputs": [{"id": "ao1", "name": "AUDIO", "type": "AUDIO",
                         "linkIds": [3]}],
            "nodes": [{"id": 10, "type": "FakeAudioProc",
                       "inputs": [{"name": "audio", "type": "AUDIO", "link": 1}],
                       "outputs": [{"name": "AUDIO", "type": "AUDIO",
                                    "links": [3]}],
                       "properties": {}, "widgets_values": []}],
            "links": [
                {"id": 1, "origin_id": -10, "origin_slot": 0, "target_id": 10,
                 "target_slot": 0, "type": "AUDIO"},
                {"id": 3, "origin_id": 10, "origin_slot": 0, "target_id": -20,
                 "target_slot": 0, "type": "AUDIO"}],
        }]},
    }


class TestAudioBoundary(unittest.TestCase):
    def test_audio_in_and_out_convert(self):
        r = ondemand.preflight(_audio_blueprint(), schemas())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual([i["type"] for i in r["inputs"]], ["AUDIO"])
        self.assertEqual([o["type"] for o in r["outputs"]], ["AUDIO"])

    def test_audio_output_uses_saveaudio(self):
        cw = convert(_audio_blueprint(), schemas())
        save = cw.prompt[cw.outputs[0].save_node_key]
        self.assertEqual(save["class_type"], "SaveAudio")
        self.assertEqual(save["inputs"]["audio"], ["50:10", 0])

    def test_audio_not_instant_testable(self):
        r = ondemand.preflight(_audio_blueprint(), schemas())
        self.assertFalse(r["instant_testable"])


class TestSaveBlueprint(unittest.TestCase):
    def test_save_writes_a_probeable_blueprint(self):
        from comfycloudhybrid.scanner import _probe
        bp = load("mask_blueprint.json")
        with tempfile.TemporaryDirectory() as tmp:
            orig = config.SAVED_DIR
            config.SAVED_DIR = Path(tmp)
            try:
                info = ondemand.save_blueprint(bp, "My Cut (BiRefNet)")
                path = Path(info["path"])
                self.assertTrue(path.exists())
                self.assertEqual(json.loads(path.read_text())["nodes"], bp["nodes"])
                probed = _probe(str(path), "saved")
                self.assertIsNotNone(probed)
                self.assertEqual(probed.source_kind, "saved")
            finally:
                config.SAVED_DIR = orig

    def test_save_rejects_empty_payload(self):
        with self.assertRaises(ValueError):
            ondemand.save_blueprint({}, "x")


if __name__ == "__main__":
    unittest.main()
