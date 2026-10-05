"""Talk to the real AI receptionist from the terminal — the exact agent.py /
tools.py code path a live phone call runs, just typed instead of spoken.

Usage:
    python simulate_call.py [client_id]

Requires ANTHROPIC_API_KEY to be set (real API calls, real cost, real AI).
Twilio SMS calls are skipped gracefully if Twilio env vars aren't set —
you'll just see a log line instead of a text going out.
"""
from __future__ import annotations

import os
import sys
import uuid

# Windows terminals often fall back to a legacy codepage for stdout, which
# renders em-dashes and other punctuation in the AI's replies as garbled
# characters — harmless to the data, but this tool exists specifically to be
# shown to a real prospect's face, so it has to look clean.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ANTHROPIC_API_KEY is not set. Set it in your environment or .env and try again.")
    sys.exit(1)

os.environ.setdefault("CALLKETTLE_DB_PATH", "./callkettle_sim.db")

from app import agent, storage
from app.config import ClientNotFoundError, load_client_config


def main() -> None:
    client_id = sys.argv[1] if len(sys.argv) > 1 else "demo_dental"
    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        print(f"No client config found for '{client_id}' (looked in clients/{client_id}.yaml)")
        sys.exit(1)

    storage.init_db()
    call_sid = f"SIM-{uuid.uuid4().hex[:8]}"
    storage.log_call_start(call_sid, client_id, "+15555550199")
    session = agent.start_session(call_sid, config)

    print(f"\n--- Calling {config.business_name} ({client_id}) --- (type 'hangup' to end)\n")
    print(f"AI: {config.opening_line}\n")

    try:
        while True:
            caller_text = input("You: ").strip()
            if not caller_text:
                continue
            if caller_text.lower() in {"hangup", "quit", "exit"}:
                print("\n--- call ended by you ---\n")
                break

            reply, should_end, transfer_to = agent.run_turn(session, caller_text)
            print(f"\nAI: {reply}\n")

            if transfer_to:
                print(f"--- call transferred live to {transfer_to} ---\n")
                break
            if should_end:
                print("--- call ended by the AI ---\n")
                break
    except (KeyboardInterrupt, EOFError):
        print("\n--- call dropped ---\n")
    finally:
        storage.log_call_end(call_sid, "sim_ended")
        agent.end_session(call_sid)
        print(f"Call logged as {call_sid} in {os.environ['CALLKETTLE_DB_PATH']}")


if __name__ == "__main__":
    main()
