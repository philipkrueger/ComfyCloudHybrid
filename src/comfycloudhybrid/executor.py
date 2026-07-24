"""Shared runtime pipeline: inject inputs → upload → submit → poll → download.

Runs inside ComfyUI's event loop (async node execution) — aiohttp +
asyncio.sleep only, never blocking I/O. ComfyUI imports (folder_paths,
comfy.*) happen lazily so this module stays importable in offline tests.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io as _io
import json
import logging

from . import cache, config
from .cloud_client import CloudError, ComfyCloudClient
from .converter.model import SENTINEL, VALUE_OUTPUT_TYPES, ConvertedWorkflow

log = logging.getLogger("ComfyCloudHybrid")


# -- tensor <-> png ---------------------------------------------------------

def tensor_to_png_bytes(image) -> bytes:
    """ComfyUI IMAGE tensor [B,H,W,C] float 0..1 → PNG bytes (first frame)."""
    import numpy as np
    from PIL import Image as PILImage

    arr = image
    if hasattr(arr, "cpu"):
        arr = arr.cpu().numpy()
    arr = np.asarray(arr)
    if arr.ndim == 4:
        if arr.shape[0] > 1:
            log.warning("IMAGE batch (%d) an Cloud-Grenze — nur das erste Bild wird übertragen",
                        arr.shape[0])
        arr = arr[0]
    arr = (np.clip(arr, 0.0, 1.0) * 255.0).round().astype("uint8")
    buf = _io.BytesIO()
    PILImage.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def mask_to_png_bytes(mask) -> bytes:
    """ComfyUI MASK tensor [B,H,W]/[H,W] float 0..1 → grayscale PNG bytes."""
    import numpy as np
    from PIL import Image as PILImage

    arr = mask
    if hasattr(arr, "cpu"):
        arr = arr.cpu().numpy()
    arr = np.asarray(arr)
    while arr.ndim > 2:
        arr = arr[0]
    arr = (np.clip(arr, 0.0, 1.0) * 255.0).round().astype("uint8")
    buf = _io.BytesIO()
    PILImage.fromarray(arr, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def png_bytes_to_tensor(data: bytes):
    """PNG/JPEG/WebP bytes → ComfyUI IMAGE tensor [1,H,W,3] float 0..1."""
    import numpy as np
    import torch
    from PIL import Image as PILImage

    img = PILImage.open(_io.BytesIO(data)).convert("RGB")
    arr = np.asarray(img).astype("float32") / 255.0
    return torch.from_numpy(arr)[None,]


def audio_to_flac_bytes(audio) -> bytes:
    """ComfyUI AUDIO dict {"waveform": [B,C,T]|[C,T] float -1..1,
    "sample_rate": int} → FLAC bytes (lossless, 16-bit PCM)."""
    import av
    import numpy as np

    waveform = audio["waveform"]
    if hasattr(waveform, "cpu"):
        waveform = waveform.cpu()
    import torch
    waveform = torch.as_tensor(waveform)
    if waveform.dim() == 3:
        waveform = waveform[0]
    sr = int(audio["sample_rate"])
    n_ch = waveform.shape[0]
    pcm = (waveform.clamp(-1.0, 1.0).numpy() * 32767.0).astype(np.int16)  # [C,T]
    interleaved = np.ascontiguousarray(pcm.T.reshape(1, -1))  # packed s16

    buf = _io.BytesIO()
    with av.open(buf, "w", format="flac") as container:
        layout = "mono" if n_ch == 1 else "stereo"
        stream = container.add_stream("flac", rate=sr, layout=layout)
        frame = av.AudioFrame.from_ndarray(interleaved, format="s16", layout=layout)
        frame.sample_rate = sr
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):  # flush
            container.mux(packet)
    return buf.getvalue()


def flac_bytes_to_audio(data: bytes) -> dict:
    """Audio file bytes → ComfyUI AUDIO dict (mirrors core LoadAudio: PyAV
    decode to f32 PCM). Inverse of audio_to_flac_bytes."""
    import av
    import torch

    with av.open(_io.BytesIO(data)) as af:
        if not af.streams.audio:
            raise CloudError("Cloud audio output contains no audio stream.")
        stream = af.streams.audio[0]
        sr = stream.codec_context.sample_rate
        n_channels = stream.channels
        frames = []
        for frame in af.decode(streams=stream.index):
            fbuf = torch.from_numpy(frame.to_ndarray())
            if fbuf.shape[0] != n_channels:
                fbuf = fbuf.view(-1, n_channels).t()
            frames.append(fbuf)
        if not frames:
            raise CloudError("Cloud audio output decoded to zero frames.")
        wav = torch.cat(frames, dim=1)
    if wav.dtype.is_floating_point:
        wav = wav.float()
    else:
        wav = wav.float() / (2 ** (8 * wav.element_size() - 1))
    return {"waveform": wav[None, ...], "sample_rate": sr}


def png_bytes_to_mask(data: bytes):
    """PNG/JPEG/WebP bytes → ComfyUI MASK tensor [1,H,W] float 0..1.

    Inverse of mask_to_png_bytes — the cloud saves the mask as a grayscale
    image (via MaskToImage + SaveImage), here it becomes a mask tensor again.
    """
    import numpy as np
    import torch
    from PIL import Image as PILImage

    img = PILImage.open(_io.BytesIO(data)).convert("L")
    arr = np.asarray(img).astype("float32") / 255.0
    return torch.from_numpy(arr)[None,]


def _batch(tensors: list):
    import torch

    if len(tensors) == 1:
        return tensors[0]
    shapes = {tuple(t.shape[1:]) for t in tensors}
    if len(shapes) > 1:
        log.warning("Cloud-Outputs haben unterschiedliche Größen %s — nur das erste "
                    "Bild wird zurückgegeben", shapes)
        return tensors[0]
    return torch.cat(tensors, dim=0)


# -- interrupt / progress hooks ---------------------------------------------

def _check_interrupted():
    try:
        import comfy.model_management as mm
        mm.throw_exception_if_processing_interrupted()
    except ImportError:
        pass


def _make_progress_bar():
    try:
        from comfy.utils import ProgressBar
        return ProgressBar(100)
    except Exception:
        return None


def _send_progress_text(text: str, node_id: str | None) -> None:
    """Show a status line on the executing node (supported since ~0.3.4x)."""
    if not node_id:
        return
    try:
        from server import PromptServer
        PromptServer.instance.send_progress_text(text, node_id)
    except Exception:
        pass


class _ProgressReporter:
    """Merges poll status and websocket progress into bar + node text.

    Waiting and rendering are shown distinctly: while no worker is assigned
    the bar stays at 0 and the node shows a queue message; real percent only
    appears once the cloud reports actual sampling progress."""

    def __init__(self, node_id: str | None):
        self._node_id = node_id
        self._pbar = _make_progress_bar()
        self._real_progress = False
        self._current_node = ""

    def on_tick(self, status: str, phase: str, elapsed: float) -> None:
        if phase == "waiting":
            if status == "preparing":
                msg = (f"⏳ Cloud worker is preparing (loading models) … "
                       f"({int(elapsed)}s)")
            else:
                msg = (f"⏳ Waiting for cloud worker … "
                       f"({int(elapsed)}s, status: {status})")
            _send_progress_text(msg, self._node_id)
            if self._pbar is not None:
                self._pbar.update_absolute(0)
        elif not self._real_progress:
            suffix = f" — {self._current_node}" if self._current_node else ""
            _send_progress_text(
                f"☁ Cloud rendering … ({int(elapsed)}s{suffix})", self._node_id)

    def on_ws(self, kind: str, data: dict) -> None:
        if kind == "progress":
            self._real_progress = True
            value, maximum = data["value"], data["max"]
            pct = int(value / maximum * 100)
            if self._pbar is not None:
                self._pbar.update_absolute(min(pct, 99))
            suffix = f" — {self._current_node}" if self._current_node else ""
            _send_progress_text(f"☁ Cloud rendering: {pct}% ({value}/{maximum}{suffix})",
                                self._node_id)
        elif kind == "node":
            self._current_node = data.get("display", "")

    def done(self, gpu_seconds: float | None = None) -> None:
        if self._pbar is not None:
            self._pbar.update_absolute(100)
        cost = f" — GPU time: {gpu_seconds:.1f}s (billed compute)" if gpu_seconds else ""
        _send_progress_text(f"✓ Cloud job done{cost} — downloading results …",
                            self._node_id)


# -- upload with content-hash dedupe ----------------------------------------

async def upload_cached(client: ComfyCloudClient, data: bytes, filename: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    known = cache.get_upload(digest)
    if known:
        return known
    name = await client.upload_image(data, filename)
    cache.set_upload(digest, name)
    return name


# -- main pipeline ------------------------------------------------------------

async def run(converted: ConvertedWorkflow, bound_values: dict,
              timeout_s: float | None = None, node_id: str | None = None) -> tuple:
    """Execute a converted blueprint on Comfy Cloud; returns output tensors
    ordered like converted.outputs."""
    if converted.missing_classes:
        raise CloudError(
            f"Blueprint '{converted.name}' cannot run on Comfy Cloud — "
            f"missing nodes: {', '.join(converted.missing_classes)}")
    api_key = config.get_api_key()
    if not api_key:
        raise CloudError("No Comfy Cloud API key configured. "
                         "Settings → Comfy Cloud Hybrid (key from platform.comfy.org).")
    timeout = float(timeout_s if timeout_s is not None else config.get("job_timeout_s"))
    queue_timeout = float(config.get("queue_timeout_s"))
    poll = float(config.get("poll_interval_s"))

    async with ComfyCloudClient(api_key) as client:
        prompt = copy.deepcopy(converted.prompt)
        _send_progress_text("⇡ Uploading inputs to the cloud …", node_id)
        await _inject_inputs(client, prompt, converted, bound_values)
        await _upload_fixed_files(client, prompt, converted)

        # Partner/API nodes inside the workflow bill against the same account.
        job_id = await client.submit(prompt, extra_data={"api_key_comfy_org": api_key})
        log.info("cloud job submitted: %s (%s)", job_id, converted.name)

        reporter = _ProgressReporter(node_id)
        ws_task = asyncio.create_task(
            client.listen_progress(job_id, reporter.on_ws))
        try:
            detail = await client.wait_for_job(
                job_id, poll_interval=poll, timeout=timeout,
                queue_timeout=queue_timeout,
                on_tick=reporter.on_tick, check_interrupted=_check_interrupted)
        except CloudError:
            raise
        except Exception:
            # local interrupt or cancellation — stop paying for the cloud job
            await client.interrupt(job_id)
            raise
        finally:
            ws_task.cancel()

        reporter.done(ComfyCloudClient.gpu_seconds(detail))
        return await _collect_outputs(client, converted, detail)


async def _inject_inputs(client, prompt: dict, converted: ConvertedWorkflow,
                         bound_values: dict) -> None:
    for bi in converted.inputs:
        provided = bi.safe_id in bound_values and bound_values[bi.safe_id] is not None
        if bi.type in ("IMAGE", "MASK", "AUDIO"):
            if not provided:
                _drop_sentinels(prompt, bi.safe_id)
                continue
            value = bound_values[bi.safe_id]
            if bi.type == "IMAGE":
                data = tensor_to_png_bytes(value)
                name = await upload_cached(client, data, f"cch_{bi.safe_id}.png")
                loader_key, src = _ensure_loader(prompt, bi.safe_id, name)
            elif bi.type == "AUDIO":
                data = audio_to_flac_bytes(value)
                name = await upload_cached(client, data, f"cch_{bi.safe_id}.flac")
                loader_key, src = _ensure_audio_loader(prompt, bi.safe_id, name)
            else:
                data = mask_to_png_bytes(value)
                name = await upload_cached(client, data, f"cch_{bi.safe_id}.png")
                loader_key, src = _ensure_mask_loader(prompt, bi.safe_id, name)
            for nk, inp in bi.targets:
                prompt[nk]["inputs"][inp] = src
        elif bi.type == "UPLOAD_COMBO":
            filename = bound_values.get(bi.safe_id, bi.default)
            if not filename:
                continue
            name = await _upload_local_input_file(client, filename)
            for nk, inp in bi.targets:
                prompt[nk]["inputs"][inp] = name
        else:
            value = bound_values.get(bi.safe_id)
            if value is None:
                value = bi.default
            if value is None:
                # no value at all — restore the class default instead of
                # shipping None into the cloud prompt
                _drop_sentinels(prompt, bi.safe_id)
                continue
            value = _clamp_numeric(value, bi)
            for nk, inp in bi.targets:
                prompt[nk]["inputs"][inp] = value
    _assert_no_sentinels(prompt)


def _clamp_numeric(value, bi):
    """Safety net: clamp INT/FLOAT into the cloud-declared range even if a
    stale workflow or a frontend quirk sent an out-of-range value (e.g. a
    negative seed from an earlier build)."""
    if bi.type not in ("INT", "FLOAT") or not isinstance(value, (int, float)):
        return value
    original = value
    if bi.minimum is not None and value < bi.minimum:
        value = int(bi.minimum) if bi.type == "INT" else bi.minimum
    if bi.maximum is not None and value > bi.maximum:
        value = int(bi.maximum) if bi.type == "INT" else bi.maximum
    if value != original:
        log.warning("value for '%s' (%s) outside cloud range [%s, %s] — clamped to %s",
                    bi.name, original, bi.minimum, bi.maximum, value)
    return value


def _ensure_loader(prompt: dict, safe_id: str, cloud_name: str):
    key = f"cch_load_{safe_id}"
    prompt[key] = {"class_type": "LoadImage", "inputs": {"image": cloud_name},
                   "_meta": {"title": f"CloudHybrid Input {safe_id}"}}
    return key, [key, 0]


def _ensure_audio_loader(prompt: dict, safe_id: str, cloud_name: str):
    key = f"cch_load_{safe_id}"
    prompt[key] = {"class_type": "LoadAudio", "inputs": {"audio": cloud_name},
                   "_meta": {"title": f"CloudHybrid Audio Input {safe_id}"}}
    return key, [key, 0]


def _ensure_mask_loader(prompt: dict, safe_id: str, cloud_name: str):
    """Grayscale PNG → MASK via LoadImage + ImageToMask (red channel)."""
    load_key = f"cch_load_{safe_id}"
    conv_key = f"cch_mask_{safe_id}"
    prompt[load_key] = {"class_type": "LoadImage", "inputs": {"image": cloud_name},
                        "_meta": {"title": f"CloudHybrid Mask Input {safe_id}"}}
    prompt[conv_key] = {"class_type": "ImageToMask",
                        "inputs": {"image": [load_key, 0], "channel": "red"},
                        "_meta": {"title": f"CloudHybrid Mask Convert {safe_id}"}}
    return conv_key, [conv_key, 0]


def _drop_sentinels(prompt: dict, safe_id: str) -> None:
    for entry in prompt.values():
        for iname, val in list(entry.get("inputs", {}).items()):
            if isinstance(val, list) and len(val) == 2 and val[0] == SENTINEL and val[1] == safe_id:
                del entry["inputs"][iname]


def _assert_no_sentinels(prompt: dict) -> None:
    for key, entry in prompt.items():
        for iname, val in entry.get("inputs", {}).items():
            if isinstance(val, list) and val and val[0] == SENTINEL:
                raise CloudError(
                    f"Internal error: input '{val[1]}' was not substituted "
                    f"(node {key}.{iname}).")


async def _upload_local_input_file(client, filename: str) -> str:
    try:
        import folder_paths
        path = folder_paths.get_annotated_filepath(filename)
    except ImportError:
        path = filename
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise CloudError(f"Local input file '{filename}' not found ({e}). "
                         "Is it in the ComfyUI input folder?")
    return await upload_cached(client, data, filename)


async def _upload_fixed_files(client, prompt: dict, converted: ConvertedWorkflow) -> None:
    for key, input_name, filename in converted.required_uploads:
        entry = prompt.get(key)
        if entry is None:
            continue
        current = entry["inputs"].get(input_name)
        if current != filename:
            continue  # already replaced (e.g. via proxy input)
        entry["inputs"][input_name] = await _upload_local_input_file(client, filename)


def _output_files(node_out: dict) -> list:
    """Collect output file dicts regardless of media key (images/video/gifs)."""
    files = []
    for key in ("images", "video", "videos", "gifs", "audio"):
        files.extend(node_out.get(key) or [])
    return files


def _parse_value_output(typ: str, text: str):
    """Inverse of the cloud-side PreviewAny text channel (see flatten.py):
    strings verbatim, numbers via int()/float(), booleans tolerantly,
    BOUNDING_BOX via json (PreviewAny json.dumps's structured data)."""
    try:
        if typ == "INT":
            return int(float(text.strip()))
        if typ == "FLOAT":
            return float(text.strip())
        if typ == "BOOLEAN":
            return text.strip().lower() in ("true", "1", "yes")
        if typ == "BOUNDING_BOX":
            return json.loads(text)
    except (ValueError, json.JSONDecodeError) as e:
        raise CloudError(
            f"Cloud output could not be parsed as {typ}: {text[:120]!r} ({e})")
    return text


async def _collect_outputs(client, converted: ConvertedWorkflow, detail: dict) -> tuple:
    outputs = detail.get("outputs") or {}
    result = []
    for bo in converted.outputs:
        node_out = outputs.get(bo.save_node_key) or {}
        if bo.type in VALUE_OUTPUT_TYPES:
            texts = node_out.get("text")
            if not texts:
                raise CloudError(
                    f"Cloud job finished but text output '{bo.name}' "
                    f"({bo.save_node_key}) is missing.",
                    detail=json.dumps(list(outputs.keys()))[:300])
            result.append(_parse_value_output(
                bo.type, "".join(str(t) for t in texts)))
            continue
        files = _output_files(node_out)
        if not files:
            raise CloudError(
                f"Cloud job finished but output '{bo.name}' ({bo.save_node_key}) is missing.",
                detail=json.dumps(list(outputs.keys()))[:300])
        if bo.type == "VIDEO":
            im = files[0]
            data = await client.download_output(
                filename=im.get("filename", ""), subfolder=im.get("subfolder", ""),
                type=im.get("type", "output"))
            result.append(_bytes_to_video(data))
        elif bo.type == "AUDIO":
            im = files[0]
            data = await client.download_output(
                filename=im.get("filename", ""), subfolder=im.get("subfolder", ""),
                type=im.get("type", "output"))
            result.append(flac_bytes_to_audio(data))
        else:
            to_tensor = png_bytes_to_mask if bo.type == "MASK" else png_bytes_to_tensor
            tensors = []
            for im in files:
                data = await client.download_output(
                    filename=im.get("filename", ""),
                    subfolder=im.get("subfolder", ""),
                    type=im.get("type", "output"))
                tensors.append(to_tensor(data))
            result.append(_batch(tensors))
    return tuple(result)


def _bytes_to_video(data: bytes):
    """Wrap downloaded video bytes as a ComfyUI VIDEO (in-memory, no disk)."""
    import io as _stdio

    from comfy_api.latest import InputImpl
    return InputImpl.VideoFromFile(_stdio.BytesIO(data))


async def run_raw_prompt(prompt: dict, image_tokens: dict[str, "object"],
                         timeout_s: float | None = None,
                         node_id: str | None = None):
    """Generic runner: execute an arbitrary API-format prompt. image_tokens
    maps token strings (e.g. %CCH_IMAGE_1%) to IMAGE tensors; every prompt
    input whose value equals a token is replaced by an uploaded LoadImage.
    Returns a single IMAGE batch of all image outputs."""
    api_key = config.get_api_key()
    if not api_key:
        raise CloudError("No Comfy Cloud API key configured. "
                         "Settings → Comfy Cloud Hybrid (key from platform.comfy.org).")
    timeout = float(timeout_s if timeout_s is not None else config.get("job_timeout_s"))
    poll = float(config.get("poll_interval_s"))

    async with ComfyCloudClient(api_key) as client:
        prompt = copy.deepcopy(prompt)
        for token, tensor in image_tokens.items():
            if tensor is None:
                continue
            name = await upload_cached(client, tensor_to_png_bytes(tensor),
                                       f"cch_generic_{hashlib.sha256(token.encode()).hexdigest()[:8]}.png")
            loader_key = f"cch_load_{token.strip('%').lower()}"
            used = False
            for entry in prompt.values():
                for iname, val in list(entry.get("inputs", {}).items()):
                    if val == token:
                        entry["inputs"][iname] = [loader_key, 0]
                        used = True
                    elif isinstance(val, str) and val == token.strip("%"):
                        entry["inputs"][iname] = [loader_key, 0]
                        used = True
            if used:
                prompt[loader_key] = {"class_type": "LoadImage",
                                      "inputs": {"image": name},
                                      "_meta": {"title": f"CloudHybrid {token}"}}

        job_id = await client.submit(prompt, extra_data={"api_key_comfy_org": api_key})
        log.info("cloud job submitted: %s (generic)", job_id)

        reporter = _ProgressReporter(node_id)
        ws_task = asyncio.create_task(
            client.listen_progress(job_id, reporter.on_ws))
        try:
            detail = await client.wait_for_job(
                job_id, poll_interval=poll, timeout=timeout,
                queue_timeout=float(config.get("queue_timeout_s")),
                on_tick=reporter.on_tick, check_interrupted=_check_interrupted)
        except CloudError:
            raise
        except Exception:
            await client.interrupt(job_id)
            raise
        finally:
            ws_task.cancel()
        reporter.done(ComfyCloudClient.gpu_seconds(detail))

        tensors = []
        for node_out in (detail.get("outputs") or {}).values():
            for im in node_out.get("images") or []:
                if im.get("type") != "output":
                    continue
                data = await client.download_output(
                    filename=im.get("filename", ""),
                    subfolder=im.get("subfolder", ""), type="output")
                tensors.append(png_bytes_to_tensor(data))
        if not tensors:
            raise CloudError("Cloud job finished but no image outputs found. "
                             "Does the workflow contain a SaveImage node?")
        return _batch(tensors)
