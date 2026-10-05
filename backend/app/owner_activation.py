"""Sales-led portal activation. Invitation secrets are shown once, never persisted.

Initialize after storage.init_db(); include router in the application. Publication
can create invitations on its own connection so account/intake/invitation commit
 together. No phone provisioning, tenant creation, email delivery or auto-login.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from app import brand, owner_auth, portal, storage

router = APIRouter()
INVITATION_SECONDS = 3600
_SCHEMA = """CREATE TABLE IF NOT EXISTS owner_invitations (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES owner_users(id) ON DELETE CASCADE,
    password_hash_at_issue TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
)"""


def init_db(*, conn=None) -> None:
    """Additive, idempotent migration; may join the caller's transaction."""
    if conn is None:
        with storage._conn() as own:
            init_db(conn=own)
    else:
        conn.execute(_SCHEMA)


def create_invitation(user_id: int, *, conn=None) -> str:
    """Issue a one-hour invitation for an existing account; return secret once."""
    if conn is None:
        with storage._conn() as own:
            own.execute("BEGIN IMMEDIATE")
            return create_invitation(user_id, conn=own)
    init_db(conn=conn)
    row = conn.execute("SELECT pw_hash FROM owner_users WHERE id=? AND disabled_at IS NULL AND must_change=1", (user_id,)).fetchone()
    if row is None:
        raise ValueError("No pending enabled owner account.")
    token = secrets.token_urlsafe(32)
    now = owner_auth._now()
    conn.execute("INSERT INTO owner_invitations VALUES (?, ?, ?, ?, ?, NULL)",
                 (hashlib.sha256(token.encode()).hexdigest(), user_id, row[0], now, now + INVITATION_SECONDS))
    return token


CSRF_COOKIE = "ck_activation"
INVALID = "This invitation is unavailable. Contact support for account help."


def _lookup(conn, token: str):
    if not token or len(token) > 200:
        return None
    return conn.execute(
        "SELECT u.id, u.email FROM owner_invitations i JOIN owner_users u ON u.id=i.user_id "
        "WHERE i.token_hash=? AND i.consumed_at IS NULL AND i.expires_at>? "
        "AND u.disabled_at IS NULL AND u.must_change=1 AND u.pw_hash=i.password_hash_at_issue",
        (hashlib.sha256(token.encode()).hexdigest(), owner_auth._now())).fetchone()


def _csrf(cookie: str, token: str) -> str:
    return hashlib.sha256((cookie + ":" + token).encode()).hexdigest()


def _page(token: str, csrf: str, error: str = "", status: int = 200):
    body = (f'<h1>Activate your {escape(brand.get().name)} account</h1><p class="err">{escape(error)}</p>'
            '<p>Choose your owner portal password. This does not turn on phone service.</p>'
            '<form method="post" action="/portal/activate">'
            f'<input type="hidden" name="token" value="{escape(token)}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
            '<label>New password<input type="password" name="new" autocomplete="new-password" required></label>'
            '<label>Confirm password<input type="password" name="new2" autocomplete="new-password" required></label>'
            '<button type="submit">Activate account</button></form>')
    return portal._html(portal._shell(f"Activate account - {brand.get().name}", body, footer=portal._help_line()), status)


def _unavailable():
    return portal._html(portal._shell("Invitation unavailable", f'<p>{INVALID}</p>', footer=portal._help_line()), 400)


@router.get("/portal/activate")
def activate_get(request: Request, token: str = ""):
    with storage._conn() as conn:
        init_db(conn=conn)
        row = _lookup(conn, token)
    if row is None:
        return _unavailable()
    cookie = secrets.token_urlsafe(32)
    response = _page(token, _csrf(cookie, token))
    response.set_cookie(CSRF_COOKIE, cookie, max_age=INVITATION_SECONDS, httponly=True,
                        secure=portal._secure(request), samesite="strict", path="/portal/activate")
    return response


@router.post("/portal/activate")
async def activate_post(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    password = str(form.get("new", ""))
    cookie = request.cookies.get(CSRF_COOKIE, "")
    origin = request.headers.get("origin")
    if (not cookie or len(cookie) > 200 or len(token) > 200
            or not owner_auth.csrf_ok(_csrf(cookie, token), str(form.get("csrf", "")))
            or (origin is not None and origin != str(request.base_url).rstrip("/"))):
        return portal._html(portal._shell("Not authorized", "<p>Reload the invitation and try again.</p>"), 403)
    try:
        with storage._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            init_db(conn=conn)
            row = _lookup(conn, token)
            if row is None:
                return _unavailable()
            problem = owner_auth.password_problem(password, row[1])
            if password != str(form.get("new2", "")):
                problem = "The passwords do not match."
            if problem:
                return _page(token, _csrf(cookie, token), problem, 422)
            # Do not call set_password(): it opens a separate transaction. The
            # credential change, session revocation and consumption must commit
            # together under this write lock, including simultaneous submissions.
            conn.execute("UPDATE owner_users SET pw_hash=?, must_change=0, failed_count=0, locked_until=0 WHERE id=?",
                         (owner_auth.hash_password(password), row[0]))
            conn.execute("DELETE FROM owner_sessions WHERE user_id=?", (row[0],))
            conn.execute("UPDATE owner_invitations SET consumed_at=? WHERE token_hash=?",
                         (owner_auth._now(), hashlib.sha256(token.encode()).hexdigest()))
    except (sqlite3.Error, OSError):
        return portal._html(portal._shell("Try again", "<p>Account activation could not be completed. Try again or contact support.</p>",
                                          footer=portal._help_line()), 503)
    return RedirectResponse("/portal/login", status_code=303,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": portal.CSP})
