"""
Render the Zoom Marketplace listing assets for the Crystal Meet rooms app: a
160 by 160 icon and a 1280 by 720 gallery image, in the Crystal PM style.

Usage: python deploy/zoom-marketplace/render_assets.py   (writes icon.png and gallery.png here)
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
LOGO = (HERE.parent.parent / "docs" / "guides" / "crystal-meet-zoom" / "crystalpm-logo-white.svg").read_text(encoding="utf-8")
FONT_400 = (HERE.parent.parent / "docs" / "guides" / "crystal-meet-zoom" / "fonts" / "lexend-400.woff2").as_uri()
FONT_600 = (HERE.parent.parent / "docs" / "guides" / "crystal-meet-zoom" / "fonts" / "lexend-600.woff2").as_uri()

STYLE = f"""
@font-face {{ font-family: Lexend; src: url("{FONT_400}") format("woff2"); font-weight: 400; }}
@font-face {{ font-family: Lexend; src: url("{FONT_600}") format("woff2"); font-weight: 600; }}
* {{ box-sizing: border-box; }} body {{ margin: 0; font-family: Lexend, "Segoe UI", Arial, sans-serif; color: #fff; }}
"""

ICON = f"""<!DOCTYPE html><html><head><style>{STYLE}
.icon {{ width: 160px; height: 160px; background: #16244F; border-radius: 34px; display: flex; align-items: center; justify-content: center; }}
.icon svg {{ width: 112px; height: auto; }}
</style></head><body><div class="icon">{LOGO}</div></body></html>"""

GALLERY = f"""<!DOCTYPE html><html><head><style>{STYLE}
.card {{ width: 1280px; height: 720px; background: linear-gradient(135deg, #001636 0%, #16244F 100%); padding: 72px 88px; position: relative; overflow: hidden; }}
.card svg {{ width: 150px; height: auto; display: block; margin-bottom: 56px; }}
.kicker {{ font-size: 18px; font-weight: 600; letter-spacing: .18em; text-transform: uppercase; color: #BDCEFF; margin: 0 0 14px; }}
h1 {{ font-size: 64px; font-weight: 600; line-height: 1.08; margin: 0 0 22px; max-width: 900px; }}
p.lead {{ font-size: 26px; line-height: 1.4; color: #BDCEFF; margin: 0 0 54px; max-width: 880px; }}
.chips {{ display: flex; gap: 16px; }}
.chip {{ background: rgba(255,255,255,.08); border: 1px solid rgba(189,206,255,.35); border-radius: 14px; padding: 18px 22px; font-size: 22px; }}
.chip b {{ display: block; color: #fff; font-weight: 600; margin-bottom: 4px; }} .chip span {{ color: #BDCEFF; font-size: 18px; }}
.ring {{ position: absolute; right: -180px; top: -180px; width: 620px; height: 620px; border-radius: 50%; border: 90px solid rgba(27,82,229,.35); }}
</style></head><body><div class="card"><div class="ring"></div>{LOGO}
<p class="kicker">Crystal Meet rooms</p>
<h1>Conference rooms that join the call themselves</h1>
<p class="lead">A Raspberry Pi behind each TV joins Zoom meetings through Zoom's Meeting SDK when someone presses Join on the room's touch screen. Used only inside Crystal PM.</p>
<div class="chips">
  <div class="chip"><b>Join now</b><span>from the table screen</span></div>
  <div class="chip"><b>Mute, camera, leave</b><span>without touching the Pi</span></div>
  <div class="chip"><b>Door sign and dashboard</b><span>free, booked, in use</span></div>
</div></div></body></html>"""


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        page.set_content(ICON)
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.locator(".icon").screenshot(path=str(HERE / "icon.png"))
        page.set_content(GALLERY)
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.locator(".card").screenshot(path=str(HERE / "gallery.png"))
        browser.close()
    print(f"wrote {HERE / 'icon.png'} and {HERE / 'gallery.png'}")


if __name__ == "__main__":
    main()
