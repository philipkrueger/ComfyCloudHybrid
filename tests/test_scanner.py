import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401

from comfycloudhybrid import config, scanner


class TestScanner(unittest.TestCase):
    def test_curated_scan_and_blocked_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixtures = Path(__file__).parent / "fixtures"
            curated = tmp / "curated"
            (curated / "cloud_only").mkdir(parents=True)
            (curated / "blocked").mkdir(parents=True)
            src = (fixtures / "nested_subgraph.json").read_text()
            (curated / "cloud_only" / "a.json").write_text(src)
            (curated / "blocked" / "b.json").write_text(src)
            (curated / "not_a_blueprint.json").write_text('{"nodes": []}')

            saved_curated = config.CURATED_DIR
            saved_config = config.CONFIG_PATH
            config.CURATED_DIR = curated
            config.CONFIG_PATH = tmp / "config.json"
            try:
                found = scanner.scan()
            finally:
                config.CURATED_DIR = saved_curated
                config.CONFIG_PATH = saved_config

            self.assertEqual(len(found), 1)
            bp = found[0]
            self.assertEqual(bp.source_kind, "curated")
            self.assertEqual(bp.subcategory, "cloud_only")
            self.assertEqual(bp.name, "Nested Demo")
            self.assertTrue(bp.slug.startswith("Nested_Demo_"))
            # official taxonomy from the definition's category field
            self.assertEqual(bp.category, "Image Tools/Test")

    def test_category_group_follows_blueprint_naming(self):
        cases = {
            "Text to Image (Flux.2 Dev)": "Text to Image",
            "Image Edit (Qwen 2509)": "Image Edit",
            "Image Captioning(Gemini)": "Image Captioning",  # no space variant
            "Image to Video (LTX-2.3)": "Image to Video",
            "Video Upscale(GAN x4)": "Video Upscale",
            "Brightness and Contrast": "",   # no variant suffix → root
            "Sharpen": "",
        }
        for name, expected in cases.items():
            self.assertEqual(scanner.category_group(name), expected, name)

    def test_slug_stable_across_renames(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixtures = Path(__file__).parent / "fixtures"
            src = (fixtures / "nested_subgraph.json").read_text()
            (tmp / "one.json").write_text(src)
            (tmp / "two.json").write_text(src)
            a = scanner._probe(str(tmp / "one.json"), "user")
            b = scanner._probe(str(tmp / "two.json"), "user")
            # same definition UUID → same slug regardless of filename
            self.assertEqual(a.slug, b.slug)


if __name__ == "__main__":
    unittest.main()
