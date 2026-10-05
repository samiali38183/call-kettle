#!/usr/bin/env bash
# One-time setup of the Call Kettle workstation on a Mac (also works on Linux).
#   git clone owner@example.com:<you>/callkettle.git && cd callkettle && ./setup_mac.sh
# Then put your secrets in backend/.env (see docs/MAC_SETUP.md; never commit that file).
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n== %s\n' "$*"; }

say "Checking tools"
command -v python3 >/dev/null || { echo "Install Python 3.12+ (brew install python@3.12)"; exit 1; }
command -v git >/dev/null || { echo "Install git (xcode-select --install)"; exit 1; }
for t in node flyctl vercel gh; do command -v "$t" >/dev/null || echo "  missing: $t  (brew install node flyctl gh; npm i -g vercel)"; done

say "Python environment"
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt

say "Secrets file"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  created backend/.env from the template: fill in the real values (Anthropic key, Twilio, REPORT_KEY, SMTP...)."
else
  echo "  backend/.env already exists"
fi

say "Quick check (no network, no secrets needed)"
.venv/bin/python -m pytest -q -x 2>&1 | tail -3 || true
cd ..
echo
echo "Done. Next: docs/MAC_SETUP.md section 4 (log in to GitHub, Fly, Vercel) and section 5 (the daily commands on a Mac)."
