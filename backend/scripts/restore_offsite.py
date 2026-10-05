"""Rehearse (or perform) a restore from the encrypted offsite backups. Reads from the destination, decrypts with YOUR private key, and checks the result.

    python scripts/restore_offsite.py --private-key C:\\safe\\backup_private.pem --out restored.db

It never touches the live database: it writes a file you inspect first. Destination settings come from the same OFFSITE_* environment variables as the server
(put them in backend/.env on your own computer). Do this once a quarter, time it, and write the time in docs/PLATFORM_NOTES.md: a backup you have not restored is a hope.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app import offsite  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--private-key", required=True)
    ap.add_argument("--out", default="restored.db")
    a = ap.parse_args()
    dest = offsite.configured_destination()
    if dest is None:
        raise SystemExit("No destination configured: set OFFSITE_BACKUP_DIR or the OFFSITE_S3_* variables.")
    started = time.time()
    info = offsite.restore_latest(dest, Path(a.private_key).read_bytes(), Path(a.out))
    conn = sqlite3.connect(f"file:{a.out}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    finally:
        conn.close()
    print(f"restored {info['object']} ({info['bytes']:,} bytes) in {time.time() - started:.1f}s; integrity_check: {integrity}")
    print("rows:", ", ".join(f"{t}={n}" for t, n in sorted(tables.items())))
    if integrity != "ok":
        raise SystemExit("INTEGRITY CHECK FAILED: do not use this file")


if __name__ == "__main__":
    main()
