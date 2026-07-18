"""Runtime caches under <pack>/cache/: converted workflows, cloud node
catalog (24h TTL), upload dedupe map. All fail-soft — cache trouble must
never break node execution."""

from __future__ import annotations

import json
import logging
import threading
import time

from . import config
from .converter.model import ConvertedWorkflow

log = logging.getLogger("ComfyCloudHybrid")

CONVERTED_DIR = config.CACHE_DIR / "converted"
OBJECT_INFO_PATH = config.CACHE_DIR / "cloud_object_info.json"
UPLOADS_PATH = config.CACHE_DIR / "uploads.json"

OBJECT_INFO_TTL_S = 24 * 3600

_lock = threading.Lock()


def _ensure_dirs() -> None:
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)


# -- converted workflows -----------------------------------------------------

def save_converted(slug: str, source_sha256: str, converter_version: int,
                   cw: ConvertedWorkflow, had_cloud_catalog: bool = False) -> None:
    _ensure_dirs()
    payload = {"meta": {"source_sha256": source_sha256,
                        "converter_version": converter_version,
                        "had_cloud_catalog": had_cloud_catalog,
                        "saved_at": time.time()},
               "workflow": cw.to_dict()}
    try:
        with open(CONVERTED_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:
        log.warning("could not write converted cache for %s: %s", slug, e)


def load_converted(slug: str, source_sha256: str | None = None,
                   converter_version: int | None = None) -> ConvertedWorkflow | None:
    """Load cache entry; None when absent or stale (hash/version mismatch)."""
    try:
        with open(CONVERTED_DIR / f"{slug}.json", "r", encoding="utf-8") as f:
            payload = json.load(f)
        meta = payload.get("meta") or {}
        if source_sha256 is not None and meta.get("source_sha256") != source_sha256:
            return None
        if converter_version is not None and meta.get("converter_version") != converter_version:
            return None
        cw = ConvertedWorkflow.from_dict(payload["workflow"])
        # A conversion made without the cloud catalog (or one that flagged
        # missing classes) goes stale as soon as a newer catalog exists —
        # availability may have changed.
        if not meta.get("had_cloud_catalog") or cw.missing_classes:
            fetched = _object_info_fetched_at()
            if fetched and fetched > meta.get("saved_at", 0):
                return None
        return cw
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("converted cache for %s unreadable: %s", slug, e)
        return None


# -- cloud object_info --------------------------------------------------------

def _object_info_fetched_at() -> float | None:
    try:
        with open(OBJECT_INFO_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("fetched_at")
    except Exception:
        return None


def load_object_info(max_age_s: float = OBJECT_INFO_TTL_S) -> dict | None:
    try:
        with open(OBJECT_INFO_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if time.time() - payload.get("fetched_at", 0) > max_age_s:
            return None
        return payload.get("data") or None
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("object_info cache unreadable: %s", e)
        return None


def save_object_info(data: dict) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(OBJECT_INFO_PATH, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f)
    except OSError as e:
        log.warning("could not write object_info cache: %s", e)


# -- upload dedupe -------------------------------------------------------------

def _read_uploads() -> dict:
    try:
        with open(UPLOADS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_upload(digest: str) -> str | None:
    entry = _read_uploads().get(digest)
    return entry.get("cloud_name") if isinstance(entry, dict) else None


def set_upload(digest: str, cloud_name: str) -> None:
    with _lock:
        data = _read_uploads()
        data[digest] = {"cloud_name": cloud_name, "uploaded_at": time.time()}
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(UPLOADS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as e:
            log.warning("could not write uploads cache: %s", e)


def drop_upload(digest: str) -> None:
    with _lock:
        data = _read_uploads()
        if digest in data:
            del data[digest]
            try:
                with open(UPLOADS_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError:
                pass
