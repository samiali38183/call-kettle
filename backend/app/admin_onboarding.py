"""Phone-friendly onboarding pages for the operator (Sami), plus the client-facing "turn on your line" page.

  /admin/intake/<n>/review   the config generated from the client's form; edit it and press "Go live"
  /admin/client/<id>/number  find and buy a phone number for the client (one click, about $1.15/month)
  /activate/<id>?key=        what the CLIENT opens: their number and the exact forwarding codes, filled in

All pages except /activate need the master key. /activate accepts the client's own dashboard key.
Nothing here is on the call path."""
from __future__ import annotations

import logging
import os
import re
import yaml
from html import escape as _h

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import brand, onboarding, owner_activation, owner_auth, portal, provisioning, storage, twilio_utils
from app.config import ConfigRejected, list_client_ids, load_client_config, save_live_config, validate_live_config, remove_live_config

logger = logging.getLogger("callkettle.onboarding")
router = APIRouter()

_master_ok = lambda key: False           # replaced by install()
_client_key = lambda client_id: ""       # replaced by install()

CSS = """body{font-family:-apple-system,Segoe UI,sans-serif;max-width:860px;margin:18px auto;padding:0 16px 60px;color:#20293A;background:#F9FAFA;font-size:17px;line-height:1.5}
h1{font-size:24px;margin:8px 0}h2{font-size:18px;margin:26px 0 8px}.card{background:#fff;border:1px solid #DEE3E8;border-radius:12px;padding:16px;margin:12px 0}
textarea{width:100%;min-height:420px;font:13px/1.45 ui-monospace,Consolas,monospace;border:1px solid #C5CDD8;border-radius:8px;padding:10px}
input[type=text],input[type=number]{font-size:18px;padding:10px;border:1px solid #C5CDD8;border-radius:8px;width:120px}
button,.btn{display:inline-block;background:#2358D6;color:#fff;border:0;border-radius:10px;padding:13px 22px;font-size:18px;font-weight:600;text-decoration:none;cursor:pointer}
button.green{background:#0E7F66}.muted{color:#5A6577;font-size:15px}.err{background:#FBE9E7;border:1px solid #F0C9C5;color:#8A2620;padding:12px;border-radius:10px;margin:12px 0}
.ok{background:#E3F4EE;border:1px solid #B7E0D2;color:#0B5E4B;padding:12px;border-radius:10px;margin:12px 0}.warn{background:#FCF1DC;border:1px solid #F0D9A8;color:#5C3A00;padding:12px;border-radius:10px;margin:12px 0}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #DEE3E8;padding:9px 10px;text-align:left;vertical-align:top;font-size:16px}th{background:#F1F4F8;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:#5A6577}
.wrap{overflow-x:auto}.steps{padding-left:0;list-style:none;display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 0}.steps li{background:#fff;border:1px solid #DEE3E8;border-radius:20px;padding:4px 12px;font-size:14px;color:#5A6577}
@media(max-width:600px){body{font-size:16px}h1{font-size:21px}.big{font-size:28px}button,.btn{display:block;text-align:center}}
code{background:#EEF1F6;padding:2px 7px;border-radius:6px;font-size:.95em}.big{font:700 34px ui-monospace,Consolas,monospace;letter-spacing:.02em}a{color:#2358D6}"""


def install(app, *, master_key_ok, report_key_for) -> None:
    global _master_ok, _client_key
    _master_ok, _client_key = master_key_ok, report_key_for
    app.include_router(router)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="robots" content="noindex,nofollow"><title>{_h(title)}</title><style>{CSS}</style></head><body>{body}</body></html>',
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": portal.CSP},
    )


def _denied() -> HTMLResponse:
    return _page("Not authorized", "<p>Not authorized.</p>", 403)


def _form_value(form, name: str) -> str:
    return str(form.get(name, "")).strip()


