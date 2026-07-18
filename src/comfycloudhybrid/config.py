"""Server-side configuration: API key and tunables.

Key precedence: COMFY_CLOUD_API_KEY env var > config.json (0600, gitignored).
The key is never exposed through any GET route — only a masked suffix.
Importable without ComfyUI (no comfy imports here).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("ComfyCloudHybrid")

PACK_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PACK_ROOT / "config.json"
CACHE_DIR = PACK_ROOT / "cache"
CURATED_DIR = PACK_ROOT / "blueprints_curated"

ENV_KEY = "COMFY_CLOUD_API_KEY"

DEFAULTS = {
    "poll_interval_s": 2.0,
    # running-phase cap; matches the Standard/Creator cloud runtime limit
    "job_timeout_s": 1800,
    # how long to wait for a free cloud worker before giving up
    "queue_timeout_s": 900,
    "register_unavailable": True,
    # model-free blueprints (pure pixel/logic ops) run fine locally — the
    # cloud round-trip only adds latency and GPU cost, so skip them by default
    "skip_local_capable": True,
    "sources": {
        "user": True,
        "custom_nodes": True,
        "comfyui_blueprints": True,
        "curated": True,
    },
}

_lock = threading.Lock()


def _read_file() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("config.json unreadable (%s) — using defaults", e)
        return {}


def _write_file(data: dict) -> None:
    with _lock:
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def get_api_key() -> str | None:
    env = os.environ.get(ENV_KEY)
    if env:
        return env.strip()
    key = _read_file().get("api_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def get_api_key_source() -> str:
    """'env' | 'config' | 'none' — for the status route."""
    if os.environ.get(ENV_KEY):
        return "env"
    if _read_file().get("api_key"):
        return "config"
    return "none"


def masked_key() -> str | None:
    key = get_api_key()
    if not key:
        return None
    return "…" + key[-4:] if len(key) > 4 else "…"


def set_api_key(key: str | None) -> None:
    data = _read_file()
    if key:
        data["api_key"] = key.strip()
    else:
        data.pop("api_key", None)
    _write_file(data)


def get(name: str):
    """Read a tunable with fallback to DEFAULTS."""
    data = _read_file()
    if name in data:
        return data[name]
    return DEFAULTS[name]


def set_option(name: str, value) -> None:
    if name not in DEFAULTS:
        raise KeyError(f"unknown option: {name}")
    data = _read_file()
    data[name] = value
    _write_file(data)
