"""Passwords, sessions, roles, CSRF, rate limits and encrypted secrets."""
import hashlib
import hmac
import os
import secrets as pysecrets
import time
from functools import wraps

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import DATA_DIR

ROLES = {
    "owner": "Owner: everything, including settings, people and backups",
    "editor": "Editor: sources, approving and publishing, tips",
    "reviewer": "Reviewer: can edit drafts and tick checklists, but not publish",
}
RANK = {"reviewer": 1, "editor": 2, "owner": 3}


# ── passwords ─────────────────────────────────────────────
def hash_password(pw):
    return generate_password_hash(pw, method="scrypt")


def check_password(hash_, pw):
    return check_password_hash(hash_, pw)


def password_problem(pw):
    if len(pw or "") < 10:
        return "Use at least 10 characters."
    return None


# ── encryption for API keys and passwords stored in the database ──
_KEY_FILE = DATA_DIR / "secret.key"


def _fernet():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _KEY_FILE.exists():
        _KEY_FILE.write_bytes(Fernet.generate_key())
        os.chmod(_KEY_FILE, 0o600)
    return Fernet(_KEY_FILE.read_bytes())


def set_secret(db, key, value):
    if value is None or value == "":
        db.run("DELETE FROM secrets WHERE key=?", (key,))
        return
    db.run("INSERT INTO secrets(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           (key, _fernet().encrypt(value.encode())))


def get_secret(db, key):
    r = db.one("SELECT value FROM secrets WHERE key=?", (key,))
    if not r:
        return ""
    try:
        return _fernet().decrypt(r["value"]).decode()
    except InvalidToken:
        return ""


def has_secret(db, key):
    return bool(db.val("SELECT 1 FROM secrets WHERE key=?", (key,)))


def flask_secret_key():
    """Session signing key, kept beside the encryption key."""
    p = DATA_DIR / "session.key"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(pysecrets.token_bytes(32))
        os.chmod(p, 0o600)
    return p.read_bytes()


# ── privacy-preserving visitor fingerprint (for spam limits, never shown) ──
def ip_hash():
    ip = request.remote_addr or ""  # the real visitor address (ProxyFix trusts only our own proxy)
    return hmac.new(flask_secret_key(), ip.encode(), hashlib.sha256).hexdigest()[:24]


def rate_limited(db, bucket, limit, window_s):
    """True if `bucket` has had >= limit hits in the window; records this hit otherwise."""
    t = time.time()
    db.run("DELETE FROM rate WHERE at < ?", (t - 86400,))
    n = db.val("SELECT COUNT(*) FROM rate WHERE bucket=? AND at > ?", (bucket, t - window_s))
    if n >= limit:
        return True
    db.run("INSERT INTO rate(bucket,at) VALUES(?,?)", (bucket, t))
    return False


# ── CSRF ──────────────────────────────────────────────────
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = pysecrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method in ("POST", "PUT", "DELETE"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "This form expired. Go back, refresh the page and try again.")


# ── login / roles ─────────────────────────────────────────
def login_required(role="reviewer"):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = g.get("user")
            if not u:
                return redirect(url_for("admin.login", next=request.full_path))
            if RANK.get(u["role"], 0) < RANK[role]:
                abort(403, "Your role doesn't allow this. Ask the owner.")
            return fn(*a, **kw)
        return wrapper
    return deco


def can(role):
    u = g.get("user")
    return bool(u) and RANK.get(u["role"], 0) >= RANK[role]