# ---------------------------------------------------------------------------- 1. review the config and go live
def _review_body(intake_id: int, intake: dict, yaml_text: str, key: str, error: str = "", note: str = "", owner_email: str | None = None) -> str:
    p = intake["payload"]
    if owner_email is None:
        owner_email = str(p.get("owner_email") or "")
    cid_match = re.search(r"^client_id:\s*(\S+)", yaml_text, re.M)
    replaces = cid_match and cid_match.group(1) in list_client_ids()
    warn = (f'<div class="warn">A client named <code>{_h(cid_match.group(1))}</code> already exists. Choose an unused client id; a new intake cannot replace another business.</div>'
            if replaces else "")
    err = f'<div class="err">{_h(error)}</div>' if error else ""
    sens = ('<div class="warn">This looks like a healthcare or legal business. Transcripts are set to OFF. Read the compliance notes '
            'before taking this client: we are not set up for protected health information.</div>'
            if "record_transcripts: false" in yaml_text else "")
    return f"""<h1>New client: {_h(p.get('business_name', ''))}</h1>
<p class="muted">{_h(p.get('trade', ''))} &middot; {_h(p.get('owner_name', ''))} &middot; {_h(p.get('owner_phone', ''))} &middot; {_h(p.get('owner_email', '') or 'no email')}
&middot; phone provider: {_h(p.get('phone_provider', '') or 'not given')}</p>
{f'<div class="card"><b>Their notes:</b> {_h(p.get("notes", ""))}</div>' if p.get('notes') else ''}
{err}{warn}{sens}{note}
<div class="card"><p>This is the receptionist built from their form. Read it, fix anything (their own words are in <code>extra_instructions</code>), then press <b>Go live</b>.
It starts answering test calls immediately. It does not get a phone number yet.</p>
<form method="post" action="/admin/intake/{intake_id}/publish">
<p><label for="owner_email">Owner portal email (required)</label><br>
<input id="owner_email" type="email" name="owner_email" value="{_h(owner_email)}" maxlength="254" required autocomplete="email" style="width:100%;max-width:420px"></p>
<p class="muted">Confirm the customer's email for this new account. Existing accounts cannot be reassigned; use Owner login recovery.</p>
<textarea name="yaml" spellcheck="false">{_h(yaml_text)}</textarea><p><button class="green" type="submit">Go live</button></p></form></div>
<p class="muted"><a href="/admin">Back to all clients</a></p>"""


@router.get("/admin/intake/{intake_id}/review", response_class=HTMLResponse)
def review_intake(intake_id: int, key: str = "") -> HTMLResponse:
    if not _master_ok(key):
        return _denied()
    intake = storage.get_intake(intake_id)
    if intake is None:
        return _page("Not found", "<p>No such intake.</p>", 404)
    if intake["status"] != "new":
        return _page("Already published", "<p>This intake is already published. Recover the existing owner account instead of publishing again.</p>", 409)
    try:
        cid = onboarding.unique_client_id(intake["payload"]["business_name"], list_client_ids())
        yaml_text = onboarding.to_yaml(onboarding.config_from_intake(intake["payload"], client_id=cid))
    except Exception as exc:   # a form we cannot turn into a config: show why, and the raw answers
        return _page("Cannot build", f'<h1>Could not build a config</h1><div class="err">{_h(str(exc)[:600])}</div>'
                     f'<pre>{_h(str(intake["payload"])[:3000])}</pre><p><a href="/admin">Back</a></p>', 422)
    return _page("Review new client", _review_body(intake_id, intake, yaml_text, key))


