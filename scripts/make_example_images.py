"""Render the recorded example as images, from the saved run only.

Reads docs/security_eval/recorded_example.json and recorded_example.txt (written by `python -m qa.record_example`) and writes:

    docs/img/example-flow.svg        legitimate vs adversarial request through the trust boundary
    docs/img/example-terminal.svg    the saved terminal output
    docs/img/example-terminal.png    the same, rasterised with Pillow (Consolas if present, else DejaVu Sans Mono)

    python scripts/make_example_images.py

No numbers or text in the images are typed here except labels; the values come from the two saved files.
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "security_eval"
OUT = ROOT / "docs" / "img"

INK, MUTED, LINE, BG = "#111827", "#4b5563", "#cbd5e1", "#ffffff"
GREEN, RED = "#15803d", "#b91c1c"
UNTRUSTED_FILL, TRUSTED_FILL = "#fff7ed", "#f0f9ff"
UNTRUSTED_EDGE, TRUSTED_EDGE = "#c2410c", "#0369a1"
FONT = "font-family=\"Segoe UI, Helvetica, Arial, sans-serif\""


def _lines(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width, break_long_words=False) or [""]


def _t(x: float, y: float, text: str, size: int = 13, weight: str = "normal", fill: str = INK, anchor: str = "start") -> str:
    return f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" {FONT}>{escape(text)}</text>'


def _box(x, y, w, h, title, body: list[str], edge=LINE, fill="#ffffff", dashed=False, title_fill=MUTED) -> str:
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{edge}" stroke-width="1.6"{dash}/>',
             _t(x + 12, y + 22, title, 12, "bold", title_fill)]
    for i, line in enumerate(body):
        parts.append(_t(x + 12, y + 44 + i * 18, line, 13))
    return "\n".join(parts)


def _arrow(x1, y1, x2, y2, color=MUTED, dashed=False) -> str:
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2 - 6}" y2="{y2}" stroke="{color}" stroke-width="2"{dash} marker-end="url(#ah)"/>'


def _args_summary(tool: str, args: dict) -> list[str]:
    parts = []
    for k, v in args.items():
        if k in ("caller_name", "caller_phone"):
            continue
        parts.append(f"{k}={v}")
    lines = [f"{tool}("]
    lines += ["  " + p for p in parts[:4]] if parts else ["  (no arguments)"]
    if "caller_name" in args:
        lines.insert(1, "  caller_name, caller_phone,")
    lines.append(")")
    return lines


def flow_svg(records: list[dict]) -> str:
    legit, adv = records[0], records[1]
    W, H = 1300, 620
    colx = [30, 275, 560, 805, 1050]       # caller, model, gate, tools, audit+state
    cw = [225, 235, 225, 225, 225]
    lanes_y = [172, 390]
    lane_h = 172
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
         f'aria-label="A legitimate and an adversarial request through the trust boundary: caller input, model proposal, authorization gate, business tools, audit record and resulting state">',
         '<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
         f'<path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker></defs>',
         f'<rect width="{W}" height="{H}" fill="{BG}"/>',
         _t(30, 38, "One legitimate and one adversarial request, recorded from the evaluation harness", 20, "bold"),
         _t(30, 62, "Scripted model, synthetic tenants, no network. The same agent loop, gate, tools and SQLite database as a call.", 13, "normal", MUTED)]
    # zones
    o.append(f'<rect x="18" y="82" width="505" height="500" rx="12" fill="{UNTRUSTED_FILL}" stroke="{UNTRUSTED_EDGE}" stroke-width="1.5" stroke-dasharray="8 5"/>')
    o.append(_t(34, 106, "UNTRUSTED INPUT: can only suggest", 13, "bold", UNTRUSTED_EDGE))
    o.append(f'<rect x="540" y="82" width="745" height="500" rx="12" fill="{TRUSTED_FILL}" stroke="{TRUSTED_EDGE}" stroke-width="1.5"/>')
    o.append(_t(556, 106, "TRUSTED: server code and policy decide", 13, "bold", TRUSTED_EDGE))
    heads = ["1  Caller says", "2  Agent proposes", "3  Authorization layer", "4  Business tools", "5  Audit record and state"]
    for x, w, h in zip(colx, cw, heads):
        o.append(_t(x + 2, 136, h, 14, "bold"))
    for lane, rec in zip(lanes_y, (legit, adv)):
        refused = rec["gate"]["decision"] == "refused"
        accent = RED if refused else GREEN
        o.append(_t(30, lane - 10, rec["label"] + f"  (case {rec['case']})", 13, "bold", accent))
        quote = _lines('"' + rec["caller_says"] + '"', 29)
        o.append(_box(colx[0], lane, cw[0], lane_h, "caller speech", quote))
        prop = rec["model_proposes"]
        o.append(_box(colx[1], lane, cw[1], lane_h, "tool call the model asks for", _args_summary(prop["tool"], prop["arguments"])))
        pol = rec["policy_overrides"]
        pol_line = ", ".join(f"{k}={str(v).lower()}" for k, v in pol.items()) if pol else "all tools on"
        gate_body = ["", f"control: {rec['gate']['control']}", "", "tenant policy:", *(_lines(pol_line, 27))]
        o.append(_box(colx[2], lane, cw[2], lane_h, "authorize_tool_call", gate_body, edge=accent, fill="#ffffff", title_fill=accent))
        res = rec["tool_result"]
        if refused:
            tool_body = ["not reached", "", "the model was only told", "the action is not available"]
            o.append(_box(colx[3], lane, cw[3], lane_h, "tools.py rules and SQLite", tool_body, dashed=True))
        else:
            tool_body = ["ran: service on list,", "slot on grid, lead time,", "no overlap", "", "result: " + ("saved" if isinstance(res, dict) and res.get("success") else "rejected")]
            o.append(_box(colx[3], lane, cw[3], lane_h, "tools.py rules and SQLite", tool_body))
        b4, b5 = len(rec["bookings_before"]), len(rec["bookings_after"])
        aud = rec["audit_row"]
        state = ["audit: " + aud["decision"], "personal args as length only", "", f"bookings: {b4} before, {b5} after"]
        state += _lines("booking changed: new row" if b5 > b4 else "no booking row changed", 27)
        if refused:
            state += _lines("victim booking still confirmed", 27)
        o.append(_box(colx[4], lane, cw[4], lane_h, "tool_audit and bookings", state, edge=accent))
        # coloured verdict text inside the gate box
        o.append(_t(colx[2] + 12, lane + 44, "ALLOWED" if not refused else "REFUSED", 17, "bold", accent))
        mid = lane + lane_h / 2
        for i in range(4):
            o.append(_arrow(colx[i] + cw[i], mid, colx[i + 1], mid, MUTED, dashed=(refused and i == 2)))
    o.append(_t(30, 604, "Source: docs/security_eval/recorded_example.json, produced by python -m qa.record_example.", 12, "normal", MUTED))
    o.append("</svg>")
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------------------------------------------- terminal
TOKEN = re.compile(r"(ALLOWED|REFUSED|held|VIOLATED)")
COLORS = {"ALLOWED": "#4ade80", "REFUSED": "#f87171", "held": "#4ade80", "VIOLATED": "#f87171"}
T_BG, T_FG, T_DIM, T_HEAD = "#0f172a", "#e2e8f0", "#94a3b8", "#7dd3fc"


def _segments(line: str) -> list[tuple[str, str]]:
    if line.startswith("$"):
        return [(line, T_DIM)]
    if line.startswith("=="):
        return [(line, T_HEAD)]
    out = []
    for part in TOKEN.split(line):
        if part:
            out.append((part, COLORS.get(part, T_FG)))
    return out


def terminal_svg(text: str) -> str:
    lines = text.rstrip("\n").split("\n")
    cols = max(len(line) for line in lines)
    cw, lh, pad = 8.4, 20, 22
    W, H = int(cols * cw + pad * 2), int(len(lines) * lh + pad * 2 + 34)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Terminal output of python -m qa.record_example">',
         f'<rect width="{W}" height="{H}" rx="10" fill="{T_BG}"/>',
         '<circle cx="22" cy="20" r="6" fill="#f87171"/><circle cx="42" cy="20" r="6" fill="#fbbf24"/><circle cx="62" cy="20" r="6" fill="#4ade80"/>']
    for i, line in enumerate(lines):
        y = 54 + pad / 2 + i * lh
        spans = "".join(f'<tspan fill="{c}">{escape(s)}</tspan>' for s, c in _segments(line))
        o.append(f'<text x="{pad}" y="{y}" font-size="14" font-family="Consolas, Menlo, \'DejaVu Sans Mono\', monospace" xml:space="preserve">{spans}</text>')
    o.append("</svg>")
    return "\n".join(o) + "\n"


def terminal_png(text: str, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for cand in ("C:/Windows/Fonts/consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "DejaVuSansMono.ttf"):
        try:
            font = ImageFont.truetype(cand, 30)
            break
        except OSError:
            continue
    if font is None:
        raise SystemExit("no monospace font found for the PNG")
    lines = text.rstrip("\n").split("\n")
    cw = font.getlength("M")
    lh, pad, top = 44, 44, 90
    W, H = int(max(len(line) for line in lines) * cw + pad * 2), int(len(lines) * lh + top + pad)
    img = Image.new("RGB", (W, H), T_BG)
    d = ImageDraw.Draw(img)
    for i, c in enumerate(("#f87171", "#fbbf24", "#4ade80")):
        d.ellipse((pad + i * 44, 28, pad + i * 44 + 24, 52), fill=c)
    for i, line in enumerate(lines):
        x = pad
        for seg, color in _segments(line):
            d.text((x, top + i * lh), seg, font=font, fill=color)
            x += font.getlength(seg)
    img.save(path, optimize=True)


def main() -> None:
    records = json.loads((SRC / "recorded_example.json").read_text(encoding="utf-8"))
    text = (SRC / "recorded_example.txt").read_text(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "example-flow.svg").write_text(flow_svg(records), encoding="utf-8")
    (OUT / "example-terminal.svg").write_text(terminal_svg(text), encoding="utf-8")
    terminal_png(text, OUT / "example-terminal.png")
    print("wrote", OUT / "example-flow.svg", OUT / "example-terminal.svg", OUT / "example-terminal.png")


if __name__ == "__main__":
    main()
