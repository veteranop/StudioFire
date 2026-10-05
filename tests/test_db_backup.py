"""§6.4 pre-migration DB backup (services/core/db.py).

migrate() must, before applying any pending migration, snapshot the DB with
the SQLite backup API into data/backups/pre-migration-*.db, verify the COPY
with PRAGMA integrity_check, keep only the last 5, and ABORT the migrate (leave
the DB untouched) if the copy or its check fails.

Run: python -m tests.test_db_backup
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def _backups(td):
    bdir = os.path.join(td, "backups")
    if not os.path.isdir(bdir):
        return []
    return sorted(f for f in os.listdir(bdir)
                  if f.startswith("pre-migration-") and f.endswith(".db"))


def _schema_version(db_path):
    conn = sqlite3.connect(db_path)
    try:
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            return 0            # table not created yet
        return row[0] if row else 0
    finally:
        conn.close()


def _has_table(db_path, name):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    finally:
        conn.close()


def test_backup_on_real_migration():
    td = tempfile.mkdtemp(prefix="sf-dbbak-")
    db_path = os.path.join(td, "studiofire.db")

    db.migrate(db_path)       # fresh (v0) -> latest: has pending migrations

    bk = _backups(td)
    check("one pre-migration backup written on a real migration", len(bk) == 1)
    check("backup is named v<old>-to-v<new>",
          bk[0].startswith(f"pre-migration-v0-to-v{db.SCHEMA_VERSION}-"))
    check("the DB migrated to the latest schema",
          _schema_version(db_path) == db.SCHEMA_VERSION)

    # the backup itself passes integrity_check and is a real DB (pre-migration,
    # so it is the EMPTY v0 db — schema_version not yet populated)
    bpath = os.path.join(td, "backups", bk[0])
    conn = sqlite3.connect(bpath)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    check("the backup passes its own integrity_check", ok == "ok")

    # running migrate again has no pending work -> no new backup
    db.migrate(db_path)
    check("no backup on a no-op migrate (already current)",
          len(_backups(td)) == 1)


def test_prune_keeps_last_five():
    td = tempfile.mkdtemp(prefix="sf-dbprune-")
    db_path = os.path.join(td, "studiofire.db")
    db.migrate(db_path)                        # a valid source DB (+1 backup)
    for _ in range(7):                         # 1 + 7 = 8 snapshots made
        db._pre_migration_backup(db_path, 0, db.SCHEMA_VERSION)
    check("prune keeps only the last 5 pre-migration backups",
          len(_backups(td)) == db.BACKUP_KEEP == 5)


def test_failed_integrity_aborts_migrate():
    td = tempfile.mkdtemp(prefix="sf-dbfail-")
    db_path = os.path.join(td, "studiofire.db")
    saved = db._integrity_ok
    db._integrity_ok = lambda conn: False       # force the COPY to "fail"
    try:
        raised = False
        try:
            db.migrate(db_path)
        except RuntimeError as exc:
            raised = True
            msg = str(exc)
    finally:
        db._integrity_ok = saved
    check("a failing integrity check raises out of migrate", raised
          and "integrity" in msg)
    check("the DB was NOT migrated (left untouched)",
          _schema_version(db_path) == 0 and not _has_table(db_path, "tracks"))
    check("the unverifiable backup copy was not left behind",
          _backups(td) == [])


def test_copy_failure_aborts_and_cleans_up():
    td = tempfile.mkdtemp(prefix="sf-dbgarbage-")
    garbage = os.path.join(td, "studiofire.db")
    with open(garbage, "wb") as f:
        f.write(b"this is not a sqlite database at all " * 8)
    raised = False
    try:
        db._pre_migration_backup(garbage, 0, 1)
    except RuntimeError:
        raised = True
    check("a copy failure (non-DB source) raises RuntimeError", raised)
    check("the partial/failed backup file is removed", _backups(td) == [])


def main():
    test_backup_on_real_migration()
    test_prune_keeps_last_five()
    test_failed_integrity_aborts_migrate()
    test_copy_failure_aborts_and_cleans_up()
    print(f"DB BACKUP OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
