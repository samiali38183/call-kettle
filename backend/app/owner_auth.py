"""Owner accounts and sessions for the customer portal (/portal).

* One account = one email bound to exactly ONE client. The client always comes from the account/session, never from a URL or form field.
* Passwords: hashlib.scrypt with a random per-user salt, constant-time compare. The plaintext is never stored or logged. A temporary
  password (made by scripts/create_owner_user.py) must be changed at first sign-in.
* Lockout: 5 consecutive failures lock that account for 15 minutes; 20 failures from one address in 15 minutes block that address.
  Both counters live in SQLite (they survive a restart). A locked, unknown or wrongly-passworded account gets the SAME answer.
* Sessions: random 256-bit token in an HttpOnly cookie; only its sha256 is stored. Absolute expiry plus an idle timeout; logout and a
  password change delete them server-side. Each session carries its own CSRF token.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from app import storage

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 200
MAX_FAILS_PER_ACCOUNT = 5
ACCOUNT_LOCK_SECONDS = 15 * 60
MAX_FAILS_PER_IP = 20
IP_WINDOW_SECONDS = 15 * 60
SESSION_ABSOLUTE_SECONDS = 12 * 3600
SESSION_IDLE_SECONDS = 2 * 3600
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


def _now() -> float:          # tests replace this to move the clock
    return time.time()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL_RE.match(email))


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


_dummy_hash: str | None = None


def _burn_time(password: str) -> None:
    """Spend the same work as a real check, so an unknown or locked account is not distinguishable by response time."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


def password_problem(password: str, email: str = "") -> str | None:
    """Why this password is not acceptable, or None. Deliberately simple (length first); no composition theatre."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"Use at least {MIN_PASSWORD_LEN} characters."
    if len(password) > MAX_PASSWORD_LEN:
        return "That password is too long."
    low = password.lower()
    if len(set(password)) < 5:
        return "Use a less repetitive password."
    local = email.split("@")[0].lower()
    if email and (low == email.lower() or (len(local) >= 4 and local in low)):
        return "Do not use your email address in your password."
    return None


def new_temp_password() -> str:
    return secrets.token_urlsafe(12)        # 16 url-safe characters, about 96 bits


def _ip_hash(ip: str) -> str:
    return hashlib.sha256(f"ip:{ip}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- accounts
def create_user(client_id: str, email: str, *, conn=None) -> str:
    """Create an account with a generated temporary password and return that password (the caller shows it once; it is not stored)."""
    email = normalize_email(email)
    if not valid_email(email):
        raise ValueError("That does not look like an email address.")
    temp = new_temp_password()
    try:
        if conn is None:
            with storage._conn() as own_conn:
                own_conn.execute("INSERT INTO owner_users (client_id, email, pw_hash, must_change, created_at) VALUES (?, ?, ?, 1, ?)",
                                 (client_id, email, hash_password(temp), _now()))
        else:
            conn.execute("INSERT INTO owner_users (client_id, email, pw_hash, must_change, created_at) VALUES (?, ?, ?, 1, ?)",
                         (client_id, email, hash_password(temp), _now()))
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise ValueError("An owner account with that email already exists.") from exc
        raise
    return temp


def reset_user(email: str) -> str:
    """New temporary password (forced change), unlock, and sign out everywhere."""
    email = normalize_email(email)
    temp = new_temp_password()
    with storage._conn() as conn:
        row = conn.execute("SELECT id, disabled_at FROM owner_users WHERE email = ?", (email,)).fetchone()
        if not row:
            raise ValueError("No owner account with that email.")
        if row[1] is not None:
            raise ValueError("This owner account is disabled; password recovery cannot reactivate it.")
        conn.execute("UPDATE owner_users SET pw_hash = ?, must_change = 1, failed_count = 0, locked_until = 0 WHERE id = ?",
                     (hash_password(temp), row[0]))
        conn.execute("DELETE FROM owner_sessions WHERE user_id = ?", (row[0],))
    return temp


def set_password(user_id: int, new_password: str) -> None:
    """A user chose a new password: clears the forced change, and signs out every session (the caller then starts a fresh one)."""
    with storage._conn() as conn:
        conn.execute("UPDATE owner_users SET pw_hash = ?, must_change = 0, failed_count = 0, locked_until = 0 WHERE id = ?", (hash_password(new_password), user_id))
        conn.execute("DELETE FROM owner_sessions WHERE user_id = ?", (user_id,))


def _row_to_user(row) -> dict:
    return {"id": row[0], "client_id": row[1], "email": row[2], "pw_hash": row[3], "must_change": bool(row[4]),
            "failed_count": row[5], "locked_until": row[6], "disabled": row[7] is not None}


_USER_COLS = "id, client_id, email, pw_hash, must_change, failed_count, locked_until, disabled_at"


# ---------------------------------------------------------------- login
def ip_blocked(ip: str) -> bool:
    cutoff = _now() - IP_WINDOW_SECONDS
    with storage._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM owner_login_failures WHERE ip_hash = ? AND at > ?", (_ip_hash(ip), cutoff)).fetchone()[0]
    return n >= MAX_FAILS_PER_IP


def _record_ip_failure(ip: str) -> None:
    with storage._conn() as conn:
        conn.execute("INSERT INTO owner_login_failures (at, ip_hash) VALUES (?, ?)", (_now(), _ip_hash(ip)))
        conn.execute("DELETE FROM owner_login_failures WHERE at < ?", (_now() - 24 * 3600,))
        conn.execute("DELETE FROM owner_sessions WHERE expires_at < ?", (_now(),))


def _invitation_expired(conn, user: dict, now: float) -> bool:
    """An issued invitation also bounds that account's matching temporary credential."""
    if not user["must_change"]:
        return False
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_invitations'").fetchone() is None:
        return False  # Existing manually provisioned accounts predate invitation activation.
    row = conn.execute(
        "SELECT MAX(expires_at) FROM owner_invitations WHERE user_id = ? AND password_hash_at_issue = ?",
        (user["id"], user["pw_hash"])).fetchone()
    return row[0] is not None and row[0] <= now


