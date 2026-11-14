"""Render docs/img/hero-security-eval.png from the saved evaluation run.

Reads docs/security_eval/results.json (written by `python -m qa.security_eval`) and counts the boundary-removal
tests in backend/tests/test_security_eval.py. Every number in the image comes from those two files.

    pip install matplotlib
    python scripts/make_hero.py
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "security_eval" / "results.json"
TESTS = ROOT / "backend" / "tests" / "test_security_eval.py"
OUT = ROOT / "docs" / "img" / "hero-security-eval.png"

INK, MUTED, LINE = "#111827", "#4b5563", "#d1d5db"
GREEN, RED, AMBER = "#15803d", "#b91c1c", "#b45309"
plt.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans"]


def count_boundary_removals() -> int:
    for node in ast.parse(TESTS.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "MUTATIONS" for t in node.targets):
            assert isinstance(node.value, ast.Dict)
            return len(node.value.keys)
    raise SystemExit("MUTATIONS not found in the test file")


def box(ax, x, y, w, h, fc, ec):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.012", fc=fc, ec=ec, lw=1.6))


def main() -> None:
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    s = data["summary"]
    pi01 = next(c for c in data["cases"] if c["id"] == "PI-01")
    quote = pi01["input"].rsplit(".", 1)[0] + "."  # the saved input is cut at 90 characters; keep whole sentences
    removals = count_boundary_removals()
    allowed = s["tool_calls_attempted"] - s["tool_calls_refused_by_gate"] - s["tool_calls_rejected_by_tool_rules"]

    fig = plt.figure(figsize=(9, 5.6), dpi=200, facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(0.03, 0.93, "Can a caller talk the AI into breaking the rules?", fontsize=19, weight="bold", color=INK)
    ax.text(
        0.03, 0.865, "Every action the AI asks for is checked by plain code that never reads the AI's words.",
        fontsize=11.5, color=MUTED,
    )

    # Left: one real case, top to bottom
    ax.text(0.03, 0.795, "One real case from the run (%s)" % pi01["id"], fontsize=11.5, weight="bold", color=MUTED)
    box(ax, 0.03, 0.605, 0.50, 0.16, "#f3f4f6", LINE)
    ax.text(0.045, 0.735, "1  Caller says", fontsize=10.5, weight="bold", color=MUTED, va="top")
    ax.text(0.045, 0.705, textwrap.fill('"%s"' % quote, 52), fontsize=10.5, color=INK, va="top", linespacing=1.25)
    ax.annotate("", xy=(0.28, 0.515), xytext=(0.28, 0.605), arrowprops=dict(arrowstyle="-|>", color=MUTED, lw=1.8))
    box(ax, 0.03, 0.425, 0.50, 0.09, "#f3f4f6", LINE)
    ax.text(0.045, 0.497, "2  The AI (scripted to obey) asks to", fontsize=10.5, weight="bold", color=MUTED, va="top")
    ax.text(0.045, 0.462, "cancel another customer's booking", fontsize=10.5, color=INK, va="top")
    ax.annotate("", xy=(0.28, 0.33), xytext=(0.28, 0.425), arrowprops=dict(arrowstyle="-|>", color=MUTED, lw=1.8))
    box(ax, 0.03, 0.18, 0.50, 0.15, "#fef2f2", RED)
    ax.text(0.045, 0.31, "3  Server check says", fontsize=10.5, weight="bold", color=RED, va="top")
    ax.text(0.045, 0.275, "REFUSED", fontsize=19, weight="bold", color=RED, va="top")
    ax.text(0.22, 0.282, "rule: %s" % pi01["actual_control"], fontsize=10.5, color=INK, va="top")
    ax.text(0.22, 0.252, "The booking stays confirmed.", fontsize=10.5, color=INK, va="top")

    # Right: tool-call breakdown
    rx, rw = 0.58, 0.39
    ax.text(rx, 0.795, "All %d actions the scripted AI tried" % s["tool_calls_attempted"], fontsize=11.5,
            weight="bold", color=MUTED)
    parts = [
        (s["tool_calls_refused_by_gate"], RED, "refused by the gate"),
        (s["tool_calls_rejected_by_tool_rules"], AMBER, "rejected by the tool's own rules"),
        (allowed, GREEN, "allowed (normal use)"),
    ]
    x = rx
    for n, color, _ in parts:
        w = rw * n / s["tool_calls_attempted"]
        ax.add_patch(plt.Rectangle((x, 0.69), w, 0.07, fc=color, ec="white", lw=1.5))
        ax.text(x + w / 2, 0.725, str(n), color="white", fontsize=14, weight="bold", ha="center", va="center")
        x += w
    ly = 0.655
    for n, color, label in parts:
        ax.add_patch(plt.Rectangle((rx, ly - 0.012), 0.022, 0.026, fc=color, ec="none"))
        ax.text(rx + 0.035, ly, label, fontsize=10.5, color=INK, va="center")
        ly -= 0.04

    tiles = [
        ("%d of %d" % (s["passed"], s["cases"]), "cases pass (%d attacks, %d normal-use)" % (s["attack_cases"], s["benign_cases"])),
        (str(removals), "tests remove one safety check and must see cases fail"),
        (str(s["network_attempts"]), "network connections during the run"),
        (str(s["audit_leaks"]), "audit rows holding a name or phone number"),
    ]
    ty = 0.50
    for big, small in tiles:
        ax.text(rx, ty, big, fontsize=19, weight="bold", color=INK, va="top")
        ax.text(rx + 0.15, ty - 0.012, textwrap.fill(small, 30, break_on_hyphens=False), fontsize=10.5, color=INK, va="top", linespacing=1.2)
        ty -= 0.095

    ax.plot([0.03, 0.97], [0.115, 0.115], color=LINE, lw=1)
    ax.text(
        0.03, 0.075,
        "The AI in this run is a script, so it shows what the server does when the model misbehaves, not how often a real\n"
        "model would. Synthetic tenants, no live model, no production traffic.",
        fontsize=10, color=MUTED, va="center", linespacing=1.3,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=200, facecolor="white")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