@router.post("/admin/intake/{intake_id}/publish", response_class=HTMLResponse)
async def publish_intake(intake_id: int, request: Request) -> HTMLResponse:
    form = await request.form()
    key = request.query_params.get("key", "") or _form_value(form, "key")   # the login cookie supplies it; scripts may pass it
    if not _master_ok(key):
        return _denied()
    intake = storage.get_intake(intake_id)
    if intake is None:
        return _page("Not found", "<p>No such intake.</p>", 404)
    yaml_text = str(form.get("yaml", ""))
    # Only the authenticated operator's publish form can complete/correct an
    # intake email. An explicit blank is invalid, not a fallback to the intake.
    owner_email = owner_auth.normalize_email(str(form.get("owner_email", intake["payload"].get("owner_email") or "")))
    config = None
    saved = False
    try:
        # Serialize publish attempts across workers. Account and intake commit together;
        # compensate the new file if saving or the database commit fails.
        with storage._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            status = conn.execute("SELECT status FROM intakes WHERE id = ?", (intake_id,)).fetchone()[0]
            if status != "new":
                return _page("Already published", "<p>This intake is already published. Use account recovery if its password was lost; do not publish it again.</p>", 409)
            config = validate_live_config(yaml_text)
            if config.client_id in list_client_ids():
                raise ConfigRejected("This client id already exists. A new intake cannot replace an existing business.")
            existing = conn.execute("SELECT id FROM owner_users WHERE email = ? OR client_id = ?", (owner_email, config.client_id)).fetchone()
            if not owner_auth.valid_email(owner_email) or existing:
                raise ConfigRejected("A valid, unused owner email and client id are required. Recover existing accounts separately; never reassign them.")
            if "owner_email" in form:
                # Keep owner notifications and the new account on the same
                # operator-confirmed address. Leave all other reviewed YAML intact.
                reviewed = yaml.safe_load(yaml_text)
                reviewed["owner_email"] = owner_email
                yaml_text = onboarding.to_yaml(reviewed)
                config = validate_live_config(yaml_text)
            temp_pw = owner_auth.create_user(config.client_id, owner_email, conn=conn)
            user_id = conn.execute("SELECT id FROM owner_users WHERE email=?", (owner_email,)).fetchone()[0]
            invitation = owner_activation.create_invitation(user_id, conn=conn)
            conn.execute("UPDATE intakes SET status = 'live' WHERE id = ?", (intake_id,))
            # Set before save so an exception after its file write is compensated too.
            saved = True
            config, created = save_live_config(yaml_text)
    except (ConfigRejected, ValueError) as exc:
        if saved and config is not None:
            remove_live_config(config.client_id)
        return _page("Fix onboarding", _review_body(intake_id, intake, yaml_text, key, error=str(exc)[:1200], owner_email=owner_email), 422)
    except Exception:
        if saved and config is not None:
            remove_live_config(config.client_id)
        logger.error("Publish failed for intake %s; no credentials recorded", intake_id)
        return _page("Publish failed", "<p>Onboarding could not be completed. No account or intake changes were committed. Check storage before retrying.</p>", 503)
    logger.info("Client %s published from intake %s", config.client_id, intake_id)
    portal_card = (f'<div class="card"><h2 style="margin-top:0">Owner portal login</h2>'
                   f'<p>The customer account has been created:</p>'
                   f'<p><a href="/portal/activate?token={_h(invitation)}">Customer account activation</a></p>'
                   '<p class="warn">Shown once; expires in one hour. Send this invitation privately so the customer can choose their own password. '
                   'It does not turn on phone service. If it expires, use Owner login recovery.</p>'
                   f'<table><tr><th>Portal</th><td><a href="/portal/login">/portal/login</a></td></tr>'
                   f'<tr><th>Email</th><td><code>{_h(owner_email)}</code></td></tr>'
                   f'<tr><th>Temporary password</th><td><code>{_h(temp_pw)}</code></td></tr></table>'
                   '<p class="warn">Shown once. Send the password by a separate channel, then they must change it at first sign-in.</p></div>')
    body = f"""<h1>{_h(config.business_name)} is live</h1>
<div class="ok">The receptionist is answering test calls now ({'created' if created else 'updated'} as <code>{_h(config.client_id)}</code>).</div>
{portal_card}
<div class="card"><h2 style="margin-top:0">Next: give them a phone number</h2>
<p>About $1.15 a month. You choose the area code.</p>
<p><a class="btn" href="/admin/client/{_h(config.client_id)}/number">Get a phone number</a></p></div>
<div class="card"><h2 style="margin-top:0">Their legacy private dashboard</h2>
<p><a href="/report/{_h(config.client_id)}?key={_h(_client_key(config.client_id))}">Open it</a> (send this link to <b>them only</b> only if they cannot use the portal yet).</p></div>
<p class="muted"><a href="/admin">Back to all clients</a></p>"""
    return _page("Live", body)


