---
name: comfy-compat
description: Gleicht das Repo mit der aktuellen ComfyUI-Release ab (Backend + Frontend-Pin), prüft die Kompatibilität gegen einen echten Checkout und aktualisiert Pins/Code, wenn nötig.
when_to_use: Wenn eine neue ComfyUI-Version erschienen ist, der User "auf die neueste Comfy-Version updaten" will, CI gegen einen veralteten Tag läuft oder nach längerer Pause geprüft werden soll, ob das Pack noch passt.
allowed-tools: Bash, Read, Edit, Write
---

# ComfyUI-Kompatibilität abgleichen

Ziel: Repo-Pins (CI-Tag, README-Zeile) == neueste ComfyUI-Release, Check-Script
und Offline-Tests grün gegen diese Release, Pack-Code an Upstream-Änderungen angepasst.
Kein Netzwerk beim Pack-Import, keine Cloud-Credits — alles hier ist offline/gemockt.

## 1. Drift feststellen

```bash
python3 scripts/comfy_versions.py            # Exit 1 = Pin hinter neuester Release
python3 scripts/comfy_versions.py --json     # für Details
```

Zeigt: gepinnte Version (CI `ref:` + README-Zeile), neueste GitHub-Release inkl.
`comfyui-frontend-package`-Pin, `master`-Stand (kommende Version) und lokale
Checkouts (`~/ComfyUI`, `~/ComfyUI-Installs/*/ComfyUI`) mit venv/git-Status.

- **Kein Drift** → kurz berichten (inkl. master/Frontend-Ausblick), fertig.
- **Pins inkonsistent** (CI ≠ README) → in Schritt 5 beide angleichen.
- **Drift** → weiter.

## 2. Upstream-Änderungen sichten

```bash
python3 scripts/comfy_versions.py --core-diff "<Checkout mit git>" --fetch --frontend-notes   # --base TAG: anderer Ausgangs-Tag
```

`--core-diff` zeigt `git diff --stat <pin>..<latest>` der beobachteten Core-Dateien
(`CORE_WATCH` im Script). Danach die relevanten Diffs lesen
(`git -C <Checkout> diff <pin> <latest> -- <Datei>`) mit Fokus auf:

| Upstream | Betrifft im Pack |
|---|---|
| `server.py` (`PromptServer.__init__`, Routen, `/object_info`, `/upload/*`) | `scripts/check_comfyui.py`, `routes.py` |
| `nodes.py` (`load_custom_node`, `comfy_entrypoint`, `EXTENSION_WEB_DIRS`) | `extension.py`, `__init__.py` |
| `comfy_api/latest/_io.py` (`Schema`, `Hidden`, `NodeOutput`, Input-Typen) | `node_factory.py`, `extension.py` |
| `comfy_api/latest/_ui.py` (`PreviewVideo`, `SaveImage`-Formate) | `executor.py` (History-Parsing) |
| `execution.py` (`get_input_data`, `_async_map_node_over_list`, `validate_prompt`) | `check_comfyui.py`, async-Node-Ausführung |
| `comfy_extras/nodes_video.py`, `nodes_audio.py`, `nodes_images.py` | Sink-Klassen im Converter (`flatten.py`) |
| `folder_paths.py` | Upload-/Output-Pfade in `executor.py` |
| `app/subgraph_manager.py`, Frontend-Notes (subgraph/widgets_values/promoted) | `scanner.py`, `converter/flatten.py`, `converter/widgets.py`, `web/js` |
| `requirements.txt` (`aiohttp`, `av`, Frontend-Pin) | `pyproject.toml` |

`--frontend-notes` filtert die Frontend-Release-Notes zwischen altem und neuem
Frontend-Pin nach Subgraph-/Widget-Stichwörtern. Änderungen an der
Subgraph-Serialisierung (`widgets_values`, `proxyWidgets`, promoted inputs) ⇒
Fixture in `tests/fixtures/` + Test in `tests/test_current_subgraphs.py` ergänzen.

## 3. Checkout der neuen Release beschaffen

Bevorzugt einen vorhandenen Checkout mit venv aus der Script-Ausgabe nutzen
(z. B. `~/ComfyUI-Installs/…/ComfyUI`). Fehlt einer, **vorher den User fragen**
(Torch-Download ist groß), dann:

```bash
V=v0.XX.0; D=~/ComfyUI-Installs/ComfyUI-$V
git clone --depth 1 --branch $V https://github.com/Comfy-Org/ComfyUI.git "$D"
python3.12 -m venv "$D/.venv" && "$D/.venv/bin/pip" install torch torchvision torchaudio \
  --index-url https://download.pytorch.org/whl/cpu && "$D/.venv/bin/pip" install -r "$D/requirements.txt"
```

## 4. Kompatibilität prüfen und fixen

```bash
"<Checkout>/.venv/bin/python" scripts/check_comfyui.py "<Checkout>" --report /tmp/compat.json
```

Erwartet: `ComfyUI <version>: N nodes registered; … async node executions passed`.
Bei Fehlern in dieser Reihenfolge:

1. **Check-Script** selbst (Konstruktor-Signaturen wie `PromptServer(loop, asset_manager)`
   seit 0.36) — versionsverträglich lösen (`try: import …; except ImportError`), damit
   das Script gegen ältere Checkouts weiter läuft.
2. **Pack-Code** — Fix + Offline-Regressionstest in `tests/`. Converter-Änderung ⇒
   `CONVERTER_VERSION` in `src/comfycloudhybrid/converter/__init__.py` bumpen.
3. Gegen den *alten* gepinnten Checkout gegenprüfen, falls vorhanden (Abwärtskompatibilität).

Danach Offline-Tests:

```bash
"<Checkout>/.venv/bin/python" -m unittest discover tests
node --test tests/js/*.test.mjs
```

## 5. Pins und Doku aktualisieren

- `.github/workflows/tests.yml`: `ref: v0.XX.0`
- `README.md`: Zeile `Compatibility checked on **ComfyUI vX / frontend Y** (YYYY-MM-DD)`
  und der Satz zur CI-Version im Abschnitt *Development*
- `CLAUDE.md` → *Bekannte Grenzen*, wenn sich Grenzen/Verhalten geändert haben
- `pyproject.toml`: Patch-Version bumpen, wenn Pack-Code geändert wurde
- `python3 scripts/comfy_versions.py` muss jetzt `drift: no` melden

## 6. Berichten

Kurz: Upstream-Änderungen mit Auswirkung, was im Repo geändert wurde, Test-/Check-
Ergebnisse (mit Zahlen), Ausblick auf `master`/nächsten Frontend-Pin. Commit nur
auf Wunsch des Users.
