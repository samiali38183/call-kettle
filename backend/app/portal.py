"""The customer-facing owner portal: sign in, then see only your own business's calls, bookings and settings.

Isolation rule: the client id is read from the signed-in account's server-side session and from nothing else. No URL or form field
chooses a client, so there is nothing for a signed-in owner to tamper with to reach another business. Every query below is
parameterised with `user["client_id"]`.

Plain server-rendered HTML, no JavaScript, no external requests, no tracking. Everything dynamic goes through html.escape.
The legacy keyed /report/<id>?key= link is untouched (see main.py).
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import re
import secrets
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr
from html import escape as _h
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import brand, outcomes, owner_auth, storage
from app.config import ClientConfig, ClientNotFoundError, load_client_config

router = APIRouter()

SESSION_COOKIE = "ck_portal"
LOGIN_CSRF_COOKIE = "ck_login"
CALLS_PER_PAGE = 100
NO_DATA = "No data"
GENERIC_LOGIN_ERROR = "That email and password did not match. Check them and try again."
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
# Shown on every page of a FICTIONAL sales-demo business (config portal_sample, scripts/seed_demo_portal.py). Such a portal is read-only.
SAMPLE_BANNER = ('<p class="sample-banner" role="note" style="background:#FFF4CC;border:2px solid #E0B000;border-radius:8px;padding:8px 12px;'
                 'margin:10px 0 0;font-weight:700;color:#5A4500">Sample business - demo data. Every call, booking, name and phone number here '
                 'is made up to show what an owner sees. Read-only.</p>')
SAMPLE_READ_ONLY = "This is a sample business with made-up demo data. Nothing here can be changed."

_STYLE = """
body{font-family:-apple-system,Segoe UI,sans-serif;max-width:960px;margin:0 auto;padding:0 14px 40px;color:#20293A;background:#F9FAFA;line-height:1.4}
h1{font-size:22px;margin:18px 0 4px} h2{font-size:15px;margin:26px 0 8px;text-transform:uppercase;letter-spacing:.06em;color:#5B6472}
nav{display:flex;flex-wrap:wrap;gap:4px 14px;padding:12px 0;border-bottom:1px solid #DEE3E8;font-size:15px;align-items:center}
nav a{color:#2F6FED;text-decoration:none;padding:4px 0} nav a.on{font-weight:700;color:#20293A;border-bottom:2px solid #2F6FED}
nav form{margin:0 0 0 auto} .sub{color:#5B6472;font-size:13px;margin:0 0 14px} .empty{color:#8A93A3}
table{width:100%;border-collapse:collapse;font-size:14px;background:#fff;border:1px solid #DEE3E8}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #EEF1F5;vertical-align:top}
th{color:#5B6472;font-size:11px;text-transform:uppercase}
.wrap{overflow-x:auto}
.stats{display:flex;gap:10px;flex-wrap:wrap}.stat{background:#fff;border:1px solid #DEE3E8;border-radius:8px;padding:10px 16px;min-width:110px}.stat b{display:block;font-size:22px}
.call{background:#fff;border:1px solid #DEE3E8;border-radius:8px;padding:12px 14px;margin-bottom:8px;font-size:14px}.call p{margin:6px 0}
.meta{font-size:13px;color:#5B6472}.tag{background:#E4F5EF;color:#0F7B63;border-radius:10px;padding:1px 8px;font-size:12px}
.tag.warn{background:#FCEBD2;color:#8A4B00}.attn{border-left:4px solid #E08A00}
details{margin-top:6px;font-size:13px}summary{cursor:pointer;color:#2F6FED}
a{color:#2F6FED}
button,.btn{font:inherit;font-size:14px;padding:7px 14px;border:1px solid #2F6FED;background:#fff;color:#2F6FED;border-radius:6px;cursor:pointer}
button.primary{background:#2F6FED;color:#fff}
input,select{font:inherit;font-size:16px;padding:8px;border:1px solid #C5CCD6;border-radius:6px;max-width:100%;box-sizing:border-box}
label{display:block;font-size:13px;color:#5B6472;margin:10px 0 3px}
.card{background:#fff;border:1px solid #DEE3E8;border-radius:8px;padding:6px 18px 16px}
.err{color:#8A2620;font-weight:600}.ok{color:#0F7B63}
.cal td{height:78px;width:14.28%;font-size:12px;padding:4px;border:1px solid #EEF1F5}.cal .d{font-weight:700;color:#5B6472}
.cal .out{background:#F3F5F7;color:#8A93A3}.cal .today{background:#EAF1FF}.cal .ev{display:block;margin-top:2px;background:#E4F5EF;border-radius:4px;padding:1px 4px;overflow:hidden}
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:12px}.filters label{margin:0 0 3px}
.btn{display:inline-block;text-decoration:none;box-sizing:border-box}.btn.call-back{background:#0F7B63;border-color:#0F7B63;color:#fff;font-weight:600}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.actions form{margin:0}
.welcome{background:#EAF1FF;border:1px solid #C9DBFF;border-radius:8px;padding:6px 18px 14px;margin:14px 0}.welcome h2{margin-top:14px}
.help{margin-top:34px;padding:12px 14px;border-top:1px solid #DEE3E8;font-size:14px;color:#5B6472}
.when{font-weight:700}.who{font-size:15px}.svc{color:#5B6472}
@media(max-width:600px){
body{font-size:16px;padding:0 12px 40px}h1{font-size:20px}
.cal td{height:56px;font-size:10px}.cal .ev{white-space:nowrap;text-overflow:ellipsis}
nav a{padding:10px 4px}nav form{margin:0}
button,.btn{min-height:44px;padding:10px 16px}.actions .btn,.actions button{flex:1 1 140px;text-align:center}
.stat{flex:1 1 40%}.filters>div,.filters select,.filters input{width:100%}
}
"""


# ------------------------------------------------------------------ helpers
def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("fly-client-ip") or request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _html(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer"})


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


def _shell(title: str, body: str, *, nav: str = "", footer: str = "") -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{_h(title)}</title><style>{_STYLE}</style></head><body>{nav}{body}{footer}</body></html>')


def _fmt_phone(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else (number or "")


def _fmt_utc(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso


def _fmt_slot(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M").strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso


def _fmt_time(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return iso


def _class_label(outcome_class: str | None, outcome: str | None = None) -> str:
    if outcome_class:
        return outcomes.LABELS.get(outcome_class, outcome_class.replace("_", " ").title())
    return (outcome or "Not classified yet").replace("_", " ").capitalize()


def _tel(number: str | None) -> str:
    """A safe tel: href for a caller's number, or "" when it has no digits (so nothing odd ever becomes a link)."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10:
        digits = "1" + digits
    return f"tel:+{digits}" if 10 <= len(digits) <= 15 else ""


def _caller_link(number: str | None) -> str:
    shown = _h(_fmt_phone(number) or "unknown number")
    href = _tel(number)
    return f'<a href="{_h(href)}">{shown}</a>' if href else shown


def _call_back_button(number: str | None, verb: str = "Call back") -> str:
    href = _tel(number)
    return f'<a class="btn call-back" href="{_h(href)}">{_h(verb)} {_h(_fmt_phone(number))}</a>' if href else ""


def _support_email() -> str:
    """The bare address: the configured sender can be written as "Name <addr>", which is not a valid mailto target."""
    return parseaddr(brand.get().support_email)[1] or brand.get().support_email


def _help_line() -> str:
    b = brand.get()
    return (f'<div class="help">Need a hand? Call or text {_h(b.contact_name)} at <a href="{_h(_tel(b.support_phone))}">{_h(b.support_phone)}</a>'
            f' or email <a href="mailto:{_h(_support_email())}">{_h(_support_email())}</a>. <a href="/portal/help">More help</a></div>')


def _csv_cell(value) -> str:
    """Neutralise spreadsheet formula injection: a caller's name or words must never run as a formula when the owner opens the file."""
    text = "" if value is None else str(value).replace("\r", " ").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t") else text


def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow([_csv_cell(c) for c in row])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _session_user(request: Request) -> dict | None:
    return owner_auth.get_session(request.cookies.get(SESSION_COOKIE))


def _config_for(user: dict) -> ClientConfig | None:
    try:
        return load_client_config(user["client_id"])
    except (ClientNotFoundError, Exception):
        return None


class _Ctx:
    """The signed-in owner, their client config and local timezone, resolved once per request."""

    def __init__(self, user: dict, config: ClientConfig):
        self.user, self.config, self.tz = user, config, ZoneInfo(config.timezone)
        self.client_id = user["client_id"]
        self.csrf = user["csrf"]
        self.sample = bool(config.portal_sample)        # fictional demo business: banner on every page, read-only

    def csrf_field(self) -> str:
        return f'<input type="hidden" name="csrf" value="{_h(self.csrf)}">'

    def page(self, title: str, body: str) -> HTMLResponse:
        return _html(_shell(f"{self.config.business_name} - {title}", body, footer=_help_line()))

    def nav(self, active: str) -> str:
        items = [("overview", "Home"), ("frontdesk", "Front desk"), ("calendar", "Bookings"), ("calls", "Calls"), ("settings", "Setup"), ("help", "Help")]
        links = "".join(f'<a href="/portal/{k}"{" class=on" if k == active else ""}>{label}</a>' for k, label in items)
        return ((SAMPLE_BANNER if self.sample else "") +
                f'<nav>{links}<form method="post" action="/portal/logout">{self.csrf_field()}<button type="submit">Sign out</button></form></nav>'
                f'<h1>{_h(self.config.business_name)}</h1>')


def _require(request: Request, *, allow_must_change: bool = False) -> _Ctx | Response:
    user = _session_user(request)
    if user is None:
        resp = _redirect("/portal/login")
        if request.cookies.get(SESSION_COOKIE):
            resp.delete_cookie(SESSION_COOKIE, path="/portal")
        return resp
    config = _config_for(user)
    if config is None:                              # the business was offboarded: the account must not keep working
        owner_auth.delete_session(request.cookies.get(SESSION_COOKIE))
        resp = _redirect("/portal/login")
        resp.delete_cookie(SESSION_COOKIE, path="/portal")
        return resp
    if user["must_change"] and not allow_must_change:
        return _redirect("/portal/password")
    return _Ctx(user, config)


async def _post_form(request: Request, ctx: _Ctx) -> dict | Response:
    """Form fields of a POST, after the CSRF token has been checked against the session's own."""
    form = await request.form()
    if not owner_auth.csrf_ok(ctx.csrf, str(form.get("csrf", ""))):
        return _html(_shell("Session expired", '<p>That request could not be verified. <a href="/portal/overview">Go back</a> and try again.</p>'), 403)
    return {k: str(v) for k, v in form.items()}


def _set_session_cookie(resp: Response, request: Request, token: str) -> None:
    resp.set_cookie(SESSION_COOKIE, token, max_age=owner_auth.SESSION_ABSOLUTE_SECONDS, httponly=True, secure=_secure(request), samesite="lax", path="/portal")


# ------------------------------------------------------------------ sign in / out / password
def _login_page(request: Request, message: str = "", status: int = 200) -> HTMLResponse:
    token = secrets.token_urlsafe(24)
    br = brand.get()
    msg = f'<p class="err" role="alert">{_h(message)}</p>' if message else ""
    body = (f'<div style="max-width:420px;margin:30px auto 0"><h1>{_h(br.name)}</h1><p class="sub">Sign in to see your calls, bookings and how your receptionist is set up.</p>{msg}'
            f'<form method="post" action="/portal/login" class="card"><input type="hidden" name="csrf" value="{_h(token)}">'
            '<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required autofocus style="width:100%">'
            '<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" required style="width:100%">'
            '<p><button class="primary" type="submit" style="width:100%">Sign in</button></p></form>'
            f'<p class="sub">First time? Open the activation link {_h(br.contact_name)} gave you to choose your password. If you were given a temporary password instead, sign in above and change it on the next screen.<br>'
            '<a href="/start">Request setup for your business</a>. A setup request does not turn on phone service; we review your rules and test the line before forwarding calls.<br>'
            f'Forgot your password or locked out? Call or text {_h(br.contact_name)} at <a href="{_h(_tel(br.support_phone))}">{_h(br.support_phone)}</a> and we will reset it.</p></div>')
    resp = _html(_shell("Sign in", body), status)
    resp.set_cookie(LOGIN_CSRF_COOKIE, token, max_age=3600, httponly=True, secure=_secure(request), samesite="lax", path="/portal")
    return resp


@router.get("/portal", response_class=HTMLResponse)
def portal_home(request: Request) -> Response:
    return _redirect("/portal/overview")


@router.get("/portal/login", response_class=HTMLResponse)
def portal_login_page(request: Request) -> Response:
    if _session_user(request):
        return _redirect("/portal/overview")
    return _login_page(request)


@router.post("/portal/login")
async def portal_login(request: Request) -> Response:
    form = await request.form()
    if not owner_auth.csrf_ok(request.cookies.get(LOGIN_CSRF_COOKIE), str(form.get("csrf", ""))):
        return _login_page(request, "Your sign-in page expired. Please try again.", 403)
    status, user = owner_auth.authenticate(str(form.get("email", "")), str(form.get("password", "")), _client_ip(request))
    if status == "limited":
        return _login_page(request, "Too many attempts. Please wait a few minutes and try again.", 429)
    if status != "ok" or user is None or _config_for(user) is None:
        return _login_page(request, GENERIC_LOGIN_ERROR, 200)
    token, _csrf = owner_auth.create_session(user["id"])
    resp = _redirect("/portal/password" if user["must_change"] else "/portal/overview")
    _set_session_cookie(resp, request, token)
    resp.delete_cookie(LOGIN_CSRF_COOKIE, path="/portal")
    return resp


@router.post("/portal/logout")
async def portal_logout(request: Request) -> Response:
    user = _session_user(request)
    if user is not None:
        form = await request.form()
        if owner_auth.csrf_ok(user["csrf"], str(form.get("csrf", ""))):
            owner_auth.delete_session(request.cookies.get(SESSION_COOKIE))
        else:
            return _html(_shell("Session expired", '<p>That request could not be verified. <a href="/portal/overview">Go back</a>.</p>'), 403)
    resp = _redirect("/portal/login")
    resp.delete_cookie(SESSION_COOKIE, path="/portal")
    return resp


def _password_page(ctx: _Ctx, message: str = "", status: int = 200) -> HTMLResponse:
    if ctx.sample:                                     # the shared demo login must never be lockable by whoever holds the phone
        return _html(_shell("Change password", f'{SAMPLE_BANNER}<p>{_h(SAMPLE_READ_ONLY)}</p><p><a href="/portal/overview">Back</a></p>'), 403 if message else 200)
    intro = ("Welcome! You are using a temporary password. Choose your own to continue; it takes a few seconds." if ctx.user["must_change"]
             else "Choose a new password. Other phones and computers are signed out when you save.")
    msg = f'<p class="err" role="alert">{_h(message)}</p>' if message else ""
    body = (f'<div style="max-width:460px;margin:30px auto 0"><h1>Change password</h1><p class="sub">{_h(intro)}</p>{msg}'
            f'<form method="post" action="/portal/password" class="card">{ctx.csrf_field()}'
            f'<label for="cur">{"Temporary password" if ctx.user["must_change"] else "Current password"}</label><input id="cur" name="current" type="password" autocomplete="current-password" required style="width:100%">'
            f'<label for="new">New password (at least {owner_auth.MIN_PASSWORD_LEN} characters; a few random words works well)</label><input id="new" name="new" type="password" autocomplete="new-password" required style="width:100%">'
            '<label for="new2">New password again</label><input id="new2" name="new2" type="password" autocomplete="new-password" required style="width:100%">'
            '<p><button class="primary" type="submit" style="width:100%">Save password</button></p></form>'
            f'<p class="sub">Stuck? Call or text {_h(brand.get().contact_name)} at <a href="{_h(_tel(brand.get().support_phone))}">{_h(brand.get().support_phone)}</a>.</p></div>')
    return _html(_shell("Change password", body), status)


@router.get("/portal/password", response_class=HTMLResponse)
def portal_password_page(request: Request) -> Response:
    ctx = _require(request, allow_must_change=True)
    return ctx if isinstance(ctx, Response) else _password_page(ctx)


@router.post("/portal/password")
async def portal_password(request: Request) -> Response:
    ctx = _require(request, allow_must_change=True)
    if isinstance(ctx, Response):
        return ctx
    if ctx.sample:
        return _password_page(ctx, SAMPLE_READ_ONLY, 403)
    form = await _post_form(request, ctx)
    if isinstance(form, Response):
        return form
    new = form.get("new", "")
    if not owner_auth.check_current_password(ctx.user["id"], form.get("current", "")):
        return _password_page(ctx, "The current password was not right.", 200)
    if new != form.get("new2", ""):
        return _password_page(ctx, "The two new passwords did not match.", 200)
    problem = owner_auth.password_problem(new, ctx.user["email"])
    if problem:
        return _password_page(ctx, problem, 200)
    if new == form.get("current", ""):
        return _password_page(ctx, "Choose a password different from the current one.", 200)
    owner_auth.set_password(ctx.user["id"], new)           # signs out every session, including this one
    token, _csrf = owner_auth.create_session(ctx.user["id"])
    resp = _redirect("/portal/overview")
    _set_session_cookie(resp, request, token)
    return resp


# ------------------------------------------------------------------ overview
def _local_boundaries(tz: ZoneInfo) -> tuple[datetime, datetime]:
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=now.weekday()), midnight.replace(day=1)


def _parse_started(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _outcome_table(counts: Counter, empty: str) -> str:
    if not counts:
        return f'<p class="empty">{_h(empty)}</p>'
    rows = "".join(f"<tr><td>{_h(label)}</td><td>{n}</td></tr>" for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return f'<div class="wrap"><table><tr><th>Outcome</th><th>Calls</th></tr>{rows}<tr><th>Total</th><th>{sum(counts.values())}</th></tr></table></div>'


def waiting_label(started_iso: str | None, now: datetime | None = None) -> str:
    """'Waiting 3 days' for a callback, from the recorded call start. '' if the time is unreadable."""
    started = _parse_started(started_iso) if started_iso else None
    if started is None:
        return ""
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - started).total_seconds()))
    if seconds < 60:
        return "Waiting under a minute"
    if seconds < 3600:
        return f"Waiting {seconds // 60} min"
    if seconds < 86400:
        hours = seconds // 3600
        return f"Waiting {hours} hour" + ("" if hours == 1 else "s")
    days = seconds // 86400
    return f"Waiting {days} day" + ("" if days == 1 else "s")


OVERDUE_HOURS = 24


def _overdue(started_iso: str | None) -> bool:
    started = _parse_started(started_iso) if started_iso else None
    return started is not None and (datetime.now(timezone.utc) - started) >= timedelta(hours=OVERDUE_HOURS)


def _waiting_html(started_iso: str | None) -> str:
    label = waiting_label(started_iso)
    if not label:
        return ""
    tag = ' <span class="tag warn">Overdue</span>' if _overdue(started_iso) else ""
    return f'<p class="sub">{_h(label)}{tag}</p>'


HOURS_DAYS = 7


def _hours_html(conn, ctx: "_Ctx") -> str:
    """Business hours vs after hours for the last 7 days, from the client's own configured hours. Recorded counts only: no dollar figure."""
    from app import callhours

    since = (datetime.now(timezone.utc) - timedelta(days=HOURS_DAYS)).isoformat()
    rows = conn.execute("SELECT started_at, outcome_class, needs_attention, attention_resolved_at FROM calls WHERE client_id = ? AND started_at >= ?",
                        (ctx.client_id, since)).fetchall()
    intro = (f'<h2 id="hours">When your calls came in</h2><p class="sub">Last {HOURS_DAYS} days, using the business hours in Setup. '
             'These are counts of recorded calls, not dollars: a booking or a message is not revenue.</p>')
    split = callhours.split(ctx.config, rows)
    if not (split["business"]["calls"] or split["after"]["calls"]):
        return intro + f'<p class="empty">No calls recorded in the last {HOURS_DAYS} days.</p>'
    def row(label: str, d: dict) -> str:
        return (f'<tr><td>{_h(label)}</td><td>{d["calls"]}</td><td>{d["booked"]}</td><td>{d["left_details"]}</td>'
                f'<td>{d["hung_up"]}</td><td>{d["waiting"]}</td></tr>')
    return (intro + '<div class="wrap"><table><tr><th>When</th><th>Calls</th><th>Booked</th><th>Left details or asked for a call back</th>'
            '<th>Hung up</th><th>Still waiting on you</th></tr>' + row("Business hours", split["business"]) + row("After hours", split["after"]) + '</table></div>')


def _marked_spam_html(ctx: "_Ctx") -> str:
    from app import spamtag

    marked = spamtag.marked_list(ctx.client_id)
    if not marked:
        return ""
    items = "".join(
        f'<li>{_h(_fmt_phone(number) or "unknown number")} &middot; marked {_h(_fmt_utc(at, ctx.tz))} '
        f'<form method="post" action="/portal/spam/undo" style="display:inline">{ctx.csrf_field()}<input type="hidden" name="call" value="{_h(sid)}">'
        f'<button type="submit"{" disabled" if ctx.sample else ""}>Undo</button></form></li>' for number, at, sid in marked)
    return ('<h2 id="spam">Marked as not a customer</h2><p class="sub">Calls from these numbers do not appear under Needs your attention and do not text or email you, '
            'unless the call sounds like an emergency. Callers are not blocked and are not contacted.</p>' f'<ul>{items}</ul>')


_ATTENTION_HINT = {
    "CALLBACK_REQUESTED": "Asked for a call back.",
    "LEAD_CAPTURED": "A new customer left their details.",
    "TRANSFER_FAILED": "We tried to put this caller through to you and could not reach you.",
    "AFTER_HOURS_MESSAGE": "Left a message after hours.",
    "AI_FAILURE": "The receptionist hit a problem on this call. Please check in with this caller.",
    "EMERGENCY_ESCALATED": "May be an emergency. Call them first.",
}


RECENT_WEEKS = 8


def _recent_weeks_html(conn, ctx: _Ctx, week_start: datetime) -> str:
    """Weekly trend of RECORDED facts only (no income or ROI estimate): calls, booked, needed a person."""
    first = week_start - timedelta(weeks=RECENT_WEEKS - 1)
    rows = conn.execute("SELECT started_at, outcome_class, needs_attention FROM calls WHERE client_id = ? AND started_at >= ?",
                        (ctx.client_id, first.astimezone(timezone.utc).isoformat())).fetchall()
    buckets = [[0, 0, 0] for _ in range(RECENT_WEEKS)]
    for started, cls, needs in rows:
        dt = _parse_started(started)
        if dt is None:
            continue
        index = (dt.astimezone(ctx.tz) - first).days // 7
        if 0 <= index < RECENT_WEEKS:
            buckets[index][0] += 1
            buckets[index][1] += 1 if cls == "BOOKED" else 0
            buckets[index][2] += 1 if needs else 0
    if not any(b[0] for b in buckets):
        return f'<p class="empty">No calls recorded in the last {RECENT_WEEKS} weeks.</p>'
    body = "".join(
        f'<tr data-week="{RECENT_WEEKS - 1 - i}"><td>{_h((first + timedelta(weeks=i)).strftime("%b %d").replace(" 0", " "))}</td>'
        f'<td>{b[0]}</td><td>{b[1]}</td><td>{b[2]}</td></tr>' for i, b in reversed(list(enumerate(buckets))))
    return ('<div class="wrap"><table><tr><th>Week of</th><th>Calls</th><th>Booked</th><th>Needed a person</th></tr>'
            f'{body}</table></div>')


def _upcoming_bookings(ctx: _Ctx, limit: int) -> list[tuple]:
    """Confirmed bookings from this moment on, soonest first. Slot times are stored in the business's local time."""
    now_local = datetime.now(ctx.tz).strftime("%Y-%m-%dT%H:%M")
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        return conn.execute("SELECT caller_name, caller_phone, service, slot_start FROM bookings WHERE client_id = ? AND status = 'confirmed' AND slot_start >= ? "
                            "ORDER BY slot_start LIMIT ?", (ctx.client_id, now_local, limit)).fetchall()
    finally:
        conn.close()


def _upcoming_html(rows: list[tuple], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{_h(empty)}</p>'
    return "".join(
        f'<div class="call"><div class="when">{_h(_fmt_slot(slot))}</div><div class="who">{_h(name)} &middot; <span class="svc">{_h(service)}</span></div>'
        f'<div class="actions">{_call_back_button(phone, "Call")}</div></div>'
        for name, phone, service, slot in rows)


@router.get("/portal/overview", response_class=HTMLResponse)
def portal_overview(request: Request) -> Response:
    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    week_start, month_start = _local_boundaries(ctx.tz)
    since = min(week_start, month_start).astimezone(timezone.utc).isoformat()
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        rows = conn.execute("SELECT started_at, outcome_class, outcome FROM calls WHERE client_id = ? AND started_at >= ?", (ctx.client_id, since)).fetchall()
        attention_total = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE client_id = ? AND needs_attention = 1 AND attention_resolved_at IS NULL",
            (ctx.client_id,)).fetchone()[0]
        attention = conn.execute(
            "SELECT call_sid, from_number, started_at, outcome_class, summary FROM calls "
            "WHERE client_id = ? AND needs_attention = 1 AND attention_resolved_at IS NULL ORDER BY started_at ASC, call_sid ASC LIMIT 50", (ctx.client_id,)).fetchall()
        ever_called = conn.execute("SELECT 1 FROM calls WHERE client_id = ? LIMIT 1", (ctx.client_id,)).fetchone() is not None
        recent_weeks = _recent_weeks_html(conn, ctx, week_start)
        hours_html = _hours_html(conn, ctx)
    finally:
        conn.close()
    week, month = Counter(), Counter()
    for started, cls, outcome in rows:
        dt = _parse_started(started)
        if dt is None:
            continue
        label = _class_label(cls, outcome)
        if dt >= month_start:
            month[label] += 1
        if dt >= week_start:
            week[label] += 1
    from app import spamtag

    attn_html = "".join(
        f'<div class="call attn"><div class="meta"><b>{_h(_fmt_utc(started, ctx.tz))}</b> &middot; {_caller_link(from_number)} &middot; '
        f'<span class="tag warn">{_h(outcomes.LABELS.get(cls or "", "Needs a look"))}</span></div>'
        f'<p><b>{_h(_ATTENTION_HINT.get(cls or "", "Please take a look."))}</b></p>{_waiting_html(started)}'
        f'<p>{_h(text) if (text and ctx.config.record_transcripts) else "Open Calls for details."}</p>'
        f'<div class="actions">{_call_back_button(from_number)}'
        f'<form method="post" action="/portal/handled">{ctx.csrf_field()}<input type="hidden" name="call" value="{_h(sid)}">'
        f'<button type="submit"{" disabled" if ctx.sample else ""}>Mark handled</button></form>'
        + (f'<form method="post" action="/portal/spam">{ctx.csrf_field()}<input type="hidden" name="call" value="{_h(sid)}">'
           f'<button type="submit"{" disabled" if ctx.sample else ""}>Not a customer (spam)</button></form>' if spamtag.enabled() else '')
        + '</div></div>'
        for sid, from_number, started, cls, text in attention
    ) or '<p class="empty">Nothing waiting on you. When a caller needs a call back, they will show up here.</p>'
    welcome = "" if ever_called else (
        '<div class="welcome"><h2>No calls yet. Here is what happens next</h2>'
        '<p>As soon as the first call comes in, it appears on this page shortly after the call ends.</p>'
        '<ul><li>The receptionist answers, says it is an AI, and asks what the caller needs.</li>'
        '<li>It books an appointment when it can, or takes a name, number and reason.</li>'
        '<li>Anyone who needs a person shows up under <b>Needs your attention</b> with a one-tap <b>Call back</b> button.</li></ul>'
        '<p>Not seeing calls? Check that call forwarding is on (see <a href="/portal/settings">Setup</a>), then call your business number from another phone to test it.</p></div>')
    upcoming = _upcoming_bookings(ctx, 5)
    body = (f'{ctx.nav("overview")}<p class="sub">Times shown in {_h(ctx.config.timezone)}. Counts are what the system recorded; nothing here estimates income.</p>{welcome}'
            f'<h2 id="attention">Needs your attention ({attention_total})</h2>'
            + (f'<p class="sub">Showing the oldest 50 of {attention_total}. <a href="/portal/calls">See full call history</a>.</p>' if attention_total > len(attention) else '')
            + attn_html +
            _marked_spam_html(ctx) + hours_html +
            f'<h2>Upcoming bookings</h2>{_upcoming_html(upcoming, "No upcoming bookings yet. When the receptionist books a visit, it shows up here.")}'
            + ('<p><a href="/portal/calendar">See all bookings &raquo;</a></p>' if upcoming else '') +
            f'<h2>This week (since {_h(week_start.strftime("%a %b %d").replace(" 0", " "))})</h2>{_outcome_table(week, "No calls recorded this week.")}'
            f'<h2>This month ({_h(month_start.strftime("%B %Y"))})</h2>{_outcome_table(month, "No calls recorded this month.")}'
            f'<h2>Recent weeks</h2>{recent_weeks}')
    return ctx.page("Overview", body)


@router.post("/portal/handled")
async def portal_handled(request: Request) -> Response:
    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    if ctx.sample:
        return _html(_shell("Sample", f'{SAMPLE_BANNER}<p>{_h(SAMPLE_READ_ONLY)}</p><p><a href="/portal/overview">Back</a></p>'), 403)
    form = await _post_form(request, ctx)
    if isinstance(form, Response):
        return form
    storage.resolve_attention(ctx.client_id, form.get("call", ""))       # scoped to THIS client in SQL
    return _redirect("/portal/overview#attention")


# ------------------------------------------------------------------ calendar
async def _spam_action(request: Request, action) -> Response:
    from app import spamtag

    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    if not spamtag.enabled():
        return Response(status_code=403)
    if ctx.sample:
        return _html(_shell("Sample", f'{SAMPLE_BANNER}<p>{_h(SAMPLE_READ_ONLY)}</p><p><a href="/portal/overview">Back</a></p>'), 403)
    form = await _post_form(request, ctx)
    if isinstance(form, Response):
        return form
    action(ctx.client_id, form.get("call", ""))                 # scoped to THIS client in SQL
    return _redirect("/portal/overview#attention")


@router.post("/portal/spam")
async def portal_spam(request: Request) -> Response:
    from app import spamtag

    return await _spam_action(request, spamtag.mark)


@router.post("/portal/spam/undo")
async def portal_spam_undo(request: Request) -> Response:
    from app import spamtag

    return await _spam_action(request, spamtag.undo)


def _month_arg(raw: str, today: date) -> date:
    m = re.fullmatch(r"(\d{4})-(\d{2})", raw or "")
    if m and 2000 <= int(m.group(1)) <= 2100 and 1 <= int(m.group(2)) <= 12:
        return date(int(m.group(1)), int(m.group(2)), 1)
    return today.replace(day=1)


@router.get("/portal/calendar", response_class=HTMLResponse)
def portal_calendar(request: Request, month: str = "") -> Response:
    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    today = datetime.now(ctx.tz).date()
    first = _month_arg(month, today)
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        rows = conn.execute("SELECT caller_name, caller_phone, service, slot_start, status FROM bookings WHERE client_id = ? AND slot_start LIKE ? ORDER BY slot_start",
                            (ctx.client_id, first.strftime("%Y-%m") + "-%")).fetchall()
    finally:
        conn.close()
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r[3][:10], []).append(r)
    grid = []
    for week in calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month):
        cells = []
        for d in week:
            events = "".join(f'<span class="ev">{_h(_fmt_time(r[3]))} {_h(r[2])}</span>' for r in by_day.get(d.isoformat(), [])) if d.month == first.month else ""
            cls = "out" if d.month != first.month else ("today" if d == today else "")
            cells.append(f'<td class="{cls}"><span class="d">{d.day}</span>{events}</td>')
        grid.append("<tr>" + "".join(cells) + "</tr>")
    head = "".join(f"<th>{n}</th>" for n in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"))
    prev = (first - timedelta(days=1)).strftime("%Y-%m")
    nxt = (first + timedelta(days=31)).replace(day=1).strftime("%Y-%m")
    agenda = "".join(
        f"<tr><td>{_h(_fmt_slot(slot))}</td><td>{_h(service)}</td><td>{_h(name)}</td>"
        f'<td>{_caller_link(phone)}</td><td>{_h(status)}</td></tr>'
        for name, phone, service, slot, status in rows
    ) or f'<tr><td colspan="5" class="empty">{NO_DATA}: no bookings in {_h(first.strftime("%B %Y"))}.</td></tr>'
    upcoming = _upcoming_bookings(ctx, 20)
    body = (f'{ctx.nav("calendar")}<h2>Upcoming bookings</h2>{_upcoming_html(upcoming, "No upcoming bookings yet. When the receptionist books a visit, it shows up here.")}'
            f'<h2>{_h(first.strftime("%B %Y"))}</h2>'
            f'<p><a href="/portal/calendar?month={prev}">&laquo; Previous</a> &middot; <a href="/portal/calendar">Today</a> &middot; <a href="/portal/calendar?month={nxt}">Next &raquo;</a></p>'
            f'<div class="wrap"><table class="cal"><tr>{head}</tr>{"".join(grid)}</table></div>'
            '<p class="sub">Read-only. Bookings the receptionist made, in your local time. To change or cancel one, call or text us.</p>'
            f'<h2>Agenda</h2><div class="wrap"><table><tr><th>When</th><th>Service</th><th>Caller</th><th>Phone</th><th>Status</th></tr>{agenda}</table></div>')
    return ctx.page("Bookings", body)


# ------------------------------------------------------------------ calls
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _day_bound(raw: str, tz: ZoneInfo, *, end: bool) -> str | None:
    if not _DATE_RE.fullmatch(raw or ""):
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        return None
    if end:
        d += timedelta(days=1)
    return d.astimezone(timezone.utc).isoformat()


def _call_query(ctx: _Ctx, outcome: str, date_from: str, date_to: str) -> tuple[str, list]:
    sql, args = "FROM calls WHERE client_id = ?", [ctx.client_id]
    if outcome in outcomes.OUTCOMES:
        sql += " AND outcome_class = ?"
        args.append(outcome)
    lo, hi = _day_bound(date_from, ctx.tz, end=False), _day_bound(date_to, ctx.tz, end=True)
    if lo:
        sql += " AND started_at >= ?"
        args.append(lo)
    if hi:
        sql += " AND started_at < ?"
        args.append(hi)
    return sql, args


@router.get("/portal/calls", response_class=HTMLResponse)
def portal_calls(request: Request, outcome: str = "", date_from: str = "", date_to: str = "", page: str = "1") -> Response:
    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    # Parsed after authentication so a malformed value is a readable 400, not framework 422 JSON.
    if not page.isascii() or not page.isdigit():
        return _html(_shell("Calls", ctx.nav("calls") + '<p role="alert">That page number is not valid.</p><p><a href="/portal/calls">Back to calls</a></p>'), 400)
    page = max(1, min(int(page), 10_000))
    where, args = _call_query(ctx, outcome, date_from, date_to)
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        total = conn.execute(f"SELECT COUNT(*) {where}", args).fetchone()[0]
        rows = conn.execute(f"SELECT from_number, started_at, outcome, summary, transcript_json, outcome_class, needs_attention, attention_resolved_at {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                            args + [CALLS_PER_PAGE, (page - 1) * CALLS_PER_PAGE]).fetchall()
    finally:
        conn.close()
    items = []
    for from_number, started, outcome_raw, text, transcript_json, cls, needs, resolved in rows:
        details = ""
        if ctx.config.record_transcripts:
            try:
                turns = json.loads(transcript_json or "[]")
            except ValueError:
                turns = []
            if turns:
                lines = "".join(f"<p><b>{'Caller' if t.get('role') == 'caller' else 'AI'}:</b> {_h(str(t.get('text', '')))}</p>" for t in turns if isinstance(t, dict))
                details = f"<details><summary>Read the full conversation</summary>{lines}</details>"
            body = f"<p>{_h(text)}</p>" if text else '<p class="empty">No summary was written for this call.</p>'
        else:
            body = "<p>Details not recorded for privacy.</p>"
        flag = ""
        if needs and not resolved:
            flag = ' <span class="tag warn">Needs a call back</span>'
        elif needs:
            flag = ' <span class="tag">Handled</span>'
        back = f'<div class="actions">{_call_back_button(from_number)}</div>' if needs and not resolved else ""
        items.append(f'<div class="call{" attn" if needs and not resolved else ""}"><div class="meta"><b>{_h(_fmt_utc(started, ctx.tz))}</b> &middot; {_caller_link(from_number)} '
                     f'&middot; <span class="tag">{_h(_class_label(cls, outcome_raw))}</span>{flag}</div>{body}{details}{back}</div>')
    opts = '<option value="">All outcomes</option>' + "".join(
        f'<option value="{_h(o)}"{" selected" if o == outcome else ""}>{_h(outcomes.LABELS.get(o, o))}</option>' for o in outcomes.OUTCOMES)
    q = f"outcome={quote(outcome)}&date_from={quote(date_from)}&date_to={quote(date_to)}"
    pager = ""
    if page > 1:
        pager += f'<a href="/portal/calls?{_h(q)}&page={page - 1}">&laquo; Newer</a> '
    if page * CALLS_PER_PAGE < total:
        pager += f'<a href="/portal/calls?{_h(q)}&page={page + 1}">Older &raquo;</a>'
    filtered = bool(outcome in outcomes.OUTCOMES or _DATE_RE.fullmatch(date_from) or _DATE_RE.fullmatch(date_to))
    empty_calls = (f"<p class=empty>{NO_DATA}: no calls match. Try clearing the filters.</p>" if filtered else
                   '<div class="welcome"><h2>No calls yet</h2><p>Every call the receptionist answers will be listed here, newest first, with a short summary '
                   'of what the caller wanted and what happened. Make a test call from another phone to see how it looks.</p></div>')
    body = (f'{ctx.nav("calls")}<form method="get" action="/portal/calls" class="filters">'
            f'<div><label for="o">Outcome</label><select id="o" name="outcome">{opts}</select></div>'
            f'<div><label for="f">From</label><input id="f" type="date" name="date_from" value="{_h(date_from if _DATE_RE.fullmatch(date_from) else "")}"></div>'
            f'<div><label for="t">To</label><input id="t" type="date" name="date_to" value="{_h(date_to if _DATE_RE.fullmatch(date_to) else "")}"></div>'
            '<button type="submit">Filter</button> <a href="/portal/calls">Clear</a></form>'
            f'<p class="sub">{total} call(s) match. <a href="/portal/export/calls.csv">Download all calls (CSV)</a> &middot; '
            f'<a href="/portal/export/bookings.csv">Download all bookings (CSV)</a></p>'
            f'{"".join(items) or empty_calls}<p>{pager}</p>')
    return ctx.page("Calls", body)


# ------------------------------------------------------------------ settings
_DAYS = (("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"), ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday"))


_ROUTING_PLAIN = {
    "ai_first": "The receptionist answers every call that is forwarded to it.",
    "owner_first": "Your phone rings first. If you do not pick up, the receptionist answers.",
    "after_hours": "Your phone rings first while you are open. When you are closed, the receptionist answers right away.",
}


def _yes_no(flag: bool, yes: str, no: str) -> str:
    return _h(yes if flag else no)


@router.get("/portal/settings", response_class=HTMLResponse)
def portal_settings(request: Request) -> Response:
    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    c, b = ctx.config, brand.get()
    from app import provisioning                              # local: provisioning imports storage, keep portal import-light
    numbers = provisioning.numbers_for(ctx.client_id)         # our own record only: no network call
    number_html = (" and ".join(f"<b>{_h(provisioning.format_phone(n))}</b>" for n in numbers) if numbers else "not assigned yet")
    hours = "".join(f"<tr><td>{label}</td><td>{'Closed' if c.business_hours.get(k, 'closed') == 'closed' else _h(' - '.join(c.business_hours[k]))}</td></tr>" for k, label in _DAYS)
    services = "".join(f"<tr><td>{_h(s.name)}</td><td>{s.duration_minutes} min</td></tr>" for s in c.services) or f'<tr><td colspan="2" class="empty">{NO_DATA}</td></tr>'
    booking_note = "" if c.booking_hours is None else "<p class=sub>Appointments are only offered during separate booking hours that we set up with you.</p>"
    body = (f'{ctx.nav("settings")}<h2>How {_h(b.name)} is set up for you</h2>'
            f'<p class="sub">This page is read-only so nothing changes by accident. To change anything, call or text {_h(b.contact_name)} at <a href="{_h(_tel(b.support_phone))}">{_h(b.support_phone)}</a>.</p>'
            f'<div class="card"><h2>Your receptionist</h2><p>Receptionist phone number: {number_html}.</p>'
            f'<p>{_h(_ROUTING_PLAIN.get(c.routing_mode, ""))}</p>'
            f'<p>Callers hear that it is an AI receptionist. Calls are answered in {_yes_no(c.spanish, "English, with Spanish offered on request", "English")}.</p>'
            f'<p>{_yes_no(c.record_transcripts, "Call summaries and conversations are saved so you can read them here.", "For privacy, what callers say is not saved. You see who called and the outcome only.")}</p></div>'
            f'<h2>Business hours ({_h(c.timezone)})</h2><div class="wrap"><table>{hours}</table></div>{booking_note}'
            f'<h2>Services it can book</h2><div class="wrap"><table><tr><th>Service</th><th>Length</th></tr>{services}</table></div>'
            f'<h2>Calls that need a person</h2><p>Put-through calls and urgent calls ring <b>{_h(_fmt_phone(c.escalation_phone))}</b>. '
            'If nobody answers, the receptionist takes a message and it appears under <b>Needs your attention</b>.</p>'
            '<h2>Turn call forwarding off</h2><p>You can do this any time without asking us. Dial from the phone that forwards to us: Verizon <b>*73</b>, AT&amp;T <b>#21#</b>, T-Mobile <b>##21#</b>, or <b>##002#</b> on most other carriers. '
            'Calls then ring your phone directly again.</p>'
            f'<h2>Support</h2><p>{_h(b.contact_name)}: <a href="{_h(_tel(b.support_phone))}">{_h(b.support_phone)}</a> &middot; <a href="mailto:{_h(_support_email())}">{_h(_support_email())}</a></p>'
            f'<h2>Account</h2><p>Signed in as {_h(ctx.user["email"])} &middot; <a href="/portal/password">Change password</a></p>')
    return ctx.page("Setup", body)


@router.get("/portal/help", response_class=HTMLResponse)
def portal_help(request: Request) -> Response:
    from app import spamtag

    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    b = brand.get()
    body = (f'{ctx.nav("help")}<h2>Help</h2>'
            f'<div class="card"><h2>Talk to {_h(b.contact_name)}</h2><p>{_h(b.contact_name)} runs {_h(b.name)} and answers questions personally.</p>'
            f'<div class="actions"><a class="btn call-back" href="{_h(_tel(b.support_phone))}">Call or text {_h(b.support_phone)}</a>'
            f'<a class="btn" href="mailto:{_h(_support_email())}">Email {_h(_support_email())}</a></div></div>'
            '<h2>Common questions</h2>'
            '<div class="card"><p><b>A caller needs a call back.</b> It is listed under Needs your attention on Home. Tap <b>Call back</b>, then <b>Mark handled</b> when you are done.</p>'
            + ('<p><b>A caller is not a customer (spam or a sales call).</b> Tap <b>Not a customer (spam)</b> on that call. It leaves your list, and calls from that number stop texting or emailing you. '
               'It does not block the caller. A possible emergency still alerts you. Use <b>Undo</b> under Marked as not a customer if you tapped it by mistake.</p>' if spamtag.enabled() else '')
            + '<p><b>What is in the text or email after a call?</b> The first line is who to call and their number, then the issue in one line, whether it may be an emergency, and the ZIP only if the caller said one. '
            'Anything we did not record says so.</p>'
            '<p><b>What does When your calls came in show?</b> Calls from the last 7 days split into your business hours and after hours, using the hours in Setup, and what came of each. These are counts, not dollars.</p>'
            '<p><b>I do not see a call I expected.</b> Calls appear here shortly after they end. If one is missing, text us the time and the caller\'s number.</p>'
            '<p><b>I want to change my hours, services or the way calls are answered.</b> Call or text us.</p>'
            '<p><b>I want the receptionist off.</b> Turn call forwarding off (see Setup). Your phone rings you directly again at once.</p>'
            '<p><b>I forgot my password.</b> Call or text us and we will send a new temporary one.</p></div>')
    return ctx.page("Help", body)


# ------------------------------------------------------------------ CSV export
@router.get("/portal/export/calls.csv")
def portal_export_calls(request: Request) -> Response:
    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        rows = conn.execute("SELECT started_at, from_number, outcome_class, outcome, needs_attention, attention_resolved_at, summary FROM calls WHERE client_id = ? ORDER BY started_at DESC",
                            (ctx.client_id,)).fetchall()
    finally:
        conn.close()
    keep = ctx.config.record_transcripts
    out = [[_fmt_utc(s, ctx.tz), _fmt_phone(frm), _class_label(cls, oc), "yes" if na else "no", "yes" if resolved else "no", (summary or "") if keep else ""]
           for s, frm, cls, oc, na, resolved, summary in rows]
    return _csv_response("SAMPLE-calls.csv" if ctx.sample else "calls.csv", ["Started (local)", "Caller", "Outcome", "Needed attention", "Marked handled", "Summary"], out)


@router.get("/portal/export/bookings.csv")
def portal_export_bookings(request: Request) -> Response:
    ctx = _require(request)
    if isinstance(ctx, Response):
        return ctx
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        rows = conn.execute("SELECT slot_start, slot_end, service, caller_name, caller_phone, status FROM bookings WHERE client_id = ? ORDER BY slot_start", (ctx.client_id,)).fetchall()
    finally:
        conn.close()
    out = [[s, e, svc, name, _fmt_phone(phone), status] for s, e, svc, name, phone, status in rows]
    return _csv_response("SAMPLE-bookings.csv" if ctx.sample else "bookings.csv", ["Start (local)", "End (local)", "Service", "Caller", "Phone", "Status"], out)