# ---------------------------------------------------------------------------- 2. buy a number
def _number_body(client_id: str, key: str, *, message: str = "", found: str = "", area: str = "571") -> str:
    cfg = load_client_config(client_id)
    tw = twilio_utils._client()
    have = provisioning.numbers_for(client_id, tw)
    have_html = ("".join(f'<p class="big">{_h(provisioning.format_phone(n))}</p>' for n in have)
                 + f'<p><a class="btn" href="/activate/{_h(client_id)}?key={_h(_client_key(client_id))}">Open the page to send your client</a></p>'
                 if have else '<p class="muted">No number yet.</p>')
    buy = ""
    if found:
        buy = (f'<div class="card"><p>Available: <span class="big">{_h(provisioning.format_phone(found))}</span></p>'
               f'<p class="muted">Buying it charges about ${provisioning.NUMBER_PRICE_PER_MONTH:.2f} a month to your Twilio account.</p>'
               f'<form method="post" action="/admin/client/{_h(client_id)}/number">'
               f'<input type="hidden" name="action" value="buy"><input type="hidden" name="number" value="{_h(found)}">'
               f'<button class="green" type="submit">Buy this number and connect it</button></form></div>')
    return f"""<h1>Phone number for {_h(cfg.business_name)}</h1>{message}
<div class="card"><h2 style="margin-top:0">Current number</h2>{have_html}</div>
<div class="card"><h2 style="margin-top:0">Find a new one</h2>
<form method="post" action="/admin/client/{_h(client_id)}/number"><input type="hidden" name="action" value="search">
<p>Area code <input type="text" name="area_code" value="{_h(area)}" maxlength="3" inputmode="numeric"> <button type="submit">Find a number</button></p></form></div>{buy}
<p class="muted"><a href="/admin">Back to all clients</a></p>"""


@router.get("/admin/client/{client_id}/number", response_class=HTMLResponse)
def number_page(client_id: str, key: str = "") -> HTMLResponse:
    if not _master_ok(key):
        return _denied()
    if client_id not in list_client_ids():
        return _page("Not found", "<p>No such client.</p>", 404)
    return _page("Phone number", _number_body(client_id, key))


@router.post("/admin/client/{client_id}/number", response_class=HTMLResponse)
async def number_action(client_id: str, request: Request) -> HTMLResponse:
    form = await request.form()
    key = request.query_params.get("key", "") or _form_value(form, "key")   # the login cookie supplies it; scripts may pass it
    if not _master_ok(key):
        return _denied()
    if client_id not in list_client_ids():
        return _page("Not found", "<p>No such client.</p>", 404)
    tw = twilio_utils._client()
    if tw is None:
        return _page("Phone number", _number_body(client_id, key, message='<div class="err">Twilio is not configured on the server.</div>'), 500)
    cfg = load_client_config(client_id)
    action = _form_value(form, "action")
    try:
        if action == "search":
            area = _form_value(form, "area_code")
            number = provisioning.search_number(tw, area)
            msg = "" if number else f'<div class="warn">No numbers are available in {_h(area)}. Try another area code.</div>'
            return _page("Phone number", _number_body(client_id, key, message=msg, found=number or "", area=area or "571"))
        if action == "buy":
            done = provisioning.buy_number(tw, client_id, _form_value(form, "number"), cfg.escalation_phone)
            logger.info("Bought a number for %s", client_id)
            msg = (f'<div class="ok">Done. {_h(provisioning.format_phone(done["phone_number"]))} now answers as {_h(cfg.business_name)}. '
                   f'Call it to test, then send your client the page below so they can turn on forwarding.</div>')
            return _page("Phone number", _number_body(client_id, key, message=msg))
    except ValueError as exc:
        return _page("Phone number", _number_body(client_id, key, message=f'<div class="err">{_h(str(exc))}</div>'), 422)
    except Exception as exc:   # Twilio errors: surface the reason, change nothing
        logger.exception("Number purchase failed for %s", client_id)
        return _page("Phone number", _number_body(client_id, key, message=f'<div class="err">Twilio said: {_h(str(exc)[:300])}</div>'), 502)
    return _page("Phone number", _number_body(client_id, key, message='<div class="err">Unknown action.</div>'), 400)


# ---------------------------------------------------------------------------- 2b. owner portal logins (create / recover)
# Production runs from a container image on Fly: an operator script run on a laptop writes to the LAPTOP's database, not the
# live one. This page is how the operator creates a missing owner login or recovers one whose temporary password was lost,
# from a phone, against the real database. The account is always looked up inside THIS client (never reassigned).
def _owner_rows(client_id: str) -> list[tuple]:
    with storage._conn() as conn:
        return conn.execute("SELECT email, must_change, last_login_at, locked_until, disabled_at FROM owner_users WHERE client_id = ? ORDER BY email",
                            (client_id,)).fetchall()


