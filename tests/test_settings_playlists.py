"""Settings > Playlists: the default (everyday) rotation and the emergency playlist.

Covers the two slots on the Settings page and — the part that matters on air —
the local copy the emergency playlist keeps, above all that a sync which lands
nothing (music share offline) must NEVER empty the folder the failover tier
reads. That is the KDPI incident in one line (M2632102 / ticket 1036): an empty
emergency folder + a dangling base rotation put 1h41m of PSAs on air.

Run: python tests/test_settings_playlists.py
"""
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db as coredb                          # noqa: E402
from services.core import playlists as pl                       # noqa: E402
from services.core import settings_playlists as sp              # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=0.4, freq=440):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        frames = b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds)))
        w.writeframes(frames)


def audio_files(folder):
    try:
        return sorted(f for f in os.listdir(folder)
                      if f.lower().endswith((".mp3", ".wav", ".flac", ".m4a")))
    except OSError:
        return []


def fresh(tmp):
    dbp = os.path.join(tmp, "t.db")
    coredb.migrate(dbp)
    conn = coredb.connect(dbp)
    cfg = {"paths": {"data_root": tmp, "data_dir": tmp,
                     "emergency_dir": os.path.join(tmp, "emergency")}}
    return conn, cfg, dbp


def main():
    tmp = tempfile.mkdtemp(prefix="sf-plset-")
    try:
        lib = os.path.join(tmp, "library")
        tracks = []
        for i, name in enumerate(("a.mp3", "b.mp3", "c.mp3")):
            p = os.path.join(lib, name)
            make_wav(p, freq=300 + 80 * i)
            tracks.append(p)
        folder = os.path.join(lib, "show")
        for name in ("d.mp3", "e.mp3"):
            make_wav(os.path.join(folder, name))

        conn, cfg, dbp = fresh(tmp)
        dest = cfg["paths"]["emergency_dir"]

        everyday = pl.create_playlist(conn, "Everyday")
        pl.add_items_bulk(conn, everyday, [
            {"path": p, "title": os.path.basename(p)} for p in tracks])
        backup = pl.create_playlist(conn, "Backup")
        pl.add_items_bulk(conn, backup, [
            {"path": tracks[0], "title": "a"}])
        pl.add_item(conn, backup, "folder-rotation", folder, "show")
        empty = pl.create_playlist(conn, "Empty")

        # ---- the default slot -------------------------------------------
        d = sp.get_default(conn)
        check("default slot starts empty and is not an error",
              d["id"] is None and d["valid"] is False and d["dangling_id"] is None)
        d = sp.set_default(conn, everyday)
        check("setting the default writes the same key PUT ON AIR uses",
              coredb.get_setting(conn, "active_playlist_id") == str(everyday))
        check("default reports its name", d["name"] == "Everyday" and d["valid"])

        raised = False
        try:
            sp.set_default(conn, 999999)
        except ValueError:
            raised = True
        check("a default pointing at a missing playlist is refused", raised)

        # the M2632102 shape: a stored id whose playlist was deleted
        coredb.set_setting(conn, "active_playlist_id", "424242")
        d = sp.get_default(conn)
        check("a dangling default is reported as dangling, not as empty",
              d["valid"] is False and d["dangling_id"] == 424242
              and d["id"] is None)
        sp.set_default(conn, everyday)

        check("clearing the default is allowed",
              sp.set_default(conn, None)["id"] is None
              and (coredb.get_setting(conn, "active_playlist_id") or "") == "")
        sp.set_default(conn, everyday)

        # ---- the emergency slot + its local copy -------------------------
        e = sp.get_emergency(conn, cfg)
        check("emergency slot starts empty", e["id"] is None and e["valid"] is False)

        raised = False
        try:
            sp.set_emergency(conn, 424242, cfg)
        except ValueError:
            raised = True
        check("an emergency playlist that does not exist is refused", raised)

        e = sp.set_emergency(conn, backup, cfg)
        check("emergency slot saved",
              coredb.get_setting(conn, "emergency_playlist_id") == str(backup))

        # an operator's own file already in the folder must survive
        make_wav(os.path.join(dest, "station-id.wav"), freq=900)

        res = sp.sync_now(conn, backup, cfg)
        check("every playlist item resolved to a file (1 file + folder item)",
              res["copied"] == 3 and res["total_wanted"] == 3)
        check("the files are really in the emergency folder",
              audio_files(dest) == ["a.mp3", "d.mp3", "e.mp3", "station-id.wav"])
        check("copies are byte-identical",
              open(tracks[0], "rb").read()
              == open(os.path.join(dest, "a.mp3"), "rb").read())
        check("the operator's own file is not claimed by the manifest",
              "station-id.wav" not in sp.mirrored_names(dest))
        check("no .part files left behind",
              not any(f.endswith(".part") for f in os.listdir(dest)))
        man = sp._read_manifest(dest)
        check("the manifest names the playlist it came from",
              man["playlist_id"] == backup and man["playlist_name"] == "Backup")

        res = sp.sync_now(conn, backup, cfg)
        check("a second sync is idempotent (no re-copies, nothing pruned)",
              res["copied"] == 3 and res["removed"] == 0)

        st = sp.mirror_status(conn, cfg)
        check("status reports the copy as in sync",
              st["in_sync"] is True and st["mirrored"] == 3 and st["bytes"] > 0)
        check("status separates curated audio from the mirror",
              st["curated"] == ["station-id.wav"])

        # ---- editing the playlist prunes only what we own ----------------
        item_id = conn.execute(
            "SELECT id FROM playlist_items WHERE playlist_id = ? AND path = ?",
            (backup, tracks[0])).fetchone()[0]
        pl.remove_item(conn, backup, item_id)
        res = sp.sync_now(conn, backup, cfg)
        check("a removed item is pruned from the local copy", res["removed"] == 1)
        check("the pruned file is gone but the curated one stays",
              "a.mp3" not in audio_files(dest)
              and "station-id.wav" in audio_files(dest))

        # ---- THE SAFETY RULE: an unreadable share must not empty it ------
        before = audio_files(dest)
        gone_item = os.path.join(tmp, "gone", "vanished.mp3")
        p2 = pl.create_playlist(conn, "Offline")
        pl.add_items_bulk(conn, p2, [
            {"path": gone_item, "title": "vanished"}])
        res = sp.sync_now(conn, p2, cfg)
        check("a sync that resolves nothing reports kept_existing",
              res.get("kept_existing") is True and res["copied"] == 0)
        check("and the existing emergency copy is untouched",
              audio_files(dest) == before)
        check("the manifest still names the good playlist",
              sp._read_manifest(dest)["playlist_id"] == backup)

        # ---- size cap ----------------------------------------------------
        em2 = os.path.join(tmp, "emergency_cap")
        cfg2 = {"paths": {"data_root": tmp, "emergency_dir": em2}}
        res = sp.sync_now(conn, backup, cfg2, max_bytes_=1)
        check("a one-byte budget copies nothing and truncates",
              res["copied"] == 0 and res["truncated"] is True
              and res.get("kept_existing") is True)
        check("nothing was written to the capped folder", audio_files(em2) == [])

        em3 = os.path.join(tmp, "emergency_cap2")
        cfg3 = {"paths": {"data_root": tmp, "emergency_dir": em3}}
        res = sp.sync_now(conn, backup, cfg3, max_files=1)
        check("the file cap is honoured",
              res["copied"] == 1 and res["truncated"] is True)

        # ---- an empty playlist clears the copy (deliberate, not a failure)
        res = sp.sync_now(conn, empty, cfg)
        check("an emptied playlist clears the mirror", res["copied"] == 0)
        check("curated audio still survives a deliberate clear",
              audio_files(dest) == ["station-id.wav"])

        # ---- the ENGINE sees a copy made while it is on air --------------
        # The engine used to scan this folder once, at startup. A playlist set
        # at 09:00 whose copy landed at 09:01 was therefore invisible to a
        # failover at 10:00 — the mirror has to be picked up live.
        from services.engine import supervisor as supmod
        eng_dir = os.path.join(tmp, "emergency_engine")
        os.makedirs(eng_dir, exist_ok=True)
        eng = supmod.EngineSupervisor({
            "mpv_path": os.path.join(ROOT, "bin", "mpv.exe"),
            "emergency_dir": eng_dir,
            "state_path": os.path.join(tmp, "engine_state.json"),
            "heartbeat_path": os.path.join(tmp, "engine_hb.txt"),
            "journal_path": os.path.join(tmp, "engine_journal.jsonl"),
            "precache_dir": os.path.join(tmp, "engine_precache"),
        })
        real_probe = supmod.probe_decodable
        supmod.probe_decodable = lambda *a, **k: True
        try:
            eng._validate_emergency_folder()
            check("engine: an empty emergency folder gives it nothing to play",
                  eng._emergency_candidates() == [])
            make_wav(os.path.join(eng_dir, "late.mp3"), freq=500)
            check("engine: a copy made while on air is picked up, no restart",
                  [os.path.basename(p) for p in eng._emergency_candidates()]
                  == ["late.mp3"])
            os.remove(os.path.join(eng_dir, "late.mp3"))
            check("engine: and a rescan notices the folder emptying again",
                  eng._emergency_candidates() == [])
        finally:
            supmod.probe_decodable = real_probe
            eng._journal.close()

        # ---- deleting a playlist leaves no dangling reference ------------
        sp.set_emergency(conn, backup, cfg)
        sp.sync_now(conn, backup, cfg)
        sp.set_default(conn, backup)
        check("backup is now both slots", True)
        cleared = sp.clear_references(conn, backup, cfg)
        check("deleting it clears BOTH slots", sorted(cleared) ==
              sorted(["active_playlist_id", "emergency_playlist_id"]))
        check("no dangling default is left behind",
              sp.get_default(conn)["valid"] is False
              and sp.get_default(conn)["dangling_id"] is None)
        check("the emergency copy was removed with it",
              audio_files(dest) == ["station-id.wav"])
        check("clear_mirror is idempotent", sp.clear_mirror(cfg) == 0)

        # deleting through the real function must do the same
        sp.set_default(conn, everyday)
        pl.delete_playlist(conn, everyday, cfg)
        check("delete_playlist clears the reference in one step",
              (coredb.get_setting(conn, "active_playlist_id") or "") == "")
        conn.close()

        # ---- the API the Settings card talks to ---------------------------
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        conn = coredb.connect(dbp)
        app = FastAPI()
        app.state.get_conn = lambda: conn
        app.state.api_user = lambda: {"username": "t", "role": "admin"}
        app.state.cfg = dict(cfg, db_path=dbp)
        sp.EMERGENCY_MIRROR.__init__()          # no job from a previous run
        sp.register(app)
        client = TestClient(app)

        keep = pl.create_playlist(conn, "Night")
        pl.add_items_bulk(conn, keep, [{"path": tracks[1], "title": "b"}])

        r = client.get("/api/settings/playlists")
        body = r.json()
        check("GET returns both slots and the playlist list",
              r.status_code == 200 and "default" in body
              and "emergency" in body and len(body["playlists"]) >= 4)
        check("track counts are in the picker payload",
              any(p["name"] == "Night" and p["tracks"] == 1
                  for p in body["playlists"]))

        r = client.post("/api/settings/playlists", json={"default_playlist_id": keep})
        check("POST sets the default and reports it",
              r.status_code == 200
              and r.json()["default"]["name"] == "Night"
              and "default" in r.json()["changed"])

        r = client.post("/api/settings/playlists", json={"default_playlist_id": None})
        check("POST with null clears the default",
              r.status_code == 200 and r.json()["default"]["id"] is None)

        r = client.post("/api/settings/playlists", json={"default_playlist_id": 987654})
        check("POST refuses a playlist that does not exist", r.status_code == 400)

        r = client.post("/api/settings/playlists", json={"emergency_playlist_id": keep})
        check("POST sets the emergency playlist", r.status_code == 200)
        sp.EMERGENCY_MIRROR.wait(15)
        st = sp.EMERGENCY_MIRROR.status()
        check("the copy runs as a background job and finishes",
              st["state"] == "done" and st["result"]["copied"] == 1)
        check("the copy is on disk after the job",
              "b.mp3" in audio_files(dest))

        r = client.post("/api/settings/playlists/emergency/refresh")
        check("refresh re-runs the copy", r.status_code == 200)
        sp.EMERGENCY_MIRROR.wait(15)
        check("refresh reports a finished job",
              sp.EMERGENCY_MIRROR.status()["state"] == "done")

        r = client.post("/api/settings/playlists", json={"emergency_playlist_id": None})
        check("clearing the emergency playlist wipes its copy",
              r.status_code == 200
              and sp.mirror_status(conn, cfg)["mirrored"] == 0)
        r = client.post("/api/settings/playlists/emergency/refresh")
        check("refresh with nothing set is a clean 400", r.status_code == 400)

        # ---- the Settings page carries the card --------------------------
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(
            os.path.join(ROOT, "web", "templates")))
        src = open(os.path.join(ROOT, "web", "templates", "settings.html"),
                   encoding="utf-8").read()
        check("renderPlaylists is wired into the page boot",
              "loadPlaylists()" in src and "pl-default" in src
              and "pl-emergency" in src)
        check("the card names both slots",
              "Default playlist" in src and "Emergency playlist" in src)
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{passed} checks passed")


if __name__ == "__main__":
    main()
