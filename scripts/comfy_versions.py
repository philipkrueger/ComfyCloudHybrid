"""Report drift between the pinned, released and locally installed ComfyUI versions.

Stdlib only. Reads the pins from .github/workflows/tests.yml and README.md,
asks GitHub for the latest ComfyUI release (plus its frontend pin and the
current master), and lists local checkouts. Exit code 1 means the pinned
release is behind the latest release; --offline skips every network call.

    python3 scripts/comfy_versions.py [--json] [--offline] [--checkout DIR]...
    python3 scripts/comfy_versions.py --core-diff DIR [--fetch] [--base TAG]
    python3 scripts/comfy_versions.py --frontend-notes
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITHUB = "https://api.github.com/repos/Comfy-Org"
RAW = "https://raw.githubusercontent.com/Comfy-Org/ComfyUI"
DEFAULT_CHECKOUT_GLOBS = ("~/ComfyUI", "~/ComfyUI-Installs/*/ComfyUI", "~/ComfyUI-Installs/*")

# Upstream files whose changes can affect this pack (loader, V3 API, execution,
# history/UI output format, routes, video/audio sinks, subgraph storage).
CORE_WATCH = [
    "comfy_api/latest", "comfy_api/feature_flags.py", "execution.py", "nodes.py",
    "server.py", "comfy_execution", "main.py", "comfy/cli_args.py", "folder_paths.py",
    "comfy_extras/nodes_video.py", "comfy_extras/nodes_images.py",
    "comfy_extras/nodes_audio.py", "comfy_extras/nodes_primitive.py",
    "app/subgraph_manager.py", "app/custom_node_manager.py", "requirements.txt",
]
FRONTEND_KEYWORDS = re.compile(
    r"subgraph|widgets_values|promot|serializ|proxyWidget|blueprint|widget", re.I)


def _get(url: str, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "comfycloudhybrid-versions"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _frontend_pin(requirements: str) -> str | None:
    m = re.search(r"^comfyui-frontend-package==([\w.]+)", requirements, re.M)
    return m.group(1) if m else None


def pinned() -> dict:
    ci = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ci_ref = re.search(r"^\s*ref:\s*(v[\d.]+)", ci, re.M)
    doc = re.search(r"ComfyUI (v[\d.]+) / frontend ([\d.]+)\*\* \((\d{4}-\d{2}-\d{2})\)", readme)
    return {
        "ci_ref": ci_ref.group(1) if ci_ref else None,
        "readme_comfyui": doc.group(1) if doc else None,
        "readme_frontend": doc.group(2) if doc else None,
        "readme_date": doc.group(3) if doc else None,
    }


def released() -> dict:
    latest = json.loads(_get(f"{GITHUB}/ComfyUI/releases/latest"))
    tag = latest["tag_name"]
    info = {"tag": tag, "published": latest["published_at"][:10],
            "frontend": _frontend_pin(_get(f"{RAW}/{tag}/requirements.txt"))}
    master_version = re.search(r'__version__ = "([\d.]+)"', _get(f"{RAW}/master/comfyui_version.py"))
    info["master"] = master_version.group(1) if master_version else None
    info["master_frontend"] = _frontend_pin(_get(f"{RAW}/master/requirements.txt"))
    return info


def local_checkouts(extra: list[str]) -> list[dict]:
    candidates: list[Path] = [Path(p).expanduser() for p in extra]
    for pattern in DEFAULT_CHECKOUT_GLOBS:
        candidates.extend(sorted(Path.home().glob(pattern.removeprefix("~/"))))
    out, seen = [], set()
    for path in candidates:
        version_file = path / "comfyui_version.py"
        if path in seen or not version_file.is_file():
            continue
        seen.add(path)
        m = re.search(r'__version__ = "([\d.]+)"', version_file.read_text(encoding="utf-8"))
        req = path / "requirements.txt"
        venv = next((p for p in (path / ".venv/bin/python", path.parent / ".venv/bin/python")
                     if p.exists()), None)
        out.append({"path": str(path), "version": m.group(1) if m else None,
                    "frontend": _frontend_pin(req.read_text(encoding="utf-8")) if req.exists() else None,
                    "venv": str(venv) if venv else None,
                    "git": (path / ".git").exists()})
    return out


def _vtuple(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def core_diff(checkout: Path, base: str, target: str, fetch: bool) -> str:
    if fetch:
        subprocess.run(["git", "-C", str(checkout), "fetch", "-q", "--tags", "origin"], check=False)
    cmd = ["git", "-C", str(checkout), "diff", "--stat", base, target, "--"] + CORE_WATCH
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        return proc.stderr.strip() or f"git diff failed ({proc.returncode})"
    return proc.stdout.strip() or "(no changes in watched core files)"


def frontend_notes(low: str, high: str) -> list[str]:
    releases = json.loads(_get(f"{GITHUB}/ComfyUI_frontend/releases?per_page=100"))
    lines = []
    for rel in sorted(releases, key=lambda r: _vtuple(r["tag_name"])):
        tag = rel["tag_name"]
        if not (_vtuple(low) < _vtuple(tag) <= _vtuple(high)):
            continue
        hits = [l.strip(" *") for l in (rel.get("body") or "").splitlines() if FRONTEND_KEYWORDS.search(l)]
        lines.append(f"== {tag} ({rel['published_at'][:10]}) {len(hits)} relevant entries")
        lines.extend(f"   {h[:150]}" for h in hits)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--offline", action="store_true", help="skip GitHub queries")
    ap.add_argument("--checkout", action="append", default=[], help="extra ComfyUI checkout to inspect")
    ap.add_argument("--core-diff", metavar="DIR", help="git diff --stat of watched core files (pinned..latest)")
    ap.add_argument("--fetch", action="store_true", help="git fetch tags before --core-diff")
    ap.add_argument("--base", metavar="TAG", help="compare from this tag instead of the CI pin")
    ap.add_argument("--frontend-notes", action="store_true",
                    help="frontend release notes between the pinned and latest frontend")
    args = ap.parse_args()

    report = {"pinned": pinned(), "local": local_checkouts(args.checkout)}
    if not args.offline:
        try:
            report["released"] = released()
        except (urllib.error.URLError, TimeoutError, KeyError) as exc:
            report["released"] = None
            report["network_error"] = str(exc)
    rel = report.get("released") or {}
    pin = report["pinned"]["ci_ref"]
    report["drift"] = bool(rel) and _vtuple(rel.get("tag")) > _vtuple(pin)
    report["pins_consistent"] = pin == report["pinned"]["readme_comfyui"]

    base = args.base or pin
    if args.core_diff and rel:
        report["core_diff"] = core_diff(Path(args.core_diff).expanduser(), base, rel["tag"], args.fetch)
    if args.frontend_notes and rel:
        report["frontend_notes"] = frontend_notes(report["pinned"]["readme_frontend"], rel["frontend"])

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        p = report["pinned"]
        print(f"pinned   : ComfyUI {p['ci_ref']} (CI) / {p['readme_comfyui']} + frontend "
              f"{p['readme_frontend']} (README, {p['readme_date']})"
              + ("" if report["pins_consistent"] else "   <-- CI and README pins differ"))
        if rel:
            print(f"released : ComfyUI {rel['tag']} ({rel['published']}) + frontend {rel['frontend']}; "
                  f"master {rel['master']} + frontend {rel['master_frontend']}")
        elif report.get("network_error"):
            print(f"released : unavailable ({report['network_error']})")
        for c in report["local"]:
            print(f"local    : {c['version']:>7} + frontend {c['frontend'] or '?':<8} "
                  f"{'venv ' if c['venv'] else 'no-venv '}{'git ' if c['git'] else '    '}{c['path']}")
        print("drift    : " + ("YES - pinned release is behind the latest release"
                               if report["drift"] else "no"))
        if "core_diff" in report:
            print(f"\ncore diff {base}..{rel['tag']}:\n{report['core_diff']}")
        if "frontend_notes" in report:
            print(f"\nfrontend {p['readme_frontend']} -> {rel['frontend']}:")
            print("\n".join(report["frontend_notes"]) or "   (no releases in range)")
    return 1 if report["drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