def _owner_body(client_id: str, *, message: str = "") -> str:
    cfg = load_client_config(client_id)
    now = owner_auth._now()
    rows = ""
    for email, must_change, last_login, locked_until, disabled_at in _owner_rows(client_id):
        state = ("disabled" if disabled_at is not None else "locked (too many wrong passwords)" if (locked_until or 0) > now
                 else "must change the temporary password" if must_change else "active")
        seen = "never" if not last_login else "yes"
        reset = ("" if disabled_at is not None else
                 f'<form method="post" action="/admin/client/{_h(client_id)}/owner" style="margin:0"><input type="hidden" name="action" value="reset">'
                 f'<input type="hidden" name="email" value="{_h(email)}"><button type="submit">New temporary password</button></form>')
        rows += f"<tr><td><code>{_h(email)}</code></td><td>{_h(state)}</td><td>{seen}</td><td>{reset}</td></tr>"
    table = (f"<table><tr><th>Email</th><th>State</th><th>Signed in</th><th></th></tr>{rows}</table>" if rows
             else '<p class="warn">No owner login exists for this client yet.</p>')
    return f"""<h1>Owner portal login: {_h(cfg.business_name)}</h1>{message}
<div class="card">{table}
<p class="muted">A new temporary password signs the owner out everywhere, unlocks the account and forces a new password at the next sign-in.
Send it by a different channel than the portal address (call or text). It is shown once and never stored.</p></div>
<div class="card"><h2 style="margin-top:0">Create a login</h2>
<form method="post" action="/admin/client/{_h(client_id)}/owner"><input type="hidden" name="action" value="create">
<p><input type="text" name="email" placeholder="owner@example.com" style="width:320px" autocomplete="off"> <button type="submit">Create login</button></p></form></div>
<p class="muted"><a href="/admin">Back to all clients</a></p>"""


def _temp_card(email: str, temp: str) -> str:
    return (f'<div class="card"><h2 style="margin-top:0">Give this to the owner</h2><table>'
            f'<tr><th>Portal</th><td><a href="/portal/login">/portal/login</a></td></tr>'
            f'<tr><th>Email</th><td><code>{_h(email)}</code></td></tr>'
            f'<tr><th>Temporary password</th><td><code>{_h(temp)}</code></td></tr></table>'
            '<p class="warn">Shown once. Send it by a separate channel; they must change it at first sign-in.</p></div>')


@router.get("/admin/client/{client_id}/owner", response_class=HTMLResponse)
def owner_page(client_id: str, key: str = "") -> HTMLResponse:
    if not _master_ok(key):
        return _denied()
    if client_id not in list_client_ids():
        return _page("Not found", "<p>No such client.</p>", 404)
    return _page("Owner login", _owner_body(client_id))


@router.post("/admin/client/{client_id}/owner", response_class=HTMLResponse)
async def owner_action(client_id: str, request: Request) -> HTMLResponse:
    form = await request.form()
    key = request.query_params.get("key", "") or _form_value(form, "key")
    if not _master_ok(key):
        return _denied()
    if client_id not in list_client_ids():
        return _page("Not found", "<p>No such client.</p>", 404)
    action, email = _form_value(form, "action"), owner_auth.normalize_email(_form_value(form, "email"))
    if action == "reset":
        if email not in {r[0] for r in _owner_rows(client_id)}:          # only an account of THIS client
            return _page("Owner login", _owner_body(client_id, message='<div class="err">No owner login with that email for this client.</div>'), 404)
        try:
            temp = owner_auth.reset_user(email)
        except ValueError as exc:
            return _page("Owner login", _owner_body(client_id, message=f'<div class="err">{_h(str(exc))}</div>'), 422)
        logger.info("Owner password reset for client %s by the operator", client_id)
        return _page("Owner login", _owner_body(client_id, message=_temp_card(email, temp)))
    if action == "create":
        try:
            temp = owner_auth.create_user(client_id, email)
        except ValueError as exc:
            return _page("Owner login", _owner_body(client_id, message=f'<div class="err">{_h(str(exc))}</div>'), 422)
        logger.info("Owner login created for client %s by the operator", client_id)
        return _page("Owner login", _owner_body(client_id, message=_temp_card(email, temp)))
    return _page("Owner login", _owner_body(client_id, message='<div class="err">Unknown action.</div>'), 400)


