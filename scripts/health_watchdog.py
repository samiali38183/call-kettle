"""Read-only Call Kettle health watchdog for Hermes cron.

Print nothing on healthy HTTP 200 + {"status":"ok"}; print a compact alert on
HTTP/network/JSON/status failures. No credentials, customer data, writes or calls.
The Hermes scheduler must be running on this computer for it to fire.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("CALLKETTLE_HEALTH_URL", "https://app.callkettle.com/health")


def check(url: str = URL) -> str:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "CallKettleHealth/1.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                return f"Call Kettle ALERT: health endpoint HTTP {response.status} ({url})"
            payload = json.load(response)
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                return f"Call Kettle ALERT: unexpected health response ({url})"
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return f"Call Kettle ALERT: health request failed: {type(exc).__name__} ({url})"
    return ""


if __name__ == "__main__":
    alert = check()
    if alert:
        print(alert)
        sys.exit(1)
