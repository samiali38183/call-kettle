"""Print the owner's weekly recap for a client exactly as it would be composed now. Reads the database; sends nothing and marks nothing sent.

    python scripts/digest_preview.py sample_homecare
    python scripts/digest_preview.py sample_homecare --as-of 2026-10-12     # the recap that would go out that Monday

The recap covers the last FULL Monday-to-Sunday week before the given day, so calls from this week only show with --as-of next Monday.
Uses CALLKETTLE_DB_PATH if set (point it at a copy of the production database to preview real data).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import digest  # noqa: E402
from app.config import ClientNotFoundError  # noqa: E402


def main(argv: list[str]) -> int:
    args = list(argv)
    now = None
    if "--as-of" in args:
        i = args.index("--as-of")
        try:
            now = datetime.strptime(args[i + 1], "%Y-%m-%d").replace(hour=13, tzinfo=timezone.utc)   # 13:00 UTC = morning in the US
        except (IndexError, ValueError):
            print("--as-of needs a date like 2026-10-12")
            return 2
        del args[i:i + 2]
    if len(args) != 1:
        print(__doc__)
        return 2
    try:
        subject, body = digest.preview(args[0], now)
    except ClientNotFoundError:
        print(f"No client config named {args[0]!r}.")
        return 1
    print(f"Subject: {subject}\n\n{body}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
