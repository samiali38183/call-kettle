"""Twilio UsageTrigger callback: account-wide spend/flood alarm (docs/TWILIO_USAGE_GUARD.md).

Twilio POSTs here when an account-level usage threshold is crossed. We verify the
Twilio signature and that it is OUR account, record the firing once (Twilio's
IdempotencyToken), and alert the operator through the existing alert path. This
endpoint never changes call handling and never echoes or logs the request body.
"""
from __future__ import annotations

import logging
import os
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import ops, storage, twilio_utils

logger = logging.getLogger("callkettle.usage_trigger")
router = APIRouter()

_SCHEMA = ("CREATE TABLE IF NOT EXISTS twilio_usage_trigger_events "
           "(token TEXT PRIMARY KEY, received_at REAL NOT NULL, category TEXT, recurring TEXT, "
           "trigger_by TEXT, trigger_value TEXT, current_value TEXT)")
_WORD = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_NUM = re.compile(r"^\d{1,12}(\.\d{1,6})?$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.\-:]{1,128}$")


def _public_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return f"{proto}://{host}{path}"


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


@router.post("/ops/twilio-usage-trigger")
async def twilio_usage_trigger(request: Request) -> JSONResponse:
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    if not twilio_utils.validate_signature(url=_public_url(request), form=data, signature=signature):
        logger.warning("Rejected usage-trigger callback: invalid Twilio signature")
        return _fail(403, "forbidden")
    ours = os.environ.get("TWILIO_ACCOUNT_SID", "")
    if not ours or data.get("AccountSid") != ours:
        logger.warning("Rejected usage-trigger callback: not our account")
        return _fail(403, "forbidden")

    category, recurring = data.get("UsageCategory", ""), data.get("Recurring", "") or "alltime"
    trigger_by, trigger_value, current = data.get("TriggerBy", ""), data.get("TriggerValue", ""), data.get("CurrentValue", "")
    token = data.get("IdempotencyToken", "")
    if not (_WORD.match(category) and _WORD.match(recurring) and _WORD.match(trigger_by)
            and _NUM.match(trigger_value) and _NUM.match(current) and _TOKEN.match(token)):
        return _fail(400, "malformed")

    with storage._conn() as conn:
        conn.execute(_SCHEMA)
        cur = conn.execute("INSERT OR IGNORE INTO twilio_usage_trigger_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (token, time.time(), category, recurring, trigger_by, trigger_value, current))
        first = cur.rowcount == 1
    if first:
        unit = "USD" if trigger_by == "price" else ("calls/uses/messages" if trigger_by == "count" else "usage units")
        ops.alert_operator(
            "Twilio account usage threshold crossed",
            f"{recurring} {category} ({trigger_by}) passed {trigger_value} {unit}; current value {current}. "
            "This is ACCOUNT-WIDE Twilio spend. Open console.twilio.com > Monitor > Logs > Calls, look for a flood, "
            "and if it is abuse remove or change the number's voice webhook. See docs/TWILIO_USAGE_GUARD.md.",
            key=f"twilio-usage-trigger-{token}", min_interval=0)
    return JSONResponse({"ok": True, "duplicate": not first})
