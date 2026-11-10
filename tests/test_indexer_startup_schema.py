"""Indexer startup vs the schema - one migrator, and a gate that waits.

core and worker both called `db.migrate()` at startup. On a release that
changed the schema the core won the race and the worker died seconds later with
`sqlite3.OperationalError: duplicate column name: deleted_at`; the library index
then froze while /health stayed green, and the in-app updater still logged
`[done]` because its worker check only asked "is a process with that name
running right now" (ticket #997, on-air box 192.168.6.222, 2026-11-08).

This pins the fix:

  * `wait_for_schema()` is False while nothing has migrated and True once the
    chain has run - the worker waits instead of racing;
  * concurrent migrators all exit 0 and land ONE clean schema;
  * the lock is genuinely exclusive across processes (the teeth: the pre-fix
    module has no lock at all, which is what let two chains overlap);
  * the worker's real shape - a second process that waits - ends current.

Run: python tests/test_indexer_startup_schema.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db  # noqa: E402

passed = 0


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def tmpdb(tag):
    return os.path.join(tempfile.mkdtemp(prefix="sf-schema-" + tag + "-"),
                        "studiofire.db")


MIGRATE_CODE = (
    "import sys; sys.path.insert(0, r'{root}');"
    "from services.core import db; db.migrate(r'{path}');"
    "print('migrated', db.schema_version(r'{path}'))"
)

LOCK_CODE = """
import sys, time
sys.path.insert(0, r"{root}")
from services.core import db
f = open(r"{log}", "a")
with db._migration_lock(r"{path}"):
    f.write("in %f\\n" % time.time())
    f.flush()
    time.sleep(0.8)
    f.write("out %f\\n" % time.time())
    f.flush()
f.close()
"""


def test_wait_then_migrate():
    path = tmpdb("wait")
    check("fresh db is not current", db.schema_version(path) < db.SCHEMA_VERSION)
    t0 = time.monotonic()
    check("wait_for_schema times out when nobody migrates",
          db.wait_for_schema(path, timeout=1.5, poll=0.2) is False)
    check("wait actually waited", time.monotonic() - t0 >= 1.4)

    db.migrate(path)
    check("migrate lands on SCHEMA_VERSION",
          db.schema_version(path) == db.SCHEMA_VERSION)
    check("wait_for_schema true once current",
          db.wait_for_schema(path, timeout=1.0, poll=0.1) is True)
    db.migrate(path)
    check("second migrate is a no-op",
          db.schema_version(path) == db.SCHEMA_VERSION)


def test_two_migrators_at_once():
    path = tmpdb("race")
    code = MIGRATE_CODE.format(root=ROOT, path=path)
    procs = [subprocess.Popen([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) for _ in range(3)]
    outs = [p.communicate(timeout=300)[0] for p in procs]
    rcs = [p.returncode for p in procs]
    check("all concurrent migrators exit 0", all(rc == 0 for rc in rcs),
          "rcs=%s " % rcs + " | ".join(outs)[-500:])
    check("no duplicate-column death", "duplicate column" not in " ".join(outs).lower(),
          " ".join(outs)[-500:])
    check("one clean schema at the end",
          db.schema_version(path) == db.SCHEMA_VERSION)
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    conn.close()
    check("exactly one schema_version row", len(rows) == 1, str(rows))


def test_lock_is_exclusive():
    path = tmpdb("lock")
    log = path + ".intervals"
    open(log, "w").close()
    code = LOCK_CODE.format(root=ROOT, path=path, log=log)
    procs = [subprocess.Popen([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) for _ in range(2)]
    outs = [p.communicate(timeout=180)[0] for p in procs]
    rcs = [p.returncode for p in procs]
    check("both lock holders exit 0", all(rc == 0 for rc in rcs),
          " | ".join(outs)[-600:])
    rows = [ln.split() for ln in open(log).read().splitlines() if ln.strip()]
    enters = sorted(float(r[1]) for r in rows if r[0] == "in")
    exits = sorted(float(r[1]) for r in rows if r[0] == "out")
    check("two critical sections recorded", len(enters) == 2 and len(exits) == 2,
          open(log).read())
    first, second = sorted(zip(enters, exits))
    check("critical sections do not overlap", first[1] <= second[0], str([first, second]))


def test_worker_waits_for_core():
    path = tmpdb("worker")
    ran = []

    def core():
        time.sleep(0.6)          # core wins the start race
        db.migrate(path)
        ran.append("core")

    th = threading.Thread(target=core)
    th.start()
    ok = db.wait_for_schema(path, timeout=60, poll=0.2)   # the worker's call
    th.join()
    check("worker sees the schema go current", ok is True)
    check("core is the migrator", ran == ["core"])
    check("schema is current after the wait",
          db.schema_version(path) == db.SCHEMA_VERSION)


def main():
    test_wait_then_migrate()
    test_two_migrators_at_once()
    test_worker_waits_for_core()
    test_lock_is_exclusive()
    print("\n%d checks passed" % passed)


if __name__ == "__main__":
    main()
