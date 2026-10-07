"""
Render the Zoom Marketplace listing assets for the Crystal Meet rooms app, at the
sizes the App Listing page demands: a 160 by 160 icon, a 1200 by 780 gallery
image and a 1824 by 176 cover strip, in the Crystal PM style.

Usage: python deploy/zoom-marketplace/render_assets.py   (writes the PNGs next to this file)
"""

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ZOOM_GUIDE = HERE.parent.parent / "docs" / "guides" / "crystal-meet-zoom"
LOGO = (ZOOM_GUIDE / "crystalpm-logo-white.svg").read_text(encoding="utf-8")


def font(name: str) -> str:
    data = base64.b64encode((ZOOM_GUIDE / "fonts" / name).read_bytes()).decode()
    return f"data:font/woff2;base64,{data}"


STYLE = f"""
@font-face {{ font-family: Lexend; src: url("{font('lexend-400.woff2')}") format("woff2"); font-weight: 400; }}
@font-face {{ font-family: Lexend; src: url("{font('lexend-600.woff2')}") format("woff2"); font-weight: 600; }}
* {{ box-sizing: border-box; }} body {{ margin: 0; font-family: Lexend, "Segoe UI", Arial, sans-serif; color: #fff; }}
.ring {{ position: absolute; border-radius: 50%; border: 90px solid rgba(27,82,229,.35); }}
"""

ICON = f"""<!DOCTYPE html><html><head><style>{STYLE}
.icon {{ width: 160px; height: 160px; background: #16244F; border-radius: 34px; display: flex; align-items: center; justify-content: center; }}
.icon svg {{ width: 112px; height: auto; }}
</style></head><body><div class="icon">{LOGO}</div></body></html>"""

GALLERY = f"""<!DOCTYPE html><html><head><style>{STYLE}
.card {{ width: 1200px; height: 780px; background: linear-gradient(135deg, #001636 0%, #16244F 100%); padding: 80px 84px; position: relative; overflow: hidden; }}
.card svg {{ width: 150px; height: auto; display: block; margin-bottom: 60px; }}
.kicker {{ font-size: 18px; font-weight: 600; letter-spacing: .18em; text-transform: uppercase; color: #BDCEFF; margin: 0 0 14px; }}
h1 {{ font-size: 62px; font-weight: 600; line-height: 1.08; margin: 0 0 22px; max-width: 860px; }}
p.lead {{ font-size: 25px; line-height: 1.42; color: #BDCEFF; margin: 0 0 58px; max-width: 840px; }}
.chips {{ display: flex; gap: 16px; }}
.chip {{ background: rgba(255,255,255,.08); border: 1px solid rgba(189,206,255,.35); border-radius: 14px; padding: 18px 22px; font-size: 21px; }}
.chip b {{ display: block; color: #fff; font-weight: 600; margin-bottom: 4px; }} .chip span {{ color: #BDCEFF; font-size: 17px; }}
.ring {{ right: -200px; top: -200px; width: 640px; height: 640px; }}
</style></head><body><div class="card"><div class="ring"></div>{LOGO}
<p class="kicker">Crystal Meet rooms</p>
<h1>Conference rooms that join the call themselves</h1>
<p class="lead">A Raspberry Pi behind each TV joins Zoom meetings through Zoom's Meeting SDK when someone presses Join on the room's touch screen. Used only inside Crystal PM.</p>
<div class="chips">
  <div class="chip"><b>Join now</b><span>from the table screen</span></div>
  <div class="chip"><b>Mute, camera, leave</b><span>without touching the Pi</span></div>
  <div class="chip"><b>Door sign and dashboard</b><span>free, booked, in use</span></div>
</div></div></body></html>"""

# The left third sits under the app logo on Zoom's page, so the strip keeps its shapes on the right and no text.
COVER = f"""<!DOCTYPE html><html><head><style>{STYLE}
.cover {{ width: 1824px; height: 176px; background: linear-gradient(90deg, #001636 0%, #16244F 60%, #1B52E5 100%); position: relative; overflow: hidden; }}
.ring {{ border-width: 40px; }}
.r1 {{ right: 120px; top: -140px; width: 360px; height: 360px; }}
.r2 {{ right: 560px; top: 60px; width: 260px; height: 260px; border-color: rgba(189,206,255,.25); }}
.r3 {{ right: 1000px; top: -220px; width: 300px; height: 300px; border-color: rgba(27,82,229,.25); }}
</style></head><body><div class="cover"><div class="ring r1"></div><div class="ring r2"></div><div class="ring r3"></div></div></body></html>"""


def shoot(page, html: str, selector: str, out: Path) -> None:
    page.set_content(html)
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(300)
    page.locator(selector).screenshot(path=str(out))


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1900, "height": 900}, device_scale_factor=1)
        shoot(page, ICON, ".icon", HERE / "icon.png")
        shoot(page, GALLERY, ".card", HERE / "gallery.png")
        shoot(page, COVER, ".cover", HERE / "cover.png")
        browser.close()
    print(f"wrote icon.png, gallery.png and cover.png in {HERE}")


if __name__ == "__main__":
    main()
