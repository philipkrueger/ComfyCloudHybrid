---
name: sync-blueprints
description: Aktualisiert die kuratierte Comfy-Org-Blueprint-Sammlung und meldet, welche Cloud-Nodes sich ändern.
when_to_use: Wenn der User die kuratierten Blueprints aktualisieren/syncen will oder Comfy-Org neue Blueprints veröffentlicht hat.
allowed-tools: Bash, Read
---

# Kuratierte Blueprints synchronisieren

1. Sync ausführen: `python scripts/sync_blueprints.py` (stdlib-only, lädt
   github.com/Comfy-Org/Subgraph-Blueprints als Tarball nach `blueprints_curated/`,
   `blocked/` wird übersprungen).
2. Diff aus der Script-Ausgabe zusammenfassen (neu/entfernt); `blueprints_curated/.sync_meta.json`
   enthält Zeitstempel und Dateizahl.
3. Läuft ComfyUI (Port 8000)? → Rescan auslösen:
   `curl -s -X POST http://127.0.0.1:8000/api/cloudhybrid/rescan`
   und die Antwort melden (`new`/`updated`/`failed`).
4. Wenn `restart_required: true`: den User erinnern, dass **neue** Nodes erst nach
   einem ComfyUI-Neustart erscheinen; bestehende Nodes nutzen aktualisierte
   Konvertierungen sofort.
