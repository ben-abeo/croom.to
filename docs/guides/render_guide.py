"""
Render a Crystal Meet guide folder (index.html plus its assets) to PDF with the
venv's Chromium. Each guide's build.py calls render() with its own footer title.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

ASSETS = ("index.html", "crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2")


def footer(title: str) -> str:
    return f"""
<div style="width:100%;font-family:Lexend,'Segoe UI',Arial,sans-serif;font-size:7.5pt;color:#647087;
            padding:0 0.6in;display:flex;justify-content:space-between;">
  <span>{title}</span>
  <span>Page <span class="pageNumber"></span></span>
</div>
"""


def render(folder: Path, out: Path, footer_title: str) -> None:
    for asset in ASSETS:
        if not (folder / asset).is_file():
            raise SystemExit(f"missing asset: {asset}")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto((folder / "index.html").as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.pdf(
            path=str(out),
            format="Letter",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=footer(footer_title),
            margin={"top": "0.6in", "bottom": "0.7in", "left": "0.6in", "right": "0.6in"},
        )
        browser.close()
    print(f"wrote {out} ({out.stat().st_size} bytes)")
