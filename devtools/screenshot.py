"""Capture README/catalog screenshots of the dashboard preview and the site with Playwright.

    python devtools/screenshot.py dashboard http://127.0.0.1:8765
    python devtools/screenshot.py site site/index.html
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def dashboard(base: str) -> None:
    with sync_playwright() as p:
        b = p.chromium.launch()
        for theme in ("dark", "light"):
            page = b.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            page.goto(f"{base}/?theme={theme}")
            page.wait_for_selector(".kt-detail__name")
            page.wait_for_timeout(600)
            page.screenshot(path=str(OUT / f"dashboard-review-{theme}.png"), full_page=False)
            if theme == "dark":
                page.click("text=Active")
                page.click(".kt-row")
                page.wait_for_timeout(500)
                page.screenshot(path=str(OUT / "dashboard-active.png"), full_page=True)
            page.close()
        m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=3)
        m.goto(f"{base}/?theme=dark")
        m.wait_for_selector(".kt-detail__name")
        m.wait_for_timeout(500)
        m.screenshot(path=str(OUT / "dashboard-mobile.png"), full_page=False)
        b.close()


def site(path: str) -> None:
    url = Path(path).resolve().as_uri()
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, vw, vh, scale in (("site-desktop", 1440, 900, 2), ("site-mobile", 390, 844, 3)):
            page = b.new_page(viewport={"width": vw, "height": vh}, device_scale_factor=scale)
            page.goto(url)
            page.evaluate("document.querySelectorAll('.reveal').forEach(e => e.classList.add('is-in'))")
            page.wait_for_timeout(1900)
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
            page.close()
        page = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(Path(path).resolve().with_name("docs.html").as_uri())
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "site-docs.png"), full_page=False)
        b.close()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    {"dashboard": dashboard, "site": site}[sys.argv[1]](sys.argv[2])
