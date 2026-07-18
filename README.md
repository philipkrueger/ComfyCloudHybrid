# ComfyCloudHybrid

**Subgraph Blueprints as cloud nodes:** This custom-node pack registers a
dedicated node ("☁ <Name>") for every ComfyUI Subgraph Blueprint, exposing
exactly the subgraph's inputs/outputs. On execution the subgraph runs on the
official [Comfy Cloud](https://www.comfy.org/cloud) — input images are uploaded
automatically, the job is polled, and the result images come back as tensors
into your local graph. Hybrid offloading like the partner nodes, but for your
own subgraphs.

## Requirements

- ComfyUI ≥ 0.5.0 (async node execution + V3 API)
- **Comfy Cloud subscription** (Standard/Creator/Pro — the free tier has no API access)
- API key from the [Comfy Dev Platform](https://platform.comfy.org/profile/api-keys)
  (Profile → API Keys → New; the key is shown **only once** — copy it right away)

## Installation

```bash
cd <ComfyUI>/custom_nodes
git clone https://github.com/philipkrueger/ComfyCloudHybrid
# restart ComfyUI
```

Set the API key — either of the two options:

1. **Settings → Comfy Cloud Hybrid → API key** (stored server-side in
   `config.json`, never lands in the frontend settings). The key page can be
   opened directly: Command Palette → "Comfy Cloud: Open API key page". Or:
2. Environment variable `COMFY_CLOUD_API_KEY` (takes precedence).

Check the connection: Command Palette → "Comfy Cloud: Test connection".

## Blueprint sources

On startup all blueprints are scanned and converted (precedence top to bottom,
deduplicated by the subgraph UUID):

1. `user/<id>/subgraphs/*.json` — your own published blueprints
   (select a subgraph → "Add Subgraph to Library")
2. `custom_nodes/*/subgraphs/*.json` — shipped by node packs
3. `<ComfyUI>/blueprints/*.json` — shipped by newer ComfyUI versions
4. `blueprints_curated/` — the official curated collection
   [Comfy-Org/Subgraph-Blueprints](https://github.com/Comfy-Org/Subgraph-Blueprints)
   (100+ blueprints incl. `cloud_only/`). **Already bundled in this repo** — a
   fresh clone shows the cloud nodes immediately. To pull the latest from
   Comfy-Org (optional):

   ```bash
   python scripts/sync_blueprints.py
   ```

The nodes appear under **cloud hybrid/** in the node library — sorted into the
official blueprint taxonomy from the subgraph definition (e.g.
`Image generation and editing/Text to image`). **Model-free blueprints**
(GLSL filters, crops, pure utilities) are skipped by default because they run
faster and for free locally — toggleable via Settings → Comfy Cloud Hybrid →
"Skip model-free blueprints".
After a sync: Command Palette → "Comfy Cloud: Rescan blueprints";
**new** nodes appear after a ComfyUI restart (changed blueprints are picked up
hot without a restart).

## Nodes

- **☁ <Blueprint name>** — generated per blueprint. Subgraph inputs (IMAGE/MASK/
  STRING/INT/FLOAT/BOOLEAN) become real node inputs; promoted widgets (e.g.
  seed) become optional widgets. IMAGE outputs come back as tensors.
  Internally referenced files (fixed `LoadImage`) are uploaded automatically
  (upload dedupe by content hash).
- **☁ Run Cloud Workflow** — generic fallback: any API-format JSON
  (File → Export (API)) plus up to 4 image inputs via the placeholders
  `%CCH_IMAGE_1%`…`%CCH_IMAGE_4%`.

## Limits & behavior

- Inputs: IMAGE/MASK and value types (STRING/INT/FLOAT/BOOLEAN/COMBO).
  Outputs: IMAGE and **VIDEO** (via SaveVideo, returned as VIDEO).
  LATENT/MODEL/CLIP and AUDIO boundaries are rejected with a clear message.
- Cost: after the job the node shows the **GPU time** (the cloud's billing
  basis) — the API does not expose exact credits, so GPU seconds are the honest
  approximation. Status displays and error messages are in English.
- Blueprints using nodes that don't exist in the cloud are registered as
  disabled nodes with the failure reason (toggleable via
  `register_unavailable`).
- Partner/API nodes in a blueprint (e.g. Gemini) run in the cloud under your
  account and consume credits.
- A local interrupt (Stop button) also cancels the cloud job.
- **Progress:** the node shows waiting and rendering separately — "⏳ Waiting for
  cloud worker" while no worker is assigned, then real render progress in percent
  (via cloud WebSocket). Two separate timeouts: the **wait timeout**
  (default 900 s) aborts while no render credits have been spent yet; the
  **job timeout** (default 1800 s) only counts from render start — so a long
  queue backlog no longer kills an almost-finished job.
- Errors come with clear messages: invalid key (401), insufficient credits
  (402), inactive subscription (429), node validation errors with the class name.

## Development

```bash
# Offline tests (no network, no credits):
python -m unittest discover tests
```

Architecture and rules: see [CLAUDE.md](CLAUDE.md).

## Contributing

Issues and pull requests are welcome. Please run the offline tests before opening
a PR (`python -m unittest discover tests`) — they need no network and cost no
credits. An architecture overview, module responsibilities, and the hard project
rules live in [CLAUDE.md](CLAUDE.md).

## License

[MIT](LICENSE) © Philip Krüger
