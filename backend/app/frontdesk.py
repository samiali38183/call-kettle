"""Tenant-isolated owner follow-up workspace (no outbound messaging).

Integration: include router; call frontdesk_storage.init_db() AFTER storage.init_db()
in application startup. Add /portal/frontdesk to portal navigation. No import-time I/O.
The existing portal resolves account/session, forced-password-change, sample mode,
CSRF, safe callback links and security headers; this component reuses those rules.
"""
from __future__ import annotations

from datetime import datetime
from html import escape as h
from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from app import frontdesk_storage as workspace, portal

router = APIRouter()
PER_PAGE = 50
_LABELS = {"open": "Open", "waiting": "Waiting on customer", "handled": "Handled", "none": "No follow-up"}


def _url(sid: str) -> str:
    return "/portal/frontdesk/call/" + quote(sid, safe="")


def _error(message: str, status: int) -> Response:
    return portal._html(portal._shell("Front desk", f'<p role="alert">{h(message)}</p><p><a href="/portal/frontdesk">Back to front desk</a></p>'), status)


def _call_html(ctx, row: dict) -> str:
    due = row["followup_due_date"]
    timing = "No due date"
    if due:
        today = datetime.now(ctx.tz).date().isoformat()
        timing = ("Overdue" if due < today else "Due today" if due == today else "Due") + " - " + due
    summary = h(row["summary"] or "No summary recorded.") if ctx.config.record_transcripts else "Details not recorded for privacy."
    return (f'<div class="call"><p class="meta">{h(portal._fmt_utc(row["started_at"], ctx.tz))} &middot; '
            f'{portal._caller_link(row["from_number"])} &middot; {h(portal._class_label(row["outcome_class"], row["outcome"]))}</p>'
            f'<p><b>{h(_LABELS[row["state"]])}</b> &middot; {h(timing)}</p>'
            f'{portal._waiting_html(row["started_at"]) if row["state"] in ("open", "waiting") else ""}<p>{summary}</p>'
            f'<p><b>Owner note:</b> <span style="white-space:pre-wrap">{h(row["owner_note"] or "No owner note yet.")}</span></p>'
            f'<div class="actions">{portal._call_back_button(row["from_number"])}'
            f'<a class="btn" href="{h(_url(row["call_sid"]))}">View / update follow-up</a></div></div>')


@router.get("/portal/frontdesk")
def frontdesk_queue(request: Request, view: str = "queue", page: str = "1") -> Response:
    ctx = portal._require(request)
    if isinstance(ctx, Response):
        return ctx
    # Parsed here, after authentication, so a malformed value is a readable 400 and never a framework 422 JSON.
    if not page.isascii() or not page.isdigit():
        return _error("Invalid page number.", 400)
    page = int(page)
    try:
        rows, total = workspace.list_calls(ctx.client_id, view=view, page=page, per_page=PER_PAGE)
    except ValueError as exc:
        return _error(str(exc), 400)
    filters = ('<p><a href="/portal/frontdesk">Action queue</a> &middot; '
               '<a href="/portal/frontdesk?view=handled">Handled / reopen</a> &middot; '
               '<a href="/portal/frontdesk?view=all">All recorded calls</a></p>')
    pager = ""
    if page > 1:
        pager += f'<a href="/portal/frontdesk?view={h(view)}&amp;page={page - 1}">Previous</a> '
    if page * PER_PAGE < total:
        pager += f'<a href="/portal/frontdesk?view={h(view)}&amp;page={page + 1}">Next</a>'
    empty = "Nothing waiting on you." if view == "queue" else "No calls in this view."
    body = (f'{ctx.nav("frontdesk")}<h2>{h(portal.brand.get().name)} front desk</h2>'
            f'<p class="sub">Due dates are calendar days in {h(ctx.config.timezone)}. Open and waiting calls stay in the queue, '
            'earliest due date first, then oldest undated calls. Notes are private to your business. '
            'Saving here does not send a text or email.</p>' + filters +
            f'<p>{total} call(s) in this view. Page {page}.</p>' +
            ("".join(_call_html(ctx, row) for row in rows) or f'<p class="empty">{empty}</p>') + f'<p>{pager}</p>')
    return ctx.page("Front desk", body)


def _detail(ctx, row: dict) -> Response:
    body = ctx.nav("frontdesk") + '<h2>Follow-up details</h2>' + _call_html(ctx, row)
    if ctx.sample:
        body += f'<p>{h(portal.SAMPLE_READ_ONLY)}</p>'
    else:
        options = "".join(f'<option value="{state}"{" selected" if row["state"] == state else ""}>{h(label)}</option>'
                          for state, label in _LABELS.items())
        body += (f'<form method="post" action="{h(_url(row["call_sid"]))}" class="card">{ctx.csrf_field()}'
                 f'<input type="hidden" name="revision" value="{workspace.revision(row)}">'
                 '<label for="note">Owner note (up to 4000 characters)</label>'
                 f'<textarea id="note" name="note" rows="5" maxlength="4000" style="width:100%;box-sizing:border-box">{h(row["owner_note"])}</textarea>'
                 f'<label for="state">Follow-up state</label><select id="state" name="state">{options}</select>'
                 f'<label for="due">Due date ({h(ctx.config.timezone)})</label><input id="due" name="due_date" type="date" value="{h(row["followup_due_date"] or "")}">'
                 '<p class="sub">Clear the due date when choosing Handled or No follow-up. Reopen a handled call by choosing Open or Waiting on customer.</p>'
                 '<p><button class="primary" type="submit">Save follow-up</button></p></form>')
    body += '<p><a href="/portal/frontdesk">Back to action queue</a></p>'
    return ctx.page("Follow-up details", body)


@router.get("/portal/frontdesk/call/{call_sid}")
def frontdesk_call(request: Request, call_sid: str) -> Response:
    ctx = portal._require(request)
    if isinstance(ctx, Response):
        return ctx
    if not 1 <= len(call_sid) <= 128:
        return _error("Invalid call identifier.", 400)
    row = workspace.get_call(ctx.client_id, call_sid)
    if row is None:
        return _error("Call not found.", 404)
    return _detail(ctx, row)


@router.post("/portal/frontdesk/call/{call_sid}")
async def frontdesk_save(request: Request, call_sid: str) -> Response:
    ctx = portal._require(request)
    if isinstance(ctx, Response):
        return ctx
    if ctx.sample:
        return _error(portal.SAMPLE_READ_ONLY, 403)
    form = await portal._post_form(request, ctx)
    if isinstance(form, Response):
        return form
    if not 1 <= len(call_sid) <= 128:
        return _error("Invalid call identifier.", 400)
    if workspace.get_call(ctx.client_id, call_sid) is None:
        return _error("Call not found.", 404)
    if not form.get("revision"):
        return _error("Reload the follow-up form before saving.", 400)
    try:
        workspace.save_followup(ctx.client_id, call_sid, note=form.get("note", ""),
                                state=form.get("state", ""), due_date=form.get("due_date", ""),
                                expected_revision=form["revision"])
    except workspace.FollowupConflict as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)
    except LookupError:
        return _error("Call not found.", 404)
    return portal._redirect(_url(call_sid))
