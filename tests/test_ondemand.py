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
