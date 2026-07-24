"""End-to-end pipeline against a mocked Comfy Cloud (aiohttp test server).

Covers: upload → submit → poll → job detail → 302-redirect download,
plus 401/402 error mapping. No real network, no credits."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401

import aiohttp
from aiohttp import web

from comfycloudhybrid import cache, config
from comfycloudhybrid.cloud_client import CloudError, ComfyCloudClient
from comfycloudhybrid.converter.model import (
    SENTINEL, BoundInput, BoundOutput, ConvertedWorkflow)
from comfycloudhybrid import executor


def make_png_bytes(w=4, h=4, color=(255, 0, 0)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class MockCloud:
    def __init__(self):
        self.uploads = []
        self.submitted = []
        self.polls = 0
        self.fail_mode = None  # None | 401 | 402
        self.app = web.Application()
        self.app.router.add_post("/api/upload/image", self.upload)
        self.app.router.add_post("/api/prompt", self.prompt)
        self.app.router.add_get("/api/job/{jid}/status", self.status)
        self.app.router.add_get("/api/jobs/{jid}", self.detail)
        self.app.router.add_get("/api/view", self.view)
        self.app.router.add_get("/api/user", self.user)
        self.app.router.add_get("/signed/{name}", self.signed)
        self.app.router.add_post("/api/interrupt", self.interrupt)
        self.app.router.add_post("/api/queue", self.queue)
        self.interrupted = False
        self.base = None

    async def upload(self, request):
        if request.headers.get("X-API-Key") != "test-key":
            return web.json_response({}, status=401)
        reader = await request.multipart()
        name = None
        async for part in reader:
            if part.name == "image":
                data = await part.read()
                name = f"hashed_{len(self.uploads)}.png"
                self.uploads.append((part.filename, len(data)))
        return web.json_response({"name": name, "subfolder": "", "type": "input"})

    async def prompt(self, request):
        if self.fail_mode:
            return web.json_response({"error": {"message": "nope"}},
                                     status=self.fail_mode)
        body = await request.json()
        self.submitted.append(body)
        return web.json_response({"prompt_id": "job-1", "number": 1, "node_errors": {}})

    async def status(self, request):
        self.polls += 1
        if getattr(self, "stuck_pending", False):
            # "preparing" = cold worker loading models (undocumented enum,
            # verified live) — must count as WAITING, queue timeout applies
            return web.json_response({"status": "preparing"})
        if getattr(self, "stuck_running", False):
            status = "pending" if self.polls == 1 else "in_progress"
            return web.json_response({"status": status})
        # real cloud sequence: pending → preparing → in_progress, and the
        # status endpoint reports "success" for finished jobs (the jobs list
        # says "completed" — both enums exist, verified live)
        status = ("pending", "preparing", "in_progress").__getitem__(
            self.polls - 1) if self.polls <= 3 else "success"
        return web.json_response({"status": status})

    async def detail(self, request):
        outputs = getattr(self, "outputs_override", None) or {
            "cch_save_0": {"images": [
                {"filename": "out.png", "subfolder": "", "type": "output"}]}}
        return web.json_response({
            "id": "job-1", "status": "completed", "outputs": outputs,
            "execution_status": {"messages": [
                ["execution_start", {"timestamp": 1000}],
                ["execution_success", {"timestamp": 31400}]]},
        })

    async def view(self, request):
        # cloud responds with a redirect to signed storage
        raise web.HTTPFound(f"{self.base}/signed/{request.query['filename']}")

    async def signed(self, request):
        assert "X-API-Key" not in request.headers, "API key leaked to storage host!"
        return web.Response(body=make_png_bytes(), content_type="image/png")

    async def user(self, request):
        if request.headers.get("X-API-Key") != "test-key":
            return web.json_response({}, status=401)
        return web.json_response({"status": "active"})

    async def interrupt(self, request):
        self.interrupted = True
        return web.json_response({})

    async def queue(self, request):
        return web.json_response({"deleted": ["job-1"], "cleared": False})


class GpuSecondsTest(unittest.TestCase):
    def test_gpu_seconds_from_messages(self):
        detail = {"execution_status": {"messages": [
            ["execution_start", {"timestamp": 1000}],
            ["execution_cached", {"timestamp": 1200}],
            ["execution_success", {"timestamp": 31400}]]}}
        self.assertAlmostEqual(ComfyCloudClient.gpu_seconds(detail), 30.4)

    def test_gpu_seconds_missing(self):
        self.assertIsNone(ComfyCloudClient.gpu_seconds({}))


class PollResilienceTest(unittest.IsolatedAsyncioTestCase):
    """A single flaky poll must never crash the job — verified live: a stale
    pooled connection hung until the per-request timeout fired and a raw
    TimeoutError killed a node after 15 minutes of otherwise-healthy polling."""

    async def test_transient_network_error_retried_not_crashed(self):
        client = ComfyCloudClient("test-key", base_url="http://example.invalid")
        calls = {"status": 0}

        async def flaky_status(job_id):
            calls["status"] += 1
            if calls["status"] <= 2:
                raise asyncio.TimeoutError("simulated stale connection")
            return "completed"

        async def fake_detail(job_id):
            return {"id": job_id, "status": "completed", "outputs": {}}

        closed = {"n": 0}
        orig_close = client.close

        async def spy_close():
            closed["n"] += 1
            await orig_close()

        client.job_status = flaky_status
        client.job_detail = fake_detail
        client.close = spy_close

        with self.assertLogs("ComfyCloudHybrid", level="WARNING"):
            detail = await client.wait_for_job(
                "job-x", poll_interval=0.01, timeout=30, queue_timeout=30)
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(calls["status"], 3)
        self.assertEqual(closed["n"], 2)  # one forced reconnect per failure

    async def test_persistent_network_error_escalates_after_grace(self):
        client = ComfyCloudClient("test-key", base_url="http://example.invalid")
        client._NETWORK_GRACE_S = 0.05  # keep the test fast

        async def always_fails(job_id):
            raise aiohttp.ClientConnectionError("simulated network down")

        interrupted = {"n": 0}

        async def fake_interrupt(job_id=None):
            interrupted["n"] += 1

        client.job_status = always_fails
        client.interrupt = fake_interrupt

        with self.assertRaises(CloudError) as ctx:
            await client.wait_for_job(
                "job-y", poll_interval=0.01, timeout=30, queue_timeout=30)
        self.assertIn("Lost connection", ctx.exception.user_message)
        # the job itself is left alone — we truly don't know its state, and
        # cancelling it would waste a job that might have already succeeded
        self.assertEqual(interrupted["n"], 0)


class ExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock = MockCloud()
        self.runner = web.AppRunner(self.mock.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.mock.base = f"http://127.0.0.1:{port}"

        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        # redirect all pack state into the sandbox
        self._patches = [
            (config, "CONFIG_PATH", tmp_path / "config.json"),
            (config, "CACHE_DIR", tmp_path / "cache"),
            (cache, "CONVERTED_DIR", tmp_path / "cache" / "converted"),
            (cache, "OBJECT_INFO_PATH", tmp_path / "cache" / "oi.json"),
            (cache, "UPLOADS_PATH", tmp_path / "cache" / "uploads.json"),
        ]
        self._saved = [(m, n, getattr(m, n)) for m, n, _ in self._patches]
        for m, n, v in self._patches:
            setattr(m, n, v)
        import os
        os.environ["COMFY_CLOUD_API_KEY"] = "test-key"
        os.environ["COMFY_CLOUD_BASE_URL"] = self.mock.base

    async def asyncTearDown(self):
        import os
        for m, n, v in self._saved:
            setattr(m, n, v)
        os.environ.pop("COMFY_CLOUD_API_KEY", None)
        os.environ.pop("COMFY_CLOUD_BASE_URL", None)
        await self.runner.cleanup()
        self.tmp.cleanup()

    def _converted(self):
        return ConvertedWorkflow(
            name="Test",
            prompt={
                "35:32": {"class_type": "ImageBatch",
                          "inputs": {"image1": ["35:34", 0],
                                     "image2": [SENTINEL, "image2"]}},
                "35:34": {"class_type": "LoadImage",
                          "inputs": {"image": "local_ref.png"}},
                "cch_save_0": {"class_type": "SaveImage",
                               "inputs": {"images": ["35:32", 0],
                                          "filename_prefix": "x"}},
            },
            inputs=[BoundInput(name="image2", safe_id="image2", type="IMAGE",
                               kind="slot", targets=[("35:32", "image2")])],
            outputs=[BoundOutput(name="IMAGE", type="IMAGE",
                                 save_node_key="cch_save_0")],
            required_uploads=[("35:34", "image", "local_ref.png")],
        )

    async def test_full_pipeline(self):
        import torch
        # fake local input file for the fixed-file upload
        local = Path(self.tmp.name) / "local_ref.png"
        local.write_bytes(make_png_bytes(color=(0, 255, 0)))
        cw = self._converted()
        cw.prompt["35:34"]["inputs"]["image"] = str(local)
        cw.required_uploads = [("35:34", "image", str(local))]

        image = torch.zeros((1, 4, 4, 3), dtype=torch.float32)
        result = await executor.run(cw, {"image2": image}, timeout_s=30)

        self.assertEqual(len(result), 1)
        self.assertEqual(tuple(result[0].shape), (1, 4, 4, 3))
        self.assertEqual(len(self.mock.uploads), 2)  # tensor + fixed file
        submitted = self.mock.submitted[0]["prompt"]
        # sentinel replaced by injected loader
        self.assertEqual(submitted["35:32"]["inputs"]["image2"],
                         ["cch_load_image2", 0])
        self.assertEqual(submitted["cch_load_image2"]["class_type"], "LoadImage")
        # fixed file replaced by cloud name
        self.assertTrue(submitted["35:34"]["inputs"]["image"].startswith("hashed_"))
        self.assertGreaterEqual(self.mock.polls, 3)

    async def test_upload_dedupe(self):
        import torch
        image = torch.zeros((1, 4, 4, 3), dtype=torch.float32)
        cw = self._converted()
        cw.required_uploads = []
        cw.prompt["35:34"]["inputs"]["image"] = "already-cloud.png"
        await executor.run(cw, {"image2": image}, timeout_s=30)
        self.mock.polls = 0
        await executor.run(cw, {"image2": image}, timeout_s=30)
        self.assertEqual(len(self.mock.uploads), 1)  # second run hits the cache

    async def test_error_messages(self):
        async with ComfyCloudClient("wrong-key", base_url=self.mock.base) as client:
            with self.assertRaises(CloudError) as ctx:
                await client.user_status()
        self.assertIn("Invalid", ctx.exception.user_message)

        self.mock.fail_mode = 402
        async with ComfyCloudClient("test-key", base_url=self.mock.base) as client:
            with self.assertRaises(CloudError) as ctx:
                await client.submit({"1": {"class_type": "X", "inputs": {}}})
        self.assertIn("credits", ctx.exception.user_message)

    async def test_missing_classes_refuses_to_run(self):
        cw = self._converted()
        cw.missing_classes = ["SomeCustomNode"]
        with self.assertRaises(CloudError) as ctx:
            await executor.run(cw, {}, timeout_s=5)
        self.assertIn("SomeCustomNode", ctx.exception.user_message)

    async def test_queue_timeout_when_no_worker(self):
        self.mock.stuck_pending = True
        async with ComfyCloudClient("test-key", base_url=self.mock.base) as client:
            with self.assertRaises(CloudError) as ctx:
                await client.wait_for_job("job-1", poll_interval=0.05,
                                          timeout=60, queue_timeout=0.3)
        self.assertIn("No cloud worker", ctx.exception.user_message)
        self.assertTrue(self.mock.interrupted)

    async def test_running_timeout_counts_from_dispatch(self):
        # 1 poll pending, then in_progress forever: the tiny queue_timeout must
        # NOT fire once running; the run-phase timeout must be the one that hits
        self.mock.stuck_running = True
        async with ComfyCloudClient("test-key", base_url=self.mock.base) as client:
            with self.assertRaises(CloudError) as ctx:
                await client.wait_for_job("job-1", poll_interval=0.05,
                                          timeout=0.3, queue_timeout=600)
        self.assertIn("render time", ctx.exception.user_message)

    async def test_unconnected_optional_image_dropped(self):
        import torch
        cw = self._converted()
        cw.required_uploads = []
        cw.prompt["35:34"]["inputs"]["image"] = "x.png"
        # optional second image (Qwen image2/image3 pattern)
        cw.prompt["35:32"]["inputs"]["image_b"] = [SENTINEL, "image_b"]
        cw.inputs.append(BoundInput(
            name="image_b", safe_id="image_b", type="IMAGE", kind="slot",
            targets=[("35:32", "image_b")], optional=True))
        img = torch.zeros((1, 4, 4, 3), dtype=torch.float32)
        # image_b not provided → sentinel must be removed, prompt still valid
        await executor.run(cw, {"image2": img, "image_b": None}, timeout_s=30)
        sent = self.mock.submitted[0]["prompt"]
        self.assertNotIn("image_b", sent["35:32"]["inputs"])

    async def test_negative_seed_clamped(self):
        import torch
        cw = self._converted()
        cw.required_uploads = []
        cw.prompt["35:34"]["inputs"]["image"] = "x.png"
        # a NoiseNode fed by a seed slot with cloud range 0..2^64-1
        cw.prompt["35:seed"] = {"class_type": "RandomNoise",
                                "inputs": {"noise_seed": [SENTINEL, "noise_seed"]}}
        cw.inputs.append(BoundInput(
            name="noise_seed", safe_id="noise_seed", type="INT", kind="slot",
            targets=[("35:seed", "noise_seed")],
            minimum=0, maximum=18446744073709551615))
        img = torch.zeros((1, 4, 4, 3), dtype=torch.float32)
        with self.assertLogs("ComfyCloudHybrid", level="WARNING"):
            await executor.run(cw, {"image2": img, "noise_seed": -385447860950653},
                               timeout_s=30)
        sent = self.mock.submitted[0]["prompt"]
        self.assertEqual(sent["35:seed"]["inputs"]["noise_seed"], 0)

    async def test_video_output_downloads_and_wraps(self):
        import torch
        cw = self._converted()
        cw.required_uploads = []
        cw.prompt["35:34"]["inputs"]["image"] = "x.png"
        cw.outputs[0].type = "VIDEO"
        cw.prompt["cch_save_0"] = {"class_type": "SaveVideo",
                                   "inputs": {"video": ["35:32", 0]}}
        self.mock.outputs_override = {"cch_save_0": {"video": [
            {"filename": "out.mp4", "subfolder": "", "type": "output"}]}}

        captured = {}
        orig = executor._bytes_to_video
        def _fake_video(data):
            captured["bytes"] = data
            return "VIDEO_OBJ"
        executor._bytes_to_video = _fake_video
        try:
            img = torch.zeros((1, 4, 4, 3), dtype=torch.float32)
            result = await executor.run(cw, {"image2": img}, timeout_s=30)
        finally:
            executor._bytes_to_video = orig
        self.assertEqual(result[0], "VIDEO_OBJ")
        self.assertIn("bytes", captured)  # downloaded video bytes were wrapped

    async def test_no_api_key(self):
        import os
        os.environ.pop("COMFY_CLOUD_API_KEY", None)
        with self.assertRaises(CloudError) as ctx:
            await executor.run(self._converted(), {}, timeout_s=5)
        self.assertIn("API key", ctx.exception.user_message)


class ValueOutputParseTest(unittest.TestCase):
    """PreviewAny text channel → typed Python values."""

    def test_parse_types(self):
        p = executor._parse_value_output
        self.assertEqual(p("STRING", "a caption"), "a caption")
        self.assertEqual(p("INT", "5"), 5)
        self.assertEqual(p("FLOAT", "23.976"), 23.976)
        self.assertTrue(p("BOOLEAN", "True"))
        self.assertFalse(p("BOOLEAN", "False"))
        self.assertEqual(
            p("BOUNDING_BOX", '[{"x": 1, "y": 2, "width": 3, "height": 4}]'),
            [{"x": 1, "y": 2, "width": 3, "height": 4}])

    def test_parse_error_is_clear(self):
        with self.assertRaises(CloudError):
            executor._parse_value_output("INT", "not a number")
        with self.assertRaises(CloudError):
            executor._parse_value_output("BOUNDING_BOX", "<tensor blob>")


class AudioRoundTripTest(unittest.TestCase):
    """audio_to_flac_bytes ∘ flac_bytes_to_audio must preserve the waveform
    (FLAC is lossless; only 16-bit quantization applies)."""

    def test_round_trip(self):
        import torch
        t = torch.linspace(0, 1, 4410)
        wave = torch.stack([torch.sin(2 * 3.14159 * 440 * t),
                            torch.sin(2 * 3.14159 * 220 * t)]) * 0.5  # [2,T]
        audio = {"waveform": wave[None, ...], "sample_rate": 44100}
        flac = executor.audio_to_flac_bytes(audio)
        self.assertGreater(len(flac), 100)
        back = executor.flac_bytes_to_audio(flac)
        self.assertEqual(back["sample_rate"], 44100)
        self.assertEqual(tuple(back["waveform"].shape), (1, 2, 4410))
        diff = (back["waveform"][0] - wave).abs().max().item()
        self.assertLess(diff, 1e-3, f"quantization drift too large: {diff}")

    def test_mono_and_2d_input(self):
        import torch
        audio = {"waveform": torch.zeros(1, 2205), "sample_rate": 22050}  # [C,T]
        back = executor.flac_bytes_to_audio(executor.audio_to_flac_bytes(audio))
        self.assertEqual(tuple(back["waveform"].shape), (1, 1, 2205))


class CollectAudioOutputTest(unittest.IsolatedAsyncioTestCase):
    async def test_audio_output_downloaded_and_decoded(self):
        import torch
        from comfycloudhybrid.converter.model import BoundOutput, ConvertedWorkflow
        flac = executor.audio_to_flac_bytes(
            {"waveform": torch.zeros(1, 1, 1000), "sample_rate": 16000})

        class StubClient:
            async def download_output(self, filename, subfolder, type):
                return flac

        cw = ConvertedWorkflow(name="t", prompt={}, inputs=[],
                               outputs=[BoundOutput("AUDIO", "AUDIO", "cch_save_0")],
                               required_uploads=[])
        detail = {"outputs": {"cch_save_0": {"audio": [
            {"filename": "a.flac", "subfolder": "", "type": "output"}]}}}
        (result,) = await executor._collect_outputs(StubClient(), cw, detail)
        self.assertEqual(result["sample_rate"], 16000)
        self.assertEqual(tuple(result["waveform"].shape), (1, 1, 1000))


class CollectTextOutputsTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_outputs_collected_without_downloads(self):
        from comfycloudhybrid.converter.model import BoundOutput, ConvertedWorkflow
        cw = ConvertedWorkflow(
            name="t", prompt={}, inputs=[],
            outputs=[BoundOutput("RESPONSE", "STRING", "cch_save_0"),
                     BoundOutput("count", "INT", "cch_save_1")],
            required_uploads=[])
        detail = {"outputs": {"cch_save_0": {"text": ["a caption"]},
                              "cch_save_1": {"text": ["3"]}}}
        # client=None: the text channel must not attempt any download
        result = await executor._collect_outputs(None, cw, detail)
        self.assertEqual(result, ("a caption", 3))

    async def test_missing_text_output_raises(self):
        from comfycloudhybrid.converter.model import BoundOutput, ConvertedWorkflow
        cw = ConvertedWorkflow(name="t", prompt={}, inputs=[],
                               outputs=[BoundOutput("RESPONSE", "STRING", "cch_save_0")],
                               required_uploads=[])
        with self.assertRaises(CloudError):
            await executor._collect_outputs(None, cw, {"outputs": {}})


if __name__ == "__main__":
    unittest.main()
