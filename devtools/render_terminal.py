"""Render captured terminal output as a PNG for the README (Playwright).

    python devtools/render_terminal.py OUT.png "TITLE" "$ command" FILE [ "$ command" FILE ... ]
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

CSS = """
body{margin:0;background:#f4f0e6;font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace}
.win{margin:28px;border-radius:12px;background:#171511;border:1px solid #2c2820;overflow:hidden;width:1040px}
.bar{display:flex;align-items:center;gap:8px;padding:12px 16px;background:#1f1c17;border-bottom:1px solid #2c2820}
.dot{width:11px;height:11px;border-radius:50%;background:#3a352c}.title{margin-left:10px;color:#a39b8b;font:500 13px 'IBM Plex Sans',sans-serif}
pre{margin:0;padding:18px 22px;color:#e9e2d3;font-size:13.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.cmd{color:#f0644a}.ok{color:#7fbf93}.h{color:#f0644a;font-weight:600}.dim{color:#8f887a}
"""


def colour(line: str) -> str:
    e = html.escape(line)
    s = line.lstrip()
    if s.startswith(("✓", "- [x]", "OK:")) or "Validation passed" in s or "passed)" in s:
        return f'<span class="ok">{e}</span>'
    if s.startswith("#"):
        return f'<span class="h">{e}</span>'
    if s.startswith(("◆", "·", "[")):
        return f'<span class="dim">{e}</span>'
    return e


def main() -> None:
    out, title, pairs = sys.argv[1], sys.argv[2], sys.argv[3:]
    body = []
    for cmd, file in zip(pairs[::2], pairs[1::2], strict=True):
        body.append(f'<span class="cmd">{html.escape(cmd)}</span>')
        body += [colour(x) for x in Path(file).read_text(encoding="utf-8").rstrip().splitlines()]
        body.append("")
    page = (f"<!doctype html><meta charset=utf-8><link href='https://fonts.googleapis.com/css2?family=IBM+Plex+Mono"
            f"&family=IBM+Plex+Sans:wght@500&display=swap' rel=stylesheet><style>{CSS}</style><div class=win><div class=bar>"
            f"<span class=dot></span><span class=dot></span><span class=dot></span><span class=title>{html.escape(title)}"
            f"</span></div><pre>{chr(10).join(body).rstrip()}</pre></div>")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1100, "height": 400}, device_scale_factor=2)
        pg.set_content(page)
        pg.wait_for_timeout(700)
        pg.locator(".win").screenshot(path=out)
        b.close()


if __name__ == "__main__":
    main()
