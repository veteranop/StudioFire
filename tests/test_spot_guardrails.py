"""Unit tests: spot-rule delete guardrails (release A, item 1).

Covers the floor under the 2026-11-02 underwriting blackout: deletion is soft
(Undo for 72h), the API refuses a delete without a confirm, removing the LAST
legal-ID/underwriting rule needs the word DELETE typed, and only an admin may
delete at all.

Run: python tests/test_spot_guardrails.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                       # noqa: E402

from services.core import auth, db, spots                       # noqa: E402
from services.core.app import create_app                        # noqa: E402

passed = 0


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        dbp = os.path.join(td, "core.db")
        db.migrate(dbp)
        conn = db.connect(dbp)

        # ---- model level: soft delete, undo, purge, protection ----
        ads = spots.add(conn, "dir_ads", "interval", interval_min=15)
        ids = spots.add(conn, "dir_station_ids", "clock", clock_minutes="0")
        psa = spots.add(conn, "dir_psas", "manual")

        spots.remove(conn, psa, by="tester")
        check("deleted rule leaves list_all",
              psa not in [r["id"] for r in spots.list_all(conn)])
        check("deleted rule leaves list_enabled",
              psa not in [r["id"] for r in spots.list_enabled(conn)])
        deleted = spots.list_deleted(conn)
        check("deleted rule is still there for Undo",
              [r["id"] for r in deleted] == [psa])
        check("delete records who did it", deleted[0]["deleted_by"] == "tester")
        check("count_enabled ignores the deleted rule",
              spots.count_enabled(conn) == 2)

        spots.restore(conn, psa)
        check("Undo brings it back with the same id",
              psa in [r["id"] for r in spots.list_enabled(conn)])
        check("Undo list is empty again", spots.list_deleted(conn) == [])

        check("legal-ID and underwriting rules are protected",
              {r["id"] for r in spots.protected_rules(conn)} == {ads, ids})

        # the station's real rules come from the browse picker: folder_path set,
        # folder_key empty. They must be protected too, or the guardrail misses
        # exactly the rules it exists for.
        db.set_setting(conn, "dir_station_ids", "Z:\Legal ID's")
        db.set_setting(conn, "dir_ads", "Z:\1 - ALL DJ BREAKS")
        fp_ids = spots.add(conn, "", "clock", clock_minutes="0",
                           folder_path="z:/legal id's/")
        check("browse-picker legal-ID rule is protected",
              fp_ids in {r["id"] for r in spots.protected_rules(conn)})
        check("browse-picker legal-ID rule is a legal-ID rule",
              fp_ids in {r["id"] for r in spots.legal_id_rules(conn)})
        # park it again so the API block below runs against the 2 preset rules
        spots.remove(conn, fp_ids, by="tester")
        spots.remove(conn, psa)
        conn.execute("UPDATE spot_rules SET deleted_at = ? WHERE id = ?",
                     (time.time() - 73 * 3600, psa))
        conn.commit()
        purged = spots.purge_expired(conn, hours=72)
        check("a rule past the undo window is hard-purged", purged == 1)
        check("purged rule is gone from the database",
              spots.get(conn, psa) is None)

        # ---- API level: confirms, typed confirm, admin gate ----
        webtd = tempfile.mkdtemp(prefix="sf-guard-")
        cfg = {"station_name": "GuardTest", "db_path": dbp,
               "secret_path": os.path.join(webtd, "secret.key"),
               "precache_dir": os.path.join(webtd, "precache"),
               "engine_url": "http://127.0.0.1:9",
               "journal_path": os.path.join(webtd, "play_journal.jsonl"),
               "feeder_enabled": False}
        os.makedirs(cfg["precache_dir"], exist_ok=True)
        client = TestClient(create_app(cfg), follow_redirects=False)
        r = client.post("/setup", data={"username": "boss",
                                        "password": "longenough",
                                        "password2": "longenough"})
        check("setup signs the first admin in", r.status_code == 303,
              str(r.status_code))

        rules = client.get("/api/spots").json()["rules"]
        check("API lists the live rules", len(rules) == 2, str(len(rules)))
        psa2 = spots.add(conn, "dir_psas", "manual")

        r = client.request("DELETE", "/api/spots/%d" % psa2)
        check("delete with no confirm is refused (428)", r.status_code == 428,
              str(r.status_code))
        body = r.json().get("detail") or {}
        check("refusal says what stops airing", "stops_airing" in body, str(body)[:120])

        r = client.request("DELETE", "/api/spots/%d" % psa2,
                           json={"confirm": "yes"})
        check("confirmed delete succeeds", r.status_code == 200, str(r.status_code))
        out = r.json()
        check("response tells the client it can Undo",
              out.get("undo_seconds", 0) > 3600, str(out.get("undo_seconds")))
        ids_now = [x["id"] for x in client.get("/api/spots").json()["rules"]]
        check("deleted rule is not on air any more", psa2 not in ids_now)
        ids_del = [x["id"] for x in client.get("/api/spots/deleted").json()["rules"]]
        check("deleted rule is in the Undo list", psa2 in ids_del)

        r = client.post("/api/spots/%d/restore" % psa2)
        check("Undo works over the API", r.status_code == 200, str(r.status_code))

        # removing one of two protected rules is allowed with a plain confirm
        r = client.request("DELETE", "/api/spots/%d" % ids, json={"confirm": "yes"})
        check("removing one of two protected rules needs only a confirm",
              r.status_code == 200, str(r.status_code))

        # removing the LAST protected rule needs the typed word
        r = client.request("DELETE", "/api/spots/%d" % ads, json={"confirm": "yes"})
        check("last protected rule refuses a plain confirm (428)",
              r.status_code == 428, str(r.status_code))
        body = r.json().get("detail") or {}
        check("last-rule refusal demands the typed word",
              body.get("confirm_with") == "DELETE", str(body)[:160])
        check("last-rule refusal says credits may go off air",
              "off" in (body.get("warning") or "").lower(), str(body)[:160])

        r = client.request("DELETE", "/api/spots/%d" % ads,
                           json={"confirm": "DELETE"})
        check("last protected rule deletes with the typed word",
              r.status_code == 200, str(r.status_code))
        out = r.json()
        check("empty policy is warned about in the response",
              "off air" in (out.get("warning") or "").lower()
              or "no spot rules" in (out.get("warning") or "").lower(),
              str(out.get("warning"))[:160])

        # verdict agrees: an empty policy is a fault, not a clean desk
        v = client.get("/api/health/verdict").json()
        check("empty spot policy => verdict fault", v["state"] == "fault",
              str(v.get("headline"))[:100])

        # ---- the DJ (basic role) cannot delete rules at all ----
        auth.create_user(conn, "dj", "password123", "operator")
        dj = TestClient(create_app(cfg), follow_redirects=False)
        dj.post("/login", data={"username": "dj", "password": "password123"})
        r = dj.request("DELETE", "/api/spots/%d" % psa2, json={"confirm": "yes"})
        check("basic role cannot delete a spot rule (403)", r.status_code == 403,
              str(r.status_code))

        conn.close()

    print("\nSPOT GUARDRAILS OK (%d checks)" % passed)


if __name__ == "__main__":
    main()
