# ComfyCloudHybrid

**Subgraph Blueprints als Cloud-Nodes:** Dieses Custom-Node-Paket registriert für
jeden ComfyUI Subgraph Blueprint eine eigene Node („☁ <Name>") mit exakt den
Inputs/Outputs des Subgraphs. Bei Ausführung läuft der Subgraph auf der
offiziellen [Comfy Cloud](https://www.comfy.org/cloud) — Input-Bilder werden
automatisch hochgeladen, der Job wird gepollt, die Ergebnis-Bilder kommen als
Tensor zurück in den lokalen Graphen. Hybrides Offloading wie bei Partner-Nodes,
aber für eigene Subgraphs.

## Voraussetzungen

- ComfyUI ≥ 0.5.0 (async Node-Execution + V3-API)
- **Comfy Cloud Abo** (Standard/Creator/Pro — der Free-Tier hat keinen API-Zugang)
- API-Key von der [Comfy Dev Platform](https://platform.comfy.org/profile/api-keys)
  (Profile → API Keys → New; der Key ist **nur einmal** sichtbar — direkt kopieren)

## Installation

```bash
cd <ComfyUI>/custom_nodes
git clone https://github.com/philipkrueger/ComfyCloudHybrid
# ComfyUI neu starten
```

API-Key setzen — eine der beiden Varianten:

1. **Settings → Comfy Cloud Hybrid → API key** (wird serverseitig in
   `config.json` gespeichert, landet nie in den Frontend-Settings). Die
   Key-Seite lässt sich direkt öffnen: Command-Palette →
   „Comfy Cloud: Open API key page". Oder:
2. Umgebungsvariable `COMFY_CLOUD_API_KEY` (hat Vorrang).

Verbindung prüfen: Command-Palette → „Comfy Cloud: Test connection".
Die Settings-Oberfläche und alle Statusanzeigen sind auf Englisch.

## Blueprint-Quellen

Beim Start werden alle Blueprints gescannt und konvertiert (Präzedenz von oben
nach unten, Dedupe über die Subgraph-UUID):

1. `user/<id>/subgraphs/*.json` — selbst veröffentlichte Blueprints
   (Subgraph auswählen → „Add Subgraph to Library")
2. `custom_nodes/*/subgraphs/*.json` — von Node-Packs mitgelieferte
3. `<ComfyUI>/blueprints/*.json` — von neueren ComfyUI-Versionen ausgelieferte
4. `blueprints_curated/` — die offizielle kuratierte Sammlung
   [Comfy-Org/Subgraph-Blueprints](https://github.com/Comfy-Org/Subgraph-Blueprints)
   (100+ Blueprints inkl. `cloud_only/`). **Ist bereits im Repo enthalten** — ein
   frischer Clone zeigt die Cloud-Nodes sofort. Auf den neuesten Stand von Comfy-Org
   bringen (optional):

   ```bash
   python scripts/sync_blueprints.py
   ```

Die Nodes erscheinen unter **cloud hybrid/** in der Node-Library — einsortiert
in die offizielle Blueprint-Taxonomie aus der Subgraph-Definition (z. B.
`Image generation and editing/Text to image`). **Modellfreie Blueprints**
(GLSL-Filter, Crops, reine Utilities) werden standardmäßig übersprungen, weil
sie lokal schneller und kostenlos laufen — abschaltbar über Settings →
Comfy Cloud Hybrid → „Skip model-free blueprints".
Nach dem Sync: Command-Palette → „Comfy Cloud: Rescan blueprints";
**neue** Nodes erscheinen nach einem ComfyUI-Neustart (geänderte Blueprints
werden ohne Neustart hot übernommen).

## Nodes

- **☁ <Blueprint-Name>** — pro Blueprint generiert. Subgraph-Inputs (IMAGE/MASK/
  STRING/INT/FLOAT/BOOLEAN) werden echte Node-Inputs; promotete Widgets (z. B.
  seed) werden optionale Widgets. IMAGE-Outputs kommen als Tensor zurück.
  Intern referenzierte Dateien (fixe `LoadImage`) werden automatisch mit
  hochgeladen (Upload-Dedupe per Inhalts-Hash).
- **☁ Cloud Workflow ausführen** — generischer Fallback: beliebiges API-Format-
  JSON (File → Export (API)) plus bis zu 4 Bild-Inputs über die Platzhalter
  `%CCH_IMAGE_1%`…`%CCH_IMAGE_4%`.

## Grenzen & Verhalten

- Eingänge: IMAGE/MASK und Wert-Typen (STRING/INT/FLOAT/BOOLEAN/COMBO).
  Ausgänge: IMAGE und **VIDEO** (via SaveVideo, kommt als VIDEO zurück).
  LATENT/MODEL/CLIP- und AUDIO-Grenzen werden mit klarer Meldung abgelehnt.
- Kosten: Nach dem Job zeigt die Node die **GPU-Zeit** (Abrechnungsbasis der
  Cloud) an — die API liefert keine exakten Credits, die GPU-Sekunden sind die
  ehrliche Näherung. Statusanzeigen und Fehlermeldungen sind auf Englisch.
- Blueprints, die Nodes verwenden, die es in der Cloud nicht gibt, werden als
  deaktivierte Nodes mit Fehlgrund registriert (abschaltbar via
  `register_unavailable`).
- Partner-/API-Nodes im Blueprint (z. B. Gemini) laufen in der Cloud unter
  deinem Account und verbrauchen Credits.
- Lokaler Interrupt (Stop-Button) bricht auch den Cloud-Job ab.
- **Fortschritt:** Die Node zeigt Warten und Rendern getrennt an — „⏳ Warte auf
  Cloud-Worker" solange kein Worker zugeteilt ist, danach echten Render-Fortschritt
  in Prozent (via Cloud-WebSocket). Zwei getrennte Timeouts: das **Warte-Timeout**
  (Default 900 s) bricht ab, solange noch keine Render-Credits verbraucht wurden;
  das **Job-Timeout** (Default 1800 s) zählt erst ab Render-Start — ein langer
  Queue-Stau killt also keinen fast fertigen Job mehr.
- Fehler kommen mit klaren Meldungen: ungültiger Key (401), zu wenig Credits
  (402), inaktives Abo (429), Node-Validierungsfehler mit Klassenname.

## Entwicklung

```bash
# Offline-Tests (kein Netz, keine Credits):
python -m unittest discover tests
```

Architektur und Regeln: siehe [CLAUDE.md](CLAUDE.md).

## Mitwirken

Issues und Pull Requests sind willkommen. Bitte vor einem PR die Offline-Tests laufen
lassen (`python -m unittest discover tests`) — sie brauchen kein Netz und kosten keine
Credits. Architektur-Überblick, Modul-Verantwortlichkeiten und die harten Projektregeln
stehen in [CLAUDE.md](CLAUDE.md).

## Lizenz

[MIT](LICENSE) © Philip Krüger
