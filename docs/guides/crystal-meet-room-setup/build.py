"""
Render the Crystal Meet room setup guide to PDF with the venv's Chromium.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-room-setup.pdf)
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "crystal-meet-room-setup.pdf"

FOOTER = """
<div style="width:100%;font-family:Lexend,'Segoe UI',Arial,sans-serif;font-size:7.5pt;color:#647087;
            padding:0 0.6in;display:flex;justify-content:space-between;">
  <span>How-to guide · Set up a Crystal Meet room</span>
  <span>Page <span class="pageNumber"></span></span>
</div>
"""


def build(out: Path) -> None:
    for asset in ("index.html", "crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2"):
        if not (HERE / asset).is_file():
            raise SystemExit(f"missing asset: {asset}")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto((HERE / "index.html").as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.pdf(
            path=str(out),
            format="Letter",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=FOOTER,
            margin={"top": "0.6in", "bottom": "0.7in", "left": "0.6in", "right": "0.6in"},
        )
        browser.close()
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
