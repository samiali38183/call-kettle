"""Checks the built site: internal links resolve, external links respond, each page has a unique title,
a description of sensible length, exactly one h1, canonical + sitemap entries, and stays light.

    ../../backend/.venv/Scripts/python.exe check_site.py          # exit 1 on any problem
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import httpx

SITE = Path(__file__).resolve().parent.parent / "site"
problems: list[str] = []
titles: dict[str, str] = {}


def page_path(url_path: str) -> Path | None:
    p = url_path.strip("/")
    for cand in ([SITE / p / "index.html", SITE / f"{p}.html", SITE / p] if p else [SITE / "index.html"]):
        if cand.is_file():
            return cand
    return None


def main() -> None:
    pages = sorted(SITE.rglob("*.html"))
    external: set[str] = set()
    for f in pages:
        rel = f.relative_to(SITE).as_posix()
        html = f.read_text(encoding="utf-8")
        if rel == "404.html":
            continue
        t = re.search(r"<title>(.*?)</title>", html, re.S)
        d = re.search(r'<meta name="description" content="(.*?)">', html, re.S)
        h1s = re.findall(r"<h1[ >]", html)
        if not t or not t.group(1).strip():
            problems.append(f"{rel}: missing <title>")
        else:
            if t.group(1) in titles:
                problems.append(f"{rel}: duplicate title with {titles[t.group(1)]}")
            titles[t.group(1)] = rel
            if len(t.group(1)) > 75:
                problems.append(f"{rel}: title is {len(t.group(1))} chars (keep under ~75)")
        if not d or not (60 <= len(d.group(1)) <= 175):
            problems.append(f"{rel}: description missing or not 60-175 chars ({len(d.group(1)) if d else 0})")
        if len(h1s) != 1:
            problems.append(f"{rel}: {len(h1s)} <h1> tags (want exactly 1)")
        if 'rel="canonical"' not in html:
            problems.append(f"{rel}: no canonical link")
        if 'lang="en"' not in html:
            problems.append(f"{rel}: no lang attribute")
        ids = set(re.findall(r'id="([^"]+)"', html))
        for href in re.findall(r'href="([^"]+)"', html):
            if href.startswith(("tel:", "mailto:", "data:")):
                continue
            if href.startswith("#"):
                if href[1:] and href[1:] not in ids:
                    problems.append(f"{rel}: anchor {href} has no target")
                continue
            u = urlparse(href)
            if u.scheme in ("http", "https"):
                if u.netloc != urlparse(re.search(r'rel="canonical" href="([^"]+)"', html).group(1)).netloc:
                    external.add(urldefrag(href)[0])
                continue
            target = page_path(u.path or "/")
            if target is None and not (SITE / u.path.lstrip("/")).exists():
                problems.append(f"{rel}: broken internal link {href}")
        size = len(html.encode())
        if size > 120_000:
            problems.append(f"{rel}: {size:,} bytes of HTML (heavy)")
    for asset in ("assets/style.css", "assets/call-demo.js"):
        if not (SITE / asset).exists():
            problems.append(f"missing {asset}")
    css, js = (SITE / "assets/style.css").stat().st_size, (SITE / "assets/call-demo.js").stat().st_size
    print(f"{len(pages)} pages; css {css:,} B, js {js:,} B")
    sm = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    for f in pages:
        rel = f.relative_to(SITE).as_posix()
        if rel == "404.html":
            continue
        slug = "/" + rel.removesuffix("index.html").rstrip("/")
        if slug != "/" and f"{slug}</loc>" not in sm:
            problems.append(f"{rel}: not in sitemap.xml")
    for url in sorted(external):
        if "fonts.g" in url:
            continue
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            if r.status_code >= 400:
                problems.append(f"external link {url} -> HTTP {r.status_code}")
        except Exception as exc:
            if url.split("/")[2].endswith("callkettle.com") and not os.environ.get("BRAND_DOMAIN_LIVE"):
                print(f"  (skipped {url}: the new domain is not live yet; set BRAND_DOMAIN_LIVE=1 on switch day to enforce this)")
                continue
            problems.append(f"external link {url} failed: {type(exc).__name__}")
    print(f"{len(external)} external links checked")
    if problems:
        print("\nPROBLEMS:")
        print("\n".join(" - " + p for p in problems))
        sys.exit(1)
    print("Site checks passed.")


if __name__ == "__main__":
    main()