def authenticate(email: str, password: str, ip: str) -> tuple[str, dict | None]:
    """('ok', user) | ('bad', None) | ('limited', None). 'bad' covers unknown email, wrong password, locked and disabled accounts alike."""
    if ip_blocked(ip):
        return "limited", None
    email = normalize_email(email)
    password = password[:MAX_PASSWORD_LEN]
    with storage._conn() as conn:
        row = conn.execute(f"SELECT {_USER_COLS} FROM owner_users WHERE email = ?", (email,)).fetchone()
        user = _row_to_user(row) if row else None
        invitation_expired = user is not None and _invitation_expired(conn, user, _now())
    if user is None or user["disabled"] or user["locked_until"] > _now() or invitation_expired:
        _burn_time(password)
        _record_ip_failure(ip)
        return "bad", None
    if verify_password(password, user["pw_hash"]):
        with storage._conn() as conn:
            now = _now()
            updated = conn.execute(
                "UPDATE owner_users SET failed_count = 0, locked_until = 0, last_login_at = ? "
                "WHERE id = ? AND pw_hash = ? AND disabled_at IS NULL AND locked_until <= ?",
                (now, user["id"], user["pw_hash"], now))
            if updated.rowcount != 1:
                return "bad", None
        return "ok", user
    _record_ip_failure(ip)
    _record_account_failure(user["id"])
    return "bad", None


def _record_account_failure(user_id: int) -> None:
    """Serialize failure counting without holding a write lock during password hashing."""
    with storage._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT failed_count, locked_until, disabled_at FROM owner_users WHERE id = ?", (user_id,)).fetchone()
        now = _now()
        if row is None or row[2] is not None or row[1] > now:
            return
        fails = row[0] + 1
        conn.execute("UPDATE owner_users SET failed_count = ?, locked_until = ? WHERE id = ?",
                     (0 if fails >= MAX_FAILS_PER_ACCOUNT else fails,
                      now + ACCOUNT_LOCK_SECONDS if fails >= MAX_FAILS_PER_ACCOUNT else 0, user_id))


def check_current_password(user_id: int, password: str) -> bool:
    """For the change-password form (the user is already signed in). Failures count toward the account lockout too."""
    with storage._conn() as conn:
        row = conn.execute(f"SELECT {_USER_COLS} FROM owner_users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return False
    user = _row_to_user(row)
    if user["locked_until"] > _now():
        _burn_time(password)
        return False
    ok = verify_password(password[:MAX_PASSWORD_LEN], user["pw_hash"])
    if not ok:
        _record_account_failure(user_id)
    return ok


# ---------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> tuple[str, str]:
    """(cookie token, csrf token). Always a NEW token (no session fixation)."""
    token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(24), _now()
    with storage._conn() as conn:
        conn.execute("INSERT INTO owner_sessions (token_hash, user_id, csrf, created_at, last_seen, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (_token_hash(token), user_id, csrf, now, now, now + SESSION_ABSOLUTE_SECONDS))
    return token, csrf


def get_session(token: str | None) -> dict | None:
    """The signed-in user (with 'csrf') for a cookie value, or None if missing, unknown, expired, idle too long or the account is disabled."""
    if not token or len(token) > 200:
        return None
    now = _now()
    th = _token_hash(token)
    with storage._conn() as conn:
        row = conn.execute(
            "SELECT s.csrf, s.last_seen, s.expires_at, u.id, u.client_id, u.email, u.pw_hash, u.must_change, u.failed_count, u.locked_until, u.disabled_at "
            "FROM owner_sessions s JOIN owner_users u ON u.id = s.user_id WHERE s.token_hash = ?", (th,)).fetchone()
        if not row:
            return None
        csrf, last_seen, expires_at = row[0], row[1], row[2]
        user = _row_to_user(row[3:])
        if expires_at <= now or now - last_seen > SESSION_IDLE_SECONDS or user["disabled"] or _invitation_expired(conn, user, now):
            conn.execute("DELETE FROM owner_sessions WHERE token_hash = ?", (th,))
            return None
        if now - last_seen > 60:
            conn.execute("UPDATE owner_sessions SET last_seen = ? WHERE token_hash = ?", (now, th))
    user["csrf"] = csrf
    user.pop("pw_hash", None)
    return user


def delete_session(token: str | None) -> None:
    if token:
        with storage._conn() as conn:
            conn.execute("DELETE FROM owner_sessions WHERE token_hash = ?", (_token_hash(token),))


def csrf_ok(expected: str | None, supplied: str | None) -> bool:
    return bool(expected) and bool(supplied) and hmac.compare_digest(str(expected), str(supplied))
