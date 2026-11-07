"""Schema migrations 13-15 — prove BOTH directions before believing them.

`services/core/db.py` keeps an append-only MIGRATIONS list. A station that
already ran version N never re-runs it, so a new migration must land cleanly on
an OLD database with real rows in it. This test builds a v12 database (the
version shipped in v1.6.3), puts data in it, then migrates and asserts:

  * schema_version ends at 14
  * existing rows are untouched and the new columns are NULL (nobody's rule
    appears "deleted" because of the upgrade)
  * skipped_items (v14) and the reporting tables (v15) exist and are empty
  * a FRESH database also lands at 14 with the same shape

Run: python tests/test_migrations_13_15.py
"""
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, spots  # noqa: E402

passed = 0


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def _version(conn):
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else 0


def build_old(path, version):
    """Replay the append-only list up to `version`, exactly as an install of
    that release would look."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version "
                 "(version INTEGER NOT NULL)")
    for v, script in db.MIGRATIONS:
        if v > version:
            break
        conn.executescript(script)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version VALUES (?)", (v,))
    conn.commit()
    conn.close()


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        old = os.path.join(td, "studiofire.db")
        build_old(old, 12)

        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        check("old database is at v12", _version(conn) == 12)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(spot_rules)")]
        check("v12 has no deleted_at yet", "deleted_at" not in cols, str(cols))
        check("v12 has no skipped_items yet",
              conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                           "AND name='skipped_items'").fetchone()[0] == 0)

        # real data: rules built the way the station builds them, plus history
        conn.execute("INSERT INTO spot_rules(folder_key, folder_path, label, "
                     "trigger, interval_min, enabled, created_at) "
                     "VALUES ('', ?, ?, 'interval', 30, 1, ?)",
                     ("Z:\\Legal ID's", "Legal ID's (random)", time.time() - 999))
        conn.execute("INSERT INTO spot_rules(folder_key, label, trigger, "
                     "interval_min, enabled, created_at) "
                     "VALUES ('dir_psas', 'PSAs', 'manual', NULL, 1, ?)",
                     (time.time() - 500,))
        # the station's folder settings — the policy check matches these paths
        for k, v in (("dir_station_ids", "Z:\Legal ID's"),
                     ("dir_ads", "Z:\\Ads"),
                     ("dir_psas", "Z:\PSAs")):
            conn.execute("INSERT OR REPLACE INTO settings(key, value) "
                         "VALUES (?, ?)", (k, v))
        before = [dict(r) for r in conn.execute(
            "SELECT id, label, enabled, folder_path FROM spot_rules ORDER BY id")]
        conn.commit()
        conn.close()

        # ---- the upgrade ----
        db.migrate(old)
        conn = db.connect(old)
        check("migrated to the current schema", _version(conn) == db.SCHEMA_VERSION)
        after = [dict(r) for r in conn.execute(
            "SELECT id, label, enabled, folder_path FROM spot_rules ORDER BY id")]
        check("existing spot rules survive untouched", before == after,
              "%r != %r" % (before, after))
        deleted = [r[0] for r in conn.execute(
            "SELECT deleted_at FROM spot_rules")]
        check("new deleted_at column is NULL on old rows",
              all(d is None for d in deleted), str(deleted))
        check("new deleted_by column is NULL on old rows",
              all(r[0] is None for r in conn.execute(
                  "SELECT deleted_by FROM spot_rules")))
        check("skipped_items exists after the upgrade",
              conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                           "AND name='skipped_items'").fetchone()[0] == 1)
        check("skipped_items is empty after the upgrade",
              conn.execute("SELECT COUNT(*) FROM skipped_items").fetchone()[0] == 0)
        check("reporting tables exist after the upgrade",
              conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                           "AND name IN ('queue_attribution','playlist_airings')"
                           ).fetchone()[0] == 2)
        check("the new index exists",
              conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                           "AND name='idx_spot_rules_deleted'").fetchone()[0] == 1)
        # the upgraded rules still behave: both are live policy
        check("upgraded rules still list", len(spots.list_all(conn)) == 2)
        kinds = {r["label"]: spots.rule_kind(conn, r) for r in spots.list_all(conn)}
        check("the browse-picker legal-ID rule is recognised after upgrade",
              kinds.get("Legal ID's (random)") == "ids", str(kinds))
        conn.close()

        # ---- a fresh install lands in the same place ----
        fresh = os.path.join(td, "fresh.db")
        db.migrate(fresh)
        fc = db.connect(fresh)
        check("fresh install is at the current schema", _version(fc) == db.SCHEMA_VERSION)
        check("fresh install has skipped_items",
              fc.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                         "AND name='skipped_items'").fetchone()[0] == 1)
        fcols = [r[1] for r in fc.execute("PRAGMA table_info(spot_rules)")]
        check("fresh install has the soft-delete columns",
              "deleted_at" in fcols and "deleted_by" in fcols, str(fcols))
        # and migrations are idempotent: running again changes nothing
        fc.close()
        db.migrate(fresh)
        fc = db.connect(fresh)
        check("re-running migrate is a no-op", _version(fc) == db.SCHEMA_VERSION)
        fc.close()

    # a database written by NEWER code must be refused, not silently mistreated
    newer = os.path.join(tempfile.mkdtemp(prefix="sf-newer-"), "n.db")
    db.migrate(newer)
    nc = sqlite3.connect(newer)
    nc.execute("DELETE FROM schema_version")
    nc.execute("INSERT INTO schema_version VALUES (?)", (db.SCHEMA_VERSION + 1,))
    nc.commit()
    nc.close()
    try:
        db.migrate(newer)
    except RuntimeError as exc:
        check("a newer schema is refused", "newer code" in str(exc), str(exc)[:60])
    else:
        check("a newer schema is refused", False, "migrate accepted vN+1")
    print("\nMIGRATIONS 13+14 OK (%d checks)" % passed)


if __name__ == "__main__":
    main()
