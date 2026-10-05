"""Headless-browser layout check: loads each URL at phone widths, reports horizontal overflow, console errors and CSP violations,
and saves screenshots.

    backend/.venv/Scripts/python.exe scripts/qa_layout_check.py OUTDIR TAG URL [URL ...] [--widths 320,375,414]

Uses the system Chrome/Edge (marketing/browser_path.py) over the DevTools protocol. No network writes, no form submission.
"""
from __future__ import annotations

import base64
import warnings
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "marketing"))
from browser_path import find_browser  # noqa: E402


warnings.filterwarnings("ignore", category=DeprecationWarning)


class Cdp:
    def __init__(self, ws_url):
        self.ws = connect(ws_url, max_size=64 * 1024 * 1024)
        self.n = 0
        self.events = []

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
            self.events.append(msg)

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.events.append(json.loads(self.ws.recv(timeout=0.3)))
            except TimeoutError:
                pass


def main():
    args = sys.argv[1:]
    widths = [320, 375, 414]
    if "--widths" in args:
        i = args.index("--widths")
        widths = [int(w) for w in args[i + 1].split(",")]
        del args[i:i + 2]
    scroll = None
    if "--scroll" in args:
        i = args.index("--scroll")
        scroll = args[i + 1]
        del args[i:i + 2]
    out, tag, urls = Path(args[0]), args[1], args[2:]
    out.mkdir(parents=True, exist_ok=True)
    port = 9333
    prof = tempfile.mkdtemp(prefix="qa-chrome-")
    proc = subprocess.Popen([find_browser(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
                             "--disable-gpu", "--hide-scrollbars", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.3)
        c = Cdp(page["webSocketDebuggerUrl"])
        c.call("Page.enable"); c.call("Runtime.enable"); c.call("Log.enable")
        failed = False
        for url in urls:
            slug = re.sub(r"[^a-z0-9]+", "_", url.split("//", 1)[-1].lower()).strip("_")
            for w in widths:
                c.events.clear()
                c.call("Emulation.setDeviceMetricsOverride", width=w, height=812, deviceScaleFactor=2, mobile=True)
                c.call("Page.navigate", url=url)
                c.drain(2.5)
                # push the viewport/layout to its limits: scroll to the very bottom so lazy layout settles
                res = c.call("Runtime.evaluate", returnByValue=True, expression="""(() => {
                    const de = document.documentElement, over = [];
                    document.querySelectorAll('body *').forEach(e => { const r = e.getBoundingClientRect(); if (r.width && r.right > __W__ + 1) over.push(e.tagName + '.' + (e.className||'') + ' right=' + Math.round(r.right)); });
                    return {scrollWidth: de.scrollWidth, innerWidth: innerWidth, bodyScrollWidth: document.body.scrollWidth, offenders: over.slice(0, 6)};})()""".replace("__W__", str(w)))["result"]["value"]
                errs = []
                for ev in c.events:
                    if ev.get("method") == "Runtime.exceptionThrown":
                        errs.append("exception: " + ev["params"]["exceptionDetails"].get("text", ""))
                    if ev.get("method") == "Log.entryAdded" and ev["params"]["entry"]["level"] == "error":
                        errs.append(ev["params"]["entry"]["text"][:140] + " " + ev["params"]["entry"].get("url", "")[:80])
                    if ev.get("method") == "Runtime.consoleAPICalled" and ev["params"]["type"] == "error":
                        errs.append("console.error")
                ok = res["scrollWidth"] <= w and res["bodyScrollWidth"] <= w   # compare to the device width: a mobile viewport silently grows to fit overflowing content
                failed |= not ok
                if scroll:
                    c.call("Runtime.evaluate", expression=f"document.querySelector({json.dumps(scroll)}).scrollIntoView({{block:'start'}})")
                    c.drain(0.5)
                shot = c.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
                (out / f"{tag}_{slug}_{w}.png").write_bytes(base64.b64decode(shot))
                print(f"{'PASS' if ok else 'FAIL'} {url} @{w}px scrollWidth={res['scrollWidth']} layoutViewport={res['innerWidth']} offenders={res['offenders'] if not ok else '-'} errors={errs or '-'}")
        sys.exit(1 if failed else 0)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
