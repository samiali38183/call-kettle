"""Makes og.png (1200x630), the image shown when the site is shared in a text or social post.

    ../../backend/.venv/Scripts/python.exe make_og.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build  # noqa: E402

from browser_path import find_browser  # noqa: E402

EDGE = find_browser()   # Edge, Chrome, Brave or Chromium on Windows, macOS or Linux (CALLKETTLE_BROWSER overrides)
F = build.load_facts()
brand = F.get("brand", "Call Kettle")

HTML = f"""<!doctype html><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0}}
body{{width:1200px;height:630px;background:radial-gradient(900px 400px at 90% -10%,#1F3B6E,transparent 60%),#0F1B2D;color:#fff;font-family:'Segoe UI',system-ui,sans-serif;padding:64px 72px;display:flex;flex-direction:column;justify-content:space-between}}
.b{{font:700 34px Georgia,serif;display:flex;align-items:center;gap:14px}}.b i{{width:18px;height:18px;border-radius:50%;background:#66E0C0;box-shadow:0 0 0 8px rgba(102,224,192,.18)}}
h1{{font:800 82px/1.02 Georgia,serif;letter-spacing:-2px;max-width:980px}}h1 em{{font-style:normal;color:#8FB2FF}}
.f{{display:flex;justify-content:space-between;align-items:flex-end;font-size:30px;color:#C9D5EA}}
.f b{{color:#fff;font:600 44px Consolas,monospace;white-space:nowrap}}.f>div:first-child{{flex:1}}.f>div:last-child{{white-space:nowrap;text-align:right}}.tag{{font-size:26px;color:#9FB0CC}}
</style><div class="b"><i></i>{brand}</div>
<h1>Keep good calls from <em>going cold.</em></h1>
<div class="f"><div><div class="tag">Overflow and after-hours call coverage</div><div>Call the live demo and try to confuse it</div></div><div>Hear it live<br><b>{F['DEMO_PHONE']}</b></div></div>"""


def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "og.html"
    tmp.write_text(HTML, encoding="utf-8")
    out = HERE / "assets" / "og.png"
    out.unlink(missing_ok=True)
    for _ in range(4):
        profile = tempfile.mkdtemp(prefix="edge-og-")
        subprocess.run([EDGE, "--headless", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}", "--window-size=1200,630",
                        f"--screenshot={out}", tmp.as_uri()], capture_output=True, timeout=90)
        if out.exists() and out.stat().st_size > 5000:
            print("wrote", out, out.stat().st_size, "bytes")
            return
        time.sleep(3)
    raise SystemExit("could not render og.png")


if __name__ == "__main__":
    main()
