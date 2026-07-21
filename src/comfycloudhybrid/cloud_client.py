"""Async client for the official Comfy Cloud API (cloud.comfy.org).

Pure aiohttp — importable and testable without ComfyUI. All methods raise
CloudError with a user-facing (English) message on failure.

API notes (docs.comfy.org/development/cloud):
- Auth: X-API-Key header, key from platform.comfy.org (paid tiers only).
- prompt_id returned by POST /api/prompt equals the job_id.
- GET /api/view responds 302 to a signed GCS URL; the redirect must be
  followed WITHOUT the API key header (never leak it to the storage host).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiohttp

log = logging.getLogger("ComfyCloudHybrid")

BASE_URL = "https://cloud.comfy.org"

# Documented upload limits
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_EDGE_PX = 16384
MAX_TOTAL_PX = 64 * 1024 * 1024


class CloudError(Exception):
    """Carries a user-facing message plus optional detail."""

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message + (f" [{detail}]" if detail else ""))
        self.user_message = user_message
        self.detail = detail


def _status_message(status: int, body: str) -> str:
    if status == 401:
        return ("Invalid Comfy Cloud API key. Please set it again under "
                "Settings → Comfy Cloud Hybrid (key from platform.comfy.org).")
    if status == 402:
        return "Not enough Comfy Cloud credits."
    if status == 429:
        return "Comfy Cloud subscription inactive or rate limit reached."
    if status == 400:
        try:
            parsed = json.loads(body)
            node_errors = parsed.get("node_errors") or {}
            err = parsed.get("error")
            if node_errors:
                parts = []
                for node_id, info in list(node_errors.items())[:5]:
                    cls = (info or {}).get("class_type", "?")
                    msgs = [e.get("message", "") for e in (info or {}).get("errors", [])][:2]
                    parts.append(f"{cls} ({node_id}): {'; '.join(m for m in msgs if m)}")
                return "Cloud rejected the workflow — " + " | ".join(parts)
            if isinstance(err, dict) and err.get("message"):
                return f"Cloud rejected the request: {err['message']}"
        except Exception:
            pass
        return "Cloud rejected the request (HTTP 400)."
    return f"Comfy Cloud error (HTTP {status})."


class ComfyCloudClient:
    def __init__(self, api_key: str, base_url: str | None = None):
        self._api_key = api_key
        # COMFY_CLOUD_BASE_URL override is for tests/mock servers
        self._base = (base_url or os.environ.get("COMFY_CLOUD_BASE_URL")
                      or BASE_URL).rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-API-Key": self._api_key},
                timeout=aiohttp.ClientTimeout(total=120),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _check(self, resp: aiohttp.ClientResponse) -> None:
        if resp.status < 400:
            return
        body = await resp.text()
        raise CloudError(_status_message(resp.status, body), detail=body[:500])

    # -- reference ---------------------------------------------------------

    async def object_info(self) -> dict:
        s = await self._get_session()
        async with s.get(f"{self._base}/api/object_info") as resp:
            await self._check(resp)
            return await resp.json()

    async def user_status(self) -> dict:
        s = await self._get_session()
        async with s.get(f"{self._base}/api/user") as resp:
            await self._check(resp)
            return await resp.json()

    # -- inputs ------------------------------------------------------------

    async def upload_image(self, data: bytes, filename: str) -> str:
        """POST /api/upload/image → cloud-side name to use in LoadImage."""
        if len(data) > MAX_UPLOAD_BYTES:
            raise CloudError(
                f"Image too large for Comfy Cloud upload ({len(data) / 1e6:.1f} MB > 50 MB)."
            )
        form = aiohttp.FormData()
        form.add_field("image", data, filename=filename, content_type="application/octet-stream")
        form.add_field("type", "input")
        s = await self._get_session()
        async with s.post(f"{self._base}/api/upload/image", data=form) as resp:
            await self._check(resp)
            payload = await resp.json()
        name = payload.get("name")
        if not name:
            raise CloudError("Cloud upload returned no filename.",
                             detail=json.dumps(payload)[:300])
        return name

    # -- jobs ----------------------------------------------------------------

    async def submit(self, prompt: dict, extra_data: dict | None = None) -> str:
        body: dict = {"prompt": prompt}
        if extra_data:
            body["extra_data"] = extra_data
        s = await self._get_session()
        async with s.post(f"{self._base}/api/prompt", json=body) as resp:
            await self._check(resp)
            payload = await resp.json()
        prompt_id = payload.get("prompt_id")
        if not prompt_id:
            raise CloudError("Cloud returned no prompt_id.",
                             detail=json.dumps(payload)[:300])
        return prompt_id

    async def job_status(self, job_id: str) -> str:
        s = await self._get_session()
        async with s.get(f"{self._base}/api/job/{job_id}/status") as resp:
            await self._check(resp)
            payload = await resp.json()
        return str(payload.get("status", "unknown"))

    async def job_detail(self, job_id: str) -> dict:
        s = await self._get_session()
        async with s.get(f"{self._base}/api/jobs/{job_id}") as resp:
            await self._check(resp)
            return await resp.json()

    @staticmethod
    def gpu_seconds(detail: dict) -> float | None:
        """GPU execution time (execution_start → execution_success) in seconds.
        This is what Comfy Cloud bills against; the API exposes no credit
        figure, so this is the honest cost proxy."""
        msgs = ((detail.get("execution_status") or {}).get("messages")) or []
        start = end = None
        for name, payload in msgs:
            ts = (payload or {}).get("timestamp")
            if name == "execution_start":
                start = ts
            elif name in ("execution_success", "execution_error"):
                end = ts
        if start is not None and end is not None and end >= start:
            return (end - start) / 1000.0
        return None

    async def interrupt(self, job_id: str | None = None) -> None:
        """Cancel running job(s); falls back to queue-delete for pending ones."""
        s = await self._get_session()
        try:
            async with s.post(f"{self._base}/api/interrupt") as resp:
                if resp.status >= 400:
                    log.warning("cloud interrupt returned HTTP %s", resp.status)
            if job_id:
                async with s.post(f"{self._base}/api/queue",
                                  json={"delete": [job_id]}) as resp:
                    if resp.status >= 400:
                        log.warning("cloud queue delete returned HTTP %s", resp.status)
        except Exception as e:  # best-effort — never mask the original error
            log.warning("cloud interrupt failed: %s", e)

    # -- outputs -------------------------------------------------------------

    async def download_output(self, filename: str, subfolder: str = "",
                              type: str = "output", **_ignored) -> bytes:
        """GET /api/view → follow 302 manually, no auth header to storage."""
        s = await self._get_session()
        params = {"filename": filename, "type": type}
        if subfolder:
            params["subfolder"] = subfolder
        async with s.get(f"{self._base}/api/view", params=params,
                         allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise CloudError("Cloud download: redirect without target URL.")
            elif resp.status < 400:
                return await resp.read()
            else:
                await self._check(resp)
                raise AssertionError("unreachable")
        # Fresh session without the API key header for the signed URL.
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300)) as clean:
            async with clean.get(location) as resp:
                if resp.status >= 400:
                    raise CloudError(
                        f"Cloud download failed (HTTP {resp.status}).")
                return await resp.read()

    # -- polling helper --------------------------------------------------------

    # Only these count as "a worker is actually rendering". Everything else
    # non-terminal (pending, waiting_to_dispatch, preparing = model loading,
    # plus whatever enum value the cloud invents next) is the WAITING phase —
    # verified live: the status endpoint emits "preparing" while a cold worker
    # loads models, which is not in the documented enum either.
    _RUNNING_STATES = ("in_progress", "running", "executing")

    # How long a run of transient network errors (dropped connection, stale
    # keep-alive, DNS hiccup) is tolerated before a poll failure escalates to
    # a real error. A single flaky request must never kill a job that is
    # likely still succeeding in the cloud — verified live: a stale pooled
    # connection hung until the per-request timeout fired and the raw
    # TimeoutError propagated straight out of the node after 15 minutes of
    # otherwise-healthy polling.
    _NETWORK_GRACE_S = 120

    async def wait_for_job(self, job_id: str, *, poll_interval: float,
                           timeout: float, queue_timeout: float | None = None,
                           on_tick=None, check_interrupted=None) -> dict:
        """Poll until completed; return job detail.

        Two separate limits: `queue_timeout` caps the WAITING phase (no worker
        assigned yet); `timeout` caps the RUNNING phase and only starts
        counting once the job is in_progress — a queue wait must never kill an
        almost-finished render.

        on_tick(status, phase, elapsed): phase is "waiting" | "running",
        elapsed is seconds spent in that phase.
        """
        loop = asyncio.get_event_loop()
        started = loop.time()
        running_since: float | None = None
        network_fail_since: float | None = None
        while True:
            if check_interrupted is not None:
                check_interrupted()
            try:
                status = await self.job_status(job_id)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                now = loop.time()
                if network_fail_since is None:
                    network_fail_since = now
                failing_for = now - network_fail_since
                if failing_for > self._NETWORK_GRACE_S:
                    raise CloudError(
                        f"Lost connection to Comfy Cloud for {int(failing_for)}s while "
                        "polling this job. The job itself may still be running or have "
                        "finished — check platform.comfy.org before resubmitting.") from e
                log.warning("transient network error polling job %s (failing for %ds) — "
                           "retrying: %s", job_id, int(failing_for), e)
                # a poisoned keep-alive connection is the likely culprit — force
                # a fresh one on the next attempt instead of reusing the pool
                await self.close()
                await asyncio.sleep(poll_interval)
                continue
            network_fail_since = None
            now = loop.time()
            # /api/job/{id}/status says "success", /api/jobs says "completed" —
            # the cloud uses both enums (verified against live API 2026-07)
            if status in ("completed", "success"):
                return await self.job_detail(job_id)
            if status in ("failed", "error", "cancelled"):
                detail = {}
                try:
                    detail = await self.job_detail(job_id)
                except CloudError:
                    pass
                err = (detail.get("execution_error")
                       or detail.get("error_message") or "")
                raise CloudError(f"Cloud job {status}."
                                 + (f" Error: {err}" if err else ""),
                                 detail=json.dumps(detail)[:800])
            if running_since is None and status in self._RUNNING_STATES:
                running_since = now
            if running_since is None:
                limit = queue_timeout if queue_timeout else timeout
                if now - started > limit:
                    await self.interrupt(job_id)
                    raise CloudError(
                        f"No cloud worker available within {int(limit)}s — job cancelled "
                        "(no render credits were used). Increase the queue timeout "
                        "in Settings → Comfy Cloud Hybrid.")
                if on_tick is not None:
                    on_tick(status, "waiting", now - started)
            else:
                if now - running_since > timeout:
                    await self.interrupt(job_id)
                    raise CloudError(
                        f"Time limit exceeded ({int(timeout)}s render time) — cloud job "
                        "cancelled. Increase the job timeout in Settings → "
                        "Comfy Cloud Hybrid.")
                if on_tick is not None:
                    on_tick(status, "running", now - running_since)
            await asyncio.sleep(poll_interval)

    # -- websocket progress ------------------------------------------------------

    async def listen_progress(self, job_id: str, on_progress) -> None:
        """Listen on the cloud websocket for real progress of one job.

        on_progress(kind, data): kind "progress" → {value, max}; kind "node"
        → {display}. Best-effort: any failure ends the listener silently —
        polling remains the source of truth for completion.
        """
        ws_base = self._base.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/ws?clientId=cch-{job_id[:8]}&token={self._api_key}"
        try:
            s = await self._get_session()
            async with s.ws_connect(url, heartbeat=30) as ws:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    mtype = payload.get("type")
                    data = payload.get("data") or {}
                    pid = data.get("prompt_id")
                    if pid is not None and pid != job_id:
                        continue
                    if mtype == "progress" and data.get("max"):
                        on_progress("progress", {"value": data.get("value", 0),
                                                 "max": data["max"]})
                    elif mtype == "executing" and data.get("node") is not None:
                        on_progress("node", {"display": str(data.get("display_node")
                                                           or data.get("node"))})
                    elif mtype in ("execution_success", "execution_error",
                                   "execution_interrupted") and pid == job_id:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("cloud ws progress listener ended: %s", e)
