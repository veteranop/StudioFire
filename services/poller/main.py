"""StudioFire P4 — Monitor Poller Service.

Async network probes, hard timeouts, retry backoff. Writes status (with
last-known-good + checked-at timestamps) for P2 to render; a dead probe
degrades to STALE and never blocks anything.

Probes: UniFi local API (UCG-Lite), Barix HTTP CGI, WireGuard tunnel,
BW TX150 via raw TCP to serial-to-IP adapter (connect -> query -> DISCONNECT;
the adapter allows only one TCP connection at a time).

Run: python -m services.poller.main  (Windows service via NSSM). Phase 2.
"""

raise SystemExit("P4 poller: not implemented yet (Phase 2)")
