"""P2 foundation tests: schema/migrations, scrypt auth, first-run setup,
login/session flow, role guard plumbing.

Run: python tests/test_core_foundation.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient          # noqa: E402

from services.core import auth, db                 # noqa: E402
from services.core.app import create_app           # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def main():
    td = tempfile.mkdtemp(prefix="sf-core-")
    db_path = os.path.join(td, "test.db")

    # ---- schema / migrations
    db.migrate(db_path)
    db.migrate(db_path)  # idempotent
    conn = db.connect(db_path)
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    check("schema at latest version", v == db.SCHEMA_VERSION)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL mode active", mode == "wal")

    # ---- password hashing
    h = auth.hash_password("hunter22!")
    check("scrypt verify ok", auth.verify_password("hunter22!", h))
    check("scrypt wrong pw rejected", not auth.verify_password("nope", h))
    check("garbage hash rejected", not auth.verify_password("x", "not$a$hash"))
    h2 = auth.hash_password("hunter22!")
    check("unique salts", h != h2)

    # ---- users
    auth.create_user(conn, "op", "operator123", "operator")
    check("authenticate ok", auth.authenticate(conn, "op", "operator123"))
    check("authenticate bad pw", auth.authenticate(conn, "op", "wrong") is None)
    check("authenticate unknown user",
          auth.authenticate(conn, "ghost", "x") is None)
    conn.close()

    # ---- web app: first-run setup + login flow (fresh DB)
    td2 = tempfile.mkdtemp(prefix="sf-core-web-")
    cfg = {
        "station_name": "TestFM",
        "db_path": os.path.join(td2, "web.db"),
        "secret_path": os.path.join(td2, "secret.key"),
    }
    client = TestClient(create_app(cfg), follow_redirects=False)

    check("health open without auth",
          client.get("/health").json()["ok"] is True)
    r = client.get("/")
    check("anonymous dashboard -> /login",
          r.status_code == 303 and r.headers["location"] == "/login")
    r = client.get("/login")
    check("no users: /login -> /setup",
          r.status_code == 303 and r.headers["location"] == "/setup")
    r = client.post("/setup", data={"username": "boss",
                                    "password": "short", "password2": "short"})
    check("setup rejects short password", b"at least" in r.content)
    r = client.post("/setup", data={"username": "boss",
                                    "password": "longenough",
                                    "password2": "different1"})
    check("setup rejects mismatch", b"match" in r.content)
    r = client.post("/setup", data={"username": "boss",
                                    "password": "longenough",
                                    "password2": "longenough"})
    check("setup creates admin + signs in",
          r.status_code == 303 and auth.SESSION_COOKIE in r.cookies)
    check("dashboard renders when signed in",
          client.get("/").status_code == 200)
    r = client.get("/setup")
    check("setup locked after first run",
          r.status_code == 303 and r.headers["location"] == "/login")

    # ---- logout + login
    r = client.post("/logout")
    check("logout clears session", client.get("/").status_code == 303)
    r = client.post("/login", data={"username": "boss", "password": "wrong"})
    check("bad login rejected", b"Wrong username" in r.content)
    r = client.post("/login", data={"username": "boss",
                                    "password": "longenough"})
    check("good login sets cookie",
          r.status_code == 303 and auth.SESSION_COOKIE in r.cookies)
    check("dashboard after login", client.get("/").status_code == 200)

    # ---- forged cookie rejected
    client.cookies.set(auth.SESSION_COOKIE, "eyJ1aWQiOjF9.forged.garbage")
    check("forged session cookie rejected",
          client.get("/").status_code == 303)

    print(f"CORE FOUNDATION OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
