#!/usr/bin/env python3
"""Sync the curated Comfy-Org/Subgraph-Blueprints collection (MIT) into
blueprints_curated/. Stdlib only — runnable without ComfyUI:

    python scripts/sync_blueprints.py

Skips the blocked/ directory; keeps subfolders (incl. cloud_only/) so nodes
get categorized. Afterwards trigger a rescan (command palette:
"Comfy Cloud: Blueprints neu scannen") and restart ComfyUI for new nodes.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

REPO_TARBALL = "https://github.com/Comfy-Org/Subgraph-Blueprints/archive/refs/heads/main.tar.gz"
PACK_ROOT = Path(__file__).resolve().parents[1]
TARGET = PACK_ROOT / "blueprints_curated"
META = TARGET / ".sync_meta.json"


def main() -> int:
    print(f"Lade {REPO_TARBALL} …")
    req = urllib.request.Request(REPO_TARBALL, headers={"User-Agent": "ComfyCloudHybrid-sync"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    print(f"{len(data) / 1e6:.1f} MB geladen, entpacke …")

    before = {p.relative_to(TARGET).as_posix()
              for p in TARGET.rglob("*.json")} if TARGET.is_dir() else set()

    staging = TARGET.with_suffix(".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".json"):
                continue
            # strip the top-level "Subgraph-Blueprints-main/" component
            rel = Path(*Path(member.name).parts[1:])
            if not rel.parts or rel.parts[0] == "blocked":
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.read())
            count += 1

    if count == 0:
        print("FEHLER: keine Blueprint-JSONs im Archiv gefunden.", file=sys.stderr)
        shutil.rmtree(staging)
        return 1

    if TARGET.exists():
        shutil.rmtree(TARGET)
    staging.rename(TARGET)

    after = {p.relative_to(TARGET).as_posix() for p in TARGET.rglob("*.json")}
    META.write_text(json.dumps({"synced_at": time.time(), "source": REPO_TARBALL,
                                "files": len(after)}, indent=2))

    added, removed = sorted(after - before), sorted(before - after)
    print(f"✓ {len(after)} Blueprints synchronisiert → {TARGET}")
    if added:
        print(f"  neu ({len(added)}): " + ", ".join(added[:10]) + (" …" if len(added) > 10 else ""))
    if removed:
        print(f"  entfernt ({len(removed)}): " + ", ".join(removed[:10]) + (" …" if len(removed) > 10 else ""))
    print("Danach: Rescan auslösen und ComfyUI neu starten, damit neue Nodes erscheinen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
