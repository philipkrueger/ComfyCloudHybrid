# Intentionally minimal so submodules (converter, config, cloud_client) are
# importable in tests without a running ComfyUI. All ComfyUI wiring lives in
# extension.py, which is only imported by the top-level package __init__.
