"""StudioFire P2 — Core / Web Service (FastAPI).

Web GUI + auth, playlist CRUD, scheduler/time events. Feeds P1 its queue over
localhost IPC and fills the local precache dir from the NAS (temp + verify +
atomic rename + manifest). Resolves dynamic playlist items (file /
folder-newest / folder-rotation) into concrete local files at precache time —
P1 only ever sees local paths. Ingests P1's play journal into SQLite.

May die at any time: audio is unaffected (see PLAN.md §5 crash matrix).

Run: python -m services.core.main  (Windows service via NSSM). Phase 1.
"""

raise SystemExit("P2 core: not implemented yet (Phase 1)")
