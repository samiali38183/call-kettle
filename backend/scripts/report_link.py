"""Print a client's private dashboard link (or all of them).

    python scripts/report_link.py sample_homecare
    python scripts/report_link.py --all
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

if not os.environ.get("REPORT_KEY"):
    sys.exit("REPORT_KEY isn't set in backend/.env (it must match the REPORT_KEY secret on Fly).")

from app import main  # noqa: E402
from app.config import CLIENTS_DIR  # noqa: E402

BASE = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")


def link(client_id: str) -> str:
    return f"{BASE}/report/{client_id}?key={main.report_key_for(client_id)}"


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--all"]:
        for path in sorted(CLIENTS_DIR.glob("*.yaml")):
            if not path.stem.startswith("_"):
                print(f"{path.stem:22s} {link(path.stem)}")
        print(f"\nAll clients (operator view): {BASE}/admin?key={os.environ['REPORT_KEY']}")
    elif len(args) == 1:
        print(link(args[0]))
    else:
        sys.exit(__doc__)
