"""P2 auth — scrypt password hashing + signed session cookies.

Roles (PLAN.md §6 Phase 1): 'admin' (settings, users) and 'operator'
(playlists, playback controls). LAN-only deployment; session cookie is
signed with a per-install secret persisted in the data dir.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "sf_session"
SESSION_MAX_AGE = 12 * 3600  # operators log in per-shift; 12h is plenty

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(password.encode(),
                           salt=bytes.fromhex(salt_hex), **_SCRYPT)
        return hmac.compare_digest(h, bytes.fromhex(hash_hex))
    except (ValueError, TypeError):
        return False


def load_secret(secret_path: str) -> str:
    try:
        with open(secret_path, "r", encoding="ascii") as f:
            secret = f.read().strip()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass
    secret = os.urandom(32).hex()
    with open(secret_path, "w", encoding="ascii") as f:
        f.write(secret)
    return secret


class Sessions:
    def __init__(self, secret: str):
        self._ser = URLSafeTimedSerializer(secret, salt="sf-session")

    def issue(self, user_id: int, role: str) -> str:
        return self._ser.dumps({"uid": user_id, "role": role})

    def read(self, cookie: str | None) -> dict | None:
        if not cookie:
            return None
        try:
            return self._ser.loads(cookie, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None


# ------------------------------------------------------------------- users

def create_user(conn: sqlite3.Connection, username: str, password: str,
                role: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, time.time()))
    return cur.lastrowid


def authenticate(conn: sqlite3.Connection, username: str,
                 password: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?",
                       (username,)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return row
    return None


def any_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