# ---------------------------------------------------------------------------- 3. the client's "turn on your line" page
def forwarding_codes(number: str) -> dict:
    """Dial strings for each carrier, with the receptionist's number filled in. Verified against carrier support pages (see docs/TELEPHONY.md)."""
    d = re.sub(r"\D", "", number)[-10:]
    return {
        "Verizon": {"all": f"*72{d}", "no_answer": f"*71{d}", "off": "*73"},
        "AT&T": {"all": f"*21*{d}#", "no_answer": None, "off": "#21#"},
        "T-Mobile": {"all": f"**21*+1{d}#", "no_answer": f"**61*+1{d}#", "off": "##21#  (to clear the no-answer setting: ##61#)"},
    }


@router.get("/activate/{client_id}", response_class=HTMLResponse)
def activate_page(client_id: str, key: str = "") -> HTMLResponse:
    if not (_master_ok(key) or (key and key == _client_key(client_id))):
        return _denied()
    try:
        cfg = load_client_config(client_id)
    except Exception:
        return _page("Not found", "<p>Unknown line.</p>", 404)
    numbers = provisioning.numbers_for(client_id, twilio_utils._client())
    support = brand.get().support_phone
    support_link = f'<a href="tel:+1{re.sub(r"[^0-9]", "", support)[-10:]}">{_h(support)}</a>'
    if not numbers:
        return _page("Almost ready", f"<h1>Almost ready</h1><p>Your receptionist's phone number is not set up yet. We will text you the moment it is. Questions? Call or text {support_link}.</p>")
    n = numbers[0]
    codes = forwarding_codes(n)
    rows = ""
    for carrier, c in codes.items():
        na = f"<code>{_h(c['no_answer'])}</code>" if c["no_answer"] else "Not a reliable code on this carrier. Use your phone's Call Forwarding setting or ask AT&amp;T."
        rows += f"<tr><td><b>{_h(carrier)}</b></td><td><code>{_h(c['all'])}</code></td><td>{na}</td><td><code>{_h(c['off'])}</code></td></tr>"
    body = f"""<h1>Turn on {_h(cfg.business_name)}'s receptionist</h1>
<ol class="steps"><li>1. Choose how</li><li>2. Dial the code</li><li>3. Test it</li><li>4. Open your portal</li></ol>
<div class="card"><p>Your receptionist's number:</p><p class="big">{_h(provisioning.format_phone(n))}</p>
<p class="muted">You keep your current business number. You decide when calls go to the receptionist by setting <b>call forwarding</b> on your phone.</p></div>
<h2>1. Choose how</h2>
<div class="card"><p><b>Forward all calls:</b> every call goes to the receptionist. Good if you are rarely at your phone.<br>
<b>Forward only when you miss it:</b> your phone rings first; if you don't answer, the receptionist picks up.</p></div>
<h2>2. Dial the code from your business phone</h2>
<p class="muted">Dial it like a phone number, press call, and wait for the confirmation tone or message.</p>
<div class="wrap"><table><tr><th>Your carrier</th><th>Forward all calls</th><th>Only if you don't answer</th><th>Turn it off</th></tr>{rows}
<tr><td><b>Other carrier or office phone</b></td><td colspan="3">Open your phone's Settings, Phone, Call Forwarding, or your phone system's admin page (call forwarding / call routing), and forward to <b>{_h(provisioning.format_phone(n))}</b>. Not sure? Call or text us and we will walk you through it.</td></tr></table></div>
<h2>3. Test it</h2>
<div class="card"><p>From a <b>different phone</b>, call your normal business number. You should hear the receptionist answer and say it is an AI. Try booking an appointment.</p></div>
<h2>4. Open your owner portal</h2>
<div class="card"><p>After a test call, sign in to your owner portal. You should see the call there shortly after it ends, along with your bookings, anyone who needs a call back, and how everything is set up. If you do not have a login yet, call or text {support_link} before sending real customer calls through.</p>
<p><a class="btn" href="/portal/login">Open my owner portal</a> <span class="muted">(/portal/login)</span></p></div>
<h2>Turning it off</h2>
<div class="card"><p>Dial the "turn it off" code for your carrier above, or switch call forwarding off in your phone's settings. Your line rings you again right away. You never need us for that.</p></div>
<div class="warn">Codes and options vary by carrier plan and phone system. If a code doesn't work or you're not sure it's on, call or text {support_link} and we'll check it with a real test call.</div>"""
    return _page(f"Turn on {cfg.business_name}", body)
