"""After a call ends, write a 2-3 sentence recap the owner can read in the
dashboard. Runs off the call path (a background task), fails soft, and is
skipped entirely for clients who don't record transcripts."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from app import agent, notify, storage
from app.config import ClientNotFoundError, load_client_config

logger = logging.getLogger("callkettle.summary")

_PROMPT = (
    "Summarize this phone call to a small business in 2-3 plain sentences for the owner: who called "
    "(if they gave a name), what they wanted, what the AI did (booked, took a message, transferred), "
    "and whether the owner needs to follow up. Do not include medical or financial details. "
    "Output only the summary."
)


def _emit_completed(call: dict, config) -> None:
    """call.completed: once per call (the event id is the call id, so the several end-of-call code paths cannot duplicate it)."""
    from app import webhooks

    webhooks.emit(config, "call.completed", {
        "call_sid": call["call_sid"], "from": call["from_number"], "started_at": call["started_at"], "ended_at": call["ended_at"],
        "outcome": call["outcome"], "turns": call["turn_count"], "outcome_class": storage.get_call_class(call["call_sid"]),
    }, event_id=f"call.completed:{config.client_id}:{call['call_sid']}")


DEMO_ALERTS_PER_DAY = 15


def _demo_call_alert(call: dict, config) -> None:
    """Someone who is not the owner just had a real conversation with a public demo. For a founder-led sales motion that is the most
    valuable signal there is (a prospect trying the product), so the operator is told who, which demo, and how it went.
    Capped per day so a flood of junk calls cannot become a flood of alerts. Never raises."""
    try:
        if not config.demo_mode or config.demo_menu:
            return
        caller = call.get("from_number") or ""
        if caller and caller[-10:] == (config.escalation_phone or "")[-10:]:
            return                                              # the owner testing his own demo
        digits = "".join(ch for ch in caller if ch.isdigit())[-10:]
        if config.client_id.startswith("zz_") or digits[:3] == "555" or digits[3:6] == "555":
            return                                              # our own synthetic test callers (555 numbers, throwaway zz_ clients), never a prospect
        turns = [t for t in json.loads(call.get("transcript_json") or "[]") if t.get("role") == "caller"]
        if not turns:
            return
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        if storage.metric_totals(since).get("demo_call_alert", 0) >= DEMO_ALERTS_PER_DAY:
            return
        storage.record_metric("demo_call_alert", config.client_id)
        target = config
        if not (config.ntfy_topic or config.owner_email):               # a private prospect demo has no channels of its own: tell the operator
            from app import ops

            target = load_client_config(ops.OPERATOR_CLIENT_ID)
        notify.notify_owner(
            target,
            title="Someone tried a demo line",
            body=f"{caller or 'Unknown number'} spoke to {config.business_name} for {len(turns)} turn(s) ({storage.get_call_class(call['call_sid']) or 'unclassified'}). "
                 "If this is a prospect, call them back today while it is fresh.",
        )
    except Exception:
        logger.exception("Demo call alert failed for %s", call.get("call_sid"))


def summarize_call(call_sid: str) -> None:
    try:
        call = storage.get_call(call_sid)
        if call is None or call["summary"]:
            return
        try:
            config = load_client_config(call["client_id"])
        except ClientNotFoundError:
            return
        try:
            storage.classify_and_store(call_sid)
        except Exception:
            logger.exception("Could not classify call %s", call_sid)
        _emit_completed(call, config)
        _demo_call_alert(call, config)
        if not config.record_transcripts:
            return
        transcript = json.loads(call["transcript_json"])
        caller_turns = [t for t in transcript if t.get("role") == "caller"]
        if not caller_turns:
            storage.set_call_summary(call_sid, "Caller hung up without saying anything.")
            return
        lines = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript)
        started = time.perf_counter()
        response = agent._anthropic_client().messages.create(
            model=config.model,
            max_tokens=200,
            system=_PROMPT,
            messages=[{"role": "user", "content": lines}],
        )
        agent.record_usage(call_sid, response, started, tenant=call["client_id"])
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text:
            storage.set_call_summary(call_sid, text)
    except Exception:
        logger.exception("Call summary failed for %s — non-fatal", call_sid)
