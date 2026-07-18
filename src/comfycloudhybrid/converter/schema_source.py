"""Node schema lookup: cloud object_info (authoritative — the execution
target) with local NODE_CLASS_MAPPINGS fallback for cold-cache runs."""

from __future__ import annotations

import logging

log = logging.getLogger("ComfyCloudHybrid")


class SchemaSource:
    def __init__(self, cloud_object_info: dict | None = None, use_local: bool = True):
        self._cloud = cloud_object_info or {}
        self._use_local = use_local
        self._local_cache: dict[str, dict | None] = {}

    @property
    def has_cloud_catalog(self) -> bool:
        return bool(self._cloud)

    def get(self, class_type: str) -> dict | None:
        """object_info-style entry {'input': {...}, 'input_order': {...}} or None."""
        entry = self._cloud.get(class_type)
        if entry is not None:
            return entry
        if self._use_local:
            return self._get_local(class_type)
        return None

    def get_cloud(self, class_type: str) -> dict | None:
        """Entry from the cloud catalog only — for anything whose options must
        be valid IN THE CLOUD (model selector combos etc.). Local option lists
        would leak local filenames into cloud prompts."""
        return self._cloud.get(class_type)

    def known(self, class_type: str) -> bool:
        return self.get(class_type) is not None

    def in_cloud(self, class_type: str) -> bool:
        """Only meaningful when has_cloud_catalog; else optimistically True."""
        if not self._cloud:
            return True
        return class_type in self._cloud

    def _get_local(self, class_type: str) -> dict | None:
        if class_type in self._local_cache:
            return self._local_cache[class_type]
        entry = None
        try:
            import nodes  # ComfyUI — only available in-process
            cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
            if cls is not None:
                entry = {"input": cls.INPUT_TYPES()}
        except ImportError:
            pass
        except Exception as e:
            log.warning("local INPUT_TYPES failed for %s: %s", class_type, e)
        self._local_cache[class_type] = entry
        return entry
