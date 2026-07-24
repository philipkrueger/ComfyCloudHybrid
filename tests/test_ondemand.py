import copy
import json
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from schemas import CLOUD_OBJECT_INFO

from comfycloudhybrid import config, ondemand
from comfycloudhybrid.converter import SchemaSource
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
