"""Create (or reset) a customer's owner-portal login. Operator tool; run it where the database lives.

    python scripts/create_owner_user.py <client_id> <owner_email>
    python scripts/create_owner_user.py --reset <owner_email>

Prints a ONE-TIME temporary password to this terminal and nowhere else: it is not logged and only its scrypt hash is stored. The owner
must choose their own password at first sign-in. Send the password by a different channel than the portal address (a call or text).
Database: CALLKETTLE_DB_PATH (same as the server). This script does not read backend/.env.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import owner_auth, storage  # noqa: E402
from app.config import ClientNotFoundError, load_client_config  # noqa: E402


def main(argv: list[str]) -> int:
    args = list(argv)
    reset = "--reset" in args
    args = [a for a in args if a != "--reset"]
    if (reset and len(args) != 1) or (not reset and len(args) != 2):
        print(__doc__)
        return 2
    storage.init_db()
    try:
        if reset:
            email = args[0]
            temp = owner_auth.reset_user(email)
        else:
            client_id, email = args
            try:
                load_client_config(client_id)
            except (ClientNotFoundError, Exception):
                print(f"Unknown client id: {client_id}")
                return 1
            temp = owner_auth.create_user(client_id, email)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Owner login for {owner_auth.normalize_email(email)}")
    print(f"Temporary password (shown once, change required at first sign-in): {temp}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
