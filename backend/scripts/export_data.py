"""Download a copy of ALL Call Kettle data to your own computer.

    python scripts/export_data.py

Saves a dated JSON file into C:\\Users\\<you>\\Documents\\CallKettle-Backups. Run it
weekly, and before any risky change. (The server also keeps its own daily
backups and Fly snapshots the disk daily; this copy lives on your machine.)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
base = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
key = os.environ.get("REPORT_KEY")
if not key:
    sys.exit("REPORT_KEY isn't set in backend/.env")

r = httpx.get(f"{base}/admin/export", params={"key": key}, timeout=120)
if r.status_code != 200:
    sys.exit(f"Export failed: HTTP {r.status_code}")
data = r.json()

folder = Path.home() / "Documents" / "CallKettle-Backups"
folder.mkdir(parents=True, exist_ok=True)
path = folder / f"callkettle-{datetime.now().strftime('%Y-%m-%d_%H%M')}.json"
path.write_text(json.dumps(data, indent=2), encoding="utf-8")
print(f"Saved {path}")
print({k: len(v) for k, v in data.items() if isinstance(v, list)})
