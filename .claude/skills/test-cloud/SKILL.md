---
name: test-cloud
description: Live-End-to-End-Test der Cloud-Ausführung — kostet echte Comfy-Cloud-Credits, kleinste Inputs verwenden.
when_to_use: Zum Verifizieren der Cloud-Ausführung nach Änderungen an Executor, Konverter oder Client. NICHT für Routine-Entwicklung (dafür die gemockten Tests).
allowed-tools: Bash, Read
---

# Live-Cloud-Test (⚠ verbraucht Credits)

Vorher IMMER die kostenlosen Offline-Tests:
`<ComfyUI>/.venv/bin/python -m unittest discover tests`

## Checkliste

1. Key-Status: `curl -s http://127.0.0.1:8000/api/cloudhybrid/status` —
   `key_source` muss `env` oder `config` sein. Sonst stoppen und den User bitten,
   den Key zu setzen (Settings → Comfy Cloud Hybrid, Key von platform.comfy.org).
2. Verbindung: `curl -s -X POST http://127.0.0.1:8000/api/cloudhybrid/test` —
   erwartet `"ok": true` (Account aktiv). 401/402/429-Meldungen an den User durchreichen.
3. Blueprint-Liste: `curl -s http://127.0.0.1:8000/api/cloudhybrid/blueprints` —
   Ziel-Blueprint muss `available: true` sein.
4. Kleinsten möglichen Job bauen: Workflow-JSON (API-Format) mit der Blueprint-Node
   (z. B. `CloudHybrid_Change_Style_*`), als Input ein winziges Testbild
   (z. B. 64×64) im ComfyUI-input-Ordner. Via `POST http://127.0.0.1:8000/api/prompt`
   queuen, History pollen (`GET /api/history/<prompt_id>`), Output-Bild prüfen.
5. Abbruch-Test (optional, kostet wenig): Job starten, sofort
   `POST http://127.0.0.1:8000/api/interrupt` — der Cloud-Job muss mit abgebrochen
   werden (Log: "cloud interrupt").
6. Ergebnis + ungefähre Credit-Kosten an den User berichten.
