# ComfyCloudHybrid

ComfyUI-Custom-Node-Paket: Subgraph Blueprints werden als je eine Node registriert
(„☁ <Name>") und bei Ausführung auf der offiziellen Comfy Cloud (cloud.comfy.org)
ausgeführt — Inputs hochladen, Job pollen, Bilder zurückladen.

## Architektur

| Modul (src/comfycloudhybrid/) | Verantwortung |
|---|---|
| `extension.py` | V3-Entrypoint: Scan → Convert → Nodes registrieren; Routen-Import |
| `scanner.py` | Blueprint-Discovery (saved/user/custom_nodes/shipped/curated), Dedupe per Def-UUID; `saved` = höchste Präzedenz |
| `converter/flatten.py` | Subgraph-Flattening → flaches API-Format (Kern-Algorithmus) |
| `converter/widgets.py` | positionale `widgets_values` → benannte Inputs (seed-/upload-Slots!) |
| `converter/schema_source.py` | Node-Schemas: Cloud-`object_info`-Cache, lokaler Fallback |
| `node_factory.py` | dynamische `io.ComfyNode`-Klassen pro Blueprint |
| `ondemand.py` | Rechtsklick-Subgraph→Cloud-Node: `preflight` (Report: errors/warnings), `to_generic`-Prompt (Instant-Test via generische Node), `save_blueprint` (→ `saved_blueprints/`) |
| `executor.py` | Laufzeit: inject → upload → submit → poll → download |
| `cloud_client.py` | aiohttp-Client für die Cloud-API (X-API-Key) |
| `routes.py` | `/cloudhybrid/*` (api_key, status, test, rescan, config, blueprints, convert) |
| `cache.py` | `cache/converted/`, `cloud_object_info.json` (24h), `uploads.json` |
| `config.py` | API-Key (env → config.json 0600) + Tunables |

## Kommandos

- Tests (offline, kein Netz, keine Credits): `<ComfyUI>/.venv/bin/python -m unittest discover tests`
- Kuratierte Blueprints syncen: `python scripts/sync_blueprints.py`
- Dev-Install: `ln -s "$(pwd)" <ComfyUI>/custom_nodes/ComfyCloudHybrid`
- Lokales ComfyUI: Port 8000 (`<ComfyUI>/run.sh`)

## Harte Regeln

- **Nie** `config.json` oder API-Keys committen/loggen; Key nie in GET-Responses.
- **Kein Netzwerk beim Import/Startup** — Cloud-Katalog wird nur im Background-Task
  oder via `/cloudhybrid/rescan` geholt.
- Konverter-Änderungen ⇒ `CONVERTER_VERSION` in `converter/__init__.py` bumpen
  (Cache-Invalidierung).
- Fail-soft pro Blueprint: ein defektes JSON darf den Paket-Import nie brechen.
- Async-Kontext: nur `aiohttp` + `asyncio.sleep`, nie `requests`/`time.sleep`.
- Cloud-Aufrufe kosten echte Credits — für Entwicklung die gemockten Tests nutzen;
  Live-Tests nur bewusst (Skill `/test-cloud`).
- V3-only: keine `NODE_CLASS_MAPPINGS` exportieren (würde `comfy_entrypoint` verdrängen).

## Bekannte Grenzen

- Subgraph-Grenzen: rein IMAGE/MASK/AUDIO (FLAC-Upload→LoadAudio)/Werte
  (nicht übertragbare Input-Typen wie BOUNDING_BOX werden gedroppt, wenn alle
  Ziele optional sind). Raus: IMAGE, MASK (MaskToImage→SaveImage), VIDEO,
  AUDIO (SaveAudio/FLAC→Waveform), plus Wert-Outputs STRING/INT/FLOAT/
  BOOLEAN/BOUNDING_BOX über den PreviewAny-Text-Kanal (History `text`).
  LATENT/MODEL/CLIP-Ausgänge werden übersprungen (mit Hinweis).
- Neue Blueprints ⇒ ComfyUI-Neustart (Rescan lädt nur Konvertierungen hot).
- Nested-Instance-Proxywidgets werden als Defaults eingebacken, nicht promoted.
- Reroute-Nodes werden beim Flattening kollabiert (PASSTHROUGH_CLASSES in flatten.py).
- Numeric-Constraints (min/max/control_after_generate) kommen aus dem Cloud-Schema;
  nur Seeds randomisieren. Werte werden beim Injizieren auf die Cloud-Range geklemmt.
- Kosten: die API liefert keine Credits — als Näherung wird die GPU-Zeit
  (execution_start→success) angezeigt. Laufzeit-Texte sind Englisch.
- Node-Kategorien = `category`-Feld der Subgraph-Definition (offizielle
  Taxonomie); Fallback: Namens-Präfix-Heuristik (`category_group`).
- Modellfreie Blueprints (`local_capable`, Regex-Erkennung in flatten.py)
  werden per Default nicht registriert (`skip_local_capable`).
