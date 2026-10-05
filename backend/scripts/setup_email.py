"""Turn on booking emails, calendar invites and the weekly recap, in one step.

    python scripts/setup_email.py

Before you run it, make a Gmail "app password" (2 minutes, one time):
  1. https://myaccount.google.com/security  -> turn on 2-Step Verification if it's off
  2. https://myaccount.google.com/apppasswords  -> name it "Call Kettle" -> Create
  3. Copy the 16-letter password Google shows.

This script then: checks the password really works, emails you a test message,
and only then saves it to the server. Nothing is stored on this computer.
"""
from __future__ import annotations

import argparse
import getpass
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

HOST, PORT = "smtp.gmail.com", 587
FLY_APP = "deskline-ai"


def clean(password: str) -> str:
    """Google shows app passwords as 'abcd efgh ijkl mnop'; the spaces aren't part of it."""
    return "".join(password.split())


def check_login_and_send_test(email: str, password: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"] = email, email
    msg["Subject"] = "Call Kettle email is working"
    msg.set_content(
        "If you can read this, Call Kettle can email you bookings, callbacks and weekly recaps.\n"
        "Clients you add later get the same emails at the address in their config."
    )
    with smtplib.SMTP(HOST, PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(email, password)
        smtp.send_message(msg)


def secrets_payload(email: str, password: str) -> str:
    return (
        f"SMTP_HOST={HOST}\nSMTP_PORT={PORT}\nSMTP_USER={email}\nSMTP_FROM={email}\nSMTP_PASSWORD={password}\n"
    )


def find_flyctl() -> str:
    found = shutil.which("flyctl") or shutil.which("fly")
    if found:
        return found
    fallback = Path.home() / ".fly" / "bin" / "flyctl.exe"
    if fallback.exists():
        return str(fallback)
    sys.exit("Couldn't find flyctl. Install it from https://fly.io/docs/flyctl/install/ and log in.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", default="samiali38183@gmail.com", help="the Gmail address that sends the emails")
    args = parser.parse_args()

    password = clean(getpass.getpass(f"Paste the 16-letter app password for {args.email} (typing is hidden): "))
    if len(password) != 16:
        sys.exit("That isn't 16 letters. Copy the password from https://myaccount.google.com/apppasswords "
                 "(not your normal Gmail password).")
    try:
        check_login_and_send_test(args.email, password)
    except smtplib.SMTPAuthenticationError:
        sys.exit("Google rejected that password. Make sure 2-Step Verification is on and you made an "
                 "APP password (not your normal one). Nothing was changed.")
    except Exception as exc:  # network, DNS, etc.
        sys.exit(f"Couldn't reach Gmail ({exc}). Nothing was changed.")
    print(f"Password works. A test email is on its way to {args.email}.")

    result = subprocess.run(
        [find_flyctl(), "secrets", "import", "--app", FLY_APP],
        input=secrets_payload(args.email, password), text=True,
    )
    if result.returncode != 0:
        sys.exit("Saving to the server failed (see above). Make sure you're logged in: flyctl auth login")
    print("\nDone. The server restarts in a few seconds (calls ring your phone during that blip).")
    print("Booking emails, calendar invites and Monday recaps are now on for clients that have an owner_email.")


if __name__ == "__main__":
    main()
