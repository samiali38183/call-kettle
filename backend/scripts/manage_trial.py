"""PRIVATE operator trial controls. Dry-run by default; no billing/provider calls."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage, trial
from app.config import ClientConfig
import yaml


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "status", "stop", "convert"))
    parser.add_argument("client_id")
    parser.add_argument("--db", required=True, help="Explicit target database; never inferred as production")
    parser.add_argument("--config", help="Reviewed YAML actually served for this tenant")
    parser.add_argument("--go-live", help="Agreed ISO timestamp including timezone")
    parser.add_argument("--as-of", help="Status only: ISO timestamp including timezone")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--owner-notified", action="store_true")
    parser.add_argument("--agreement-confirmed", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Explicitly write the named target database")
    args = parser.parse_args(argv)
    previous_db = storage.DB_PATH
    try:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,60}", args.client_id):
            raise ValueError("Invalid client id.")
        storage.DB_PATH = str(Path(args.db).resolve())
        if args.action == "status":
            if args.apply:
                raise ValueError("Status is read-only.")
            now = trial._utc(datetime.fromisoformat(args.as_of)) if args.as_of else None
            result = trial.status(args.client_id, now=now)
        elif args.action == "activate":
            if not args.config or not args.go_live:
                raise ValueError("Activation requires --config and --go-live.")
            # Do not print raw YAML or validation errors: they may contain credentials.
            try:
                cfg = ClientConfig.model_validate(yaml.safe_load(Path(args.config).read_text(encoding="utf-8")))
            except Exception:
                raise ValueError("Reviewed config could not be loaded/validated; inspect it privately.") from None
            if cfg.client_id != args.client_id:
                raise ValueError("Config tenant does not match the named tenant.")
            result = trial.activate(cfg, datetime.fromisoformat(args.go_live), approved_by=args.approved_by,
                                    owner_notified=args.owner_notified, apply=args.apply)
            if args.apply:
                result["verified"] = trial.status(args.client_id)
        else:
            result = trial.transition(args.client_id, "converted" if args.action == "convert" else "stopped",
                                      approved_by=args.approved_by, agreement_confirmed=args.agreement_confirmed, apply=args.apply)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Trial evidence operation failed; inspect database/config privately. Nothing is authorized by this error.", file=sys.stderr)
        return 2
    finally:
        storage.DB_PATH = previous_db


if __name__ == "__main__":
    raise SystemExit(main())
