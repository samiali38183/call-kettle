"""Start serving a client's config on the live server. No deploy, no restart.

    python scripts/push_config.py acme_plumbing

Reads clients/<id>.yaml from this computer, sends it to the server, which checks
it the same way it checks every config and starts using it immediately. Run it
again after editing the file to change a live client (takes effect at once).
Other clients' calls are never interrupted.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.config import CLIENTS_DIR  # noqa: E402

BASE = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")


def push(client_id: str, *, base: str = BASE, key: str | None = None) -> dict:
    key = key or os.environ.get("REPORT_KEY", "")
    if not key:
        sys.exit("REPORT_KEY isn't set in backend/.env.")
    path = CLIENTS_DIR / f"{client_id}.yaml"
    if not path.exists():
        sys.exit(f"No {path}. Run scripts/onboard_client.py first.")
    r = httpx.post(f"{base}/admin/client/upload", params={"key": key}, content=path.read_bytes(), timeout=30)
    if r.status_code != 200:
        try:
            detail = r.json().get("error", r.text)
        except ValueError:
            detail = r.text
        sys.exit(f"Server rejected {client_id} ({r.status_code}):\n{detail}")
    return r.json()


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    result = push(sys.argv[1])
    verb = "Added" if result["created"] else "Updated"
    print(f"{verb} {result['business_name']} ({result['client_id']}). It is live now.")
    print(f"Their private dashboard: {BASE}{result['dashboard']}")


if __name__ == "__main__":
    main()
