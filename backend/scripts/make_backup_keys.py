"""Make the keypair for encrypted offsite backups. Run it ON YOUR OWN COMPUTER, once.

    python scripts/make_backup_keys.py [--out-dir C:\\path\\to\\a\\safe\\folder]

Writes two files: backup_private.pem (KEEP THIS OFFLINE: a password manager or a USB stick, never Fly, never git) and backup_public.pem (safe to put on the server).
The server only ever holds the public key, so a stolen server or storage key cannot decrypt any backup. If you lose the private key, the offsite backups cannot be opened: keep two copies.

Then give the server the public key and the destination (see app/offsite.py):
    flyctl secrets set OFFSITE_BACKUP_PUBLIC_KEY="$(cat backup_public.pem)" OFFSITE_S3_ENDPOINT=... OFFSITE_S3_BUCKET=... OFFSITE_S3_REGION=... \\
        OFFSITE_S3_KEY_ID=... OFFSITE_S3_SECRET=... --app deskline-ai
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import offsite  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=".")
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    priv, pub = offsite.generate_keypair()
    for name in ("backup_private.pem", "backup_public.pem"):
        if (out / name).exists():
            raise SystemExit(f"{out / name} already exists: refusing to overwrite a key")
    (out / "backup_private.pem").write_bytes(priv)
    (out / "backup_public.pem").write_bytes(pub)
    print(f"wrote {out / 'backup_private.pem'} (KEEP OFFLINE) and {out / 'backup_public.pem'} (for the server)")


if __name__ == "__main__":
    main()
