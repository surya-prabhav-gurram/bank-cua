#!/usr/bin/env python3
"""
Render docs/report.html -> bank-cua-report.pdf.

docs/report.html is the source of truth for the styled report; this keeps the
committed PDF from drifting away from it. Run from the repo root after editing
the HTML (or after changing REPORT.md and mirroring the edit into the HTML).

Fonts matter: the layout targets Helvetica Neue / SF Mono, so render on macOS.
On a machine without those fonts Chromium substitutes and the one-page
executive summary spills onto a second page.

    python scripts/render_report.py
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "report.html"
OUT = ROOT / "bank-cua-report.pdf"


def main() -> int:
    if not SRC.exists():
        print(f"missing {SRC}", file=sys.stderr)
        return 1
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(SRC.as_uri(), wait_until="load")
        page.emulate_media(media="print")
        page.pdf(path=str(OUT), format="A4",
                 margin={"top": "14mm", "bottom": "14mm",
                         "left": "12mm", "right": "12mm"},
                 print_background=True)
        browser.close()
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
