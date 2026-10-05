"""Turns the QA harness output into the website's demo data (site_calls.json).

    ../../backend/.venv/Scripts/python.exe make_site_calls.py

Picks, for each trade, the standard-caller run that ended cleanly with a booking. These are
simulated test calls (an AI plays the customer against the live receptionist), and the site
labels them that way. Nothing is edited except trimming trailing whitespace.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = {"plumbing": ("Plumbing", 1), "hvac": ("HVAC", 0), "electrical": ("Electrical", 2), "landscaping": ("Landscaping", 3),
          "cleaning": ("Cleaning", 4), "auto": ("Auto repair", 5), "contractor": ("Contractor", 6)}


def main() -> None:
    src = HERE / "qa_standard.json"
    runs = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for r in runs:
        if r["trade"] not in LABELS or r["failures"] or not r["booked"] or not r["ended"]:
            continue
        label, order = LABELS[r["trade"]]
        turns = [{"who": t["who"], "text": t["text"].strip()} for t in r["turns"] if t["text"].strip()]
        out.append({"slug": r["trade"], "label": label, "order": order, "business": r["business"], "turns": turns, "booking": r["booking"]})
    out.sort(key=lambda c: c["order"])
    (HERE / "site_calls.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(out)} calls written:", [c["slug"] for c in out])


if __name__ == "__main__":
    main()
