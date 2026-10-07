"""
Render the architecture diagram for the Zoom Marketplace Technical Design page:
deploy/zoom-marketplace/architecture.png (and .pdf), in the Crystal PM style.

Usage: python deploy/zoom-marketplace/render_diagram.py
"""

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ZOOM_GUIDE = HERE.parent.parent / "docs" / "guides" / "crystal-meet-zoom"


def font(name: str) -> str:
    return "data:font/woff2;base64," + base64.b64encode((ZOOM_GUIDE / "fonts" / name).read_bytes()).decode()


HTML = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: Lexend; src: url("{font('lexend-400.woff2')}") format("woff2"); font-weight: 400; }}
@font-face {{ font-family: Lexend; src: url("{font('lexend-600.woff2')}") format("woff2"); font-weight: 600; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: Lexend, "Segoe UI", Arial, sans-serif; color: #1C2024; background: #fff; }}
.page {{ width: 1600px; height: 1000px; padding: 48px 56px; position: relative; }}
h1 {{ font-size: 30px; font-weight: 600; margin: 0 0 4px; color: #16244F; }}
p.sub {{ margin: 0 0 28px; color: #647087; font-size: 16px; }}
.zone {{ position: absolute; border: 2px dashed #BDCEFF; border-radius: 18px; }}
.zone .label {{ position: absolute; top: -13px; left: 20px; background: #fff; padding: 0 10px; font-size: 13px; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: #1B52E5; }}
.box {{ position: absolute; border-radius: 14px; padding: 16px 18px; font-size: 15px; line-height: 1.4; background: #fff; border: 1.5px solid #BDCEFF; box-shadow: 0 2px 10px rgba(0,22,54,.06); }}
.box b {{ display: block; font-size: 17px; font-weight: 600; color: #16244F; margin-bottom: 4px; }}
.box.dark {{ background: #16244F; border-color: #16244F; color: #fff; }} .box.dark b {{ color: #fff; }} .box.dark span {{ color: #BDCEFF; }}
.box.inner {{ background: #EDF2FE; border-color: #BDCEFF; }}
.box span {{ color: #647087; font-size: 13.5px; }}
.note {{ position: absolute; font-size: 13px; color: #647087; max-width: 420px; line-height: 1.45; }}
svg {{ position: absolute; left: 0; top: 0; width: 1600px; height: 1000px; pointer-events: none; }}
.arrow {{ stroke: #1B52E5; stroke-width: 2.5; fill: none; marker-end: url(#head); }}
.arrow.grey {{ stroke: #8EABF8; }}
.lbl {{ font-size: 13px; fill: #1C2024; font-family: Lexend, "Segoe UI", Arial, sans-serif; }}
</style></head><body><div class="page">
<h1>Crystal Meet Rooms: architecture</h1>
<p class="sub">An internal, device-specific Zoom Meeting SDK app. Every component is on Crystal PM's office network except Zoom itself.</p>

<div class="zone" style="left:56px; top:120px; width:1060px; height:800px;"><div class="label">Crystal PM office network (LAN only, no inbound internet access)</div></div>
<div class="zone" style="left:1170px; top:120px; width:374px; height:800px;"><div class="label">Zoom</div></div>

<div class="box" style="left:90px; top:170px; width:300px;"><b>Table touch screen</b>Browser in kiosk mode showing the room page served by the room device.<br><span>Join now, Mute, Camera, Leave, paste a link. A person presses Join; nothing joins by itself.</span></div>
<div class="box" style="left:90px; top:330px; width:300px;"><b>Door sign</b>PoE panel showing free / booked / in use, read from the room device.</div>
<div class="box" style="left:90px; top:470px; width:300px;"><b>Dashboard (Raspberry Pi)</b>Node/Express + Postgres under Docker. Fleet status and enrollment tokens.<br><span>Never talks to Zoom.</span></div>

<div class="box dark" style="left:470px; top:170px; width:600px; height:560px;"><b>Room device: Raspberry Pi 5 behind the room's TV (one per room)</b><span>Raspberry Pi OS 64-bit. The croom agent, Python 3.12, runs as a systemd service.</span>
  <div class="box inner" style="left:22px; top:92px; width:264px;"><b>Control service</b>aiohttp on :8080, LAN only. Room page, door sign, /api/meeting/*.</div>
  <div class="box inner" style="left:314px; top:92px; width:264px;"><b>Calendar service</b>Google Calendar read-only via a service account (today's bookings).</div>
  <div class="box inner" style="left:22px; top:248px; width:556px;"><b>Meeting service, Zoom provider</b>1. Mints the Meeting SDK signature (HS256 JWT) from the SDK app's client id and secret.<br>2. For a meeting hosted by another account: Server-to-Server OAuth token, then GET /users/&lt;room user&gt;/token?type=zak.<br>3. Serves a one-page site on 127.0.0.1 that loads Zoom's Web Meeting SDK 6.5.0 (Client View) from source.zoom.us.<br>4. Opens it in the bundled Chromium (Playwright) on the TV; mute, camera and leave go through the SDK's bridge.</div>
  <div class="box inner" style="left:22px; top:424px; width:556px;"><b>Credentials</b>/etc/croom/zoom-credentials.json, root-only (mode 600): SDK client id and secret, Server-to-Server account id, client id and secret, the room's Zoom user. Nothing else is stored; the ZAK lives in memory for one join.</div>
</div>
<div class="note" style="left:470px; top:760px;">Camera and speakerphone are USB devices on the room device; media goes straight from Chromium to Zoom's meeting servers over TLS/DTLS-SRTP, as in any Zoom web client.</div>

<div class="box" style="left:1200px; top:170px; width:314px;"><b>App Marketplace</b>General App "Crystal Meet Rooms" (Meeting SDK) and a Server-to-Server OAuth app with scope user:read:zak:admin. Unlisted.</div>
<div class="box" style="left:1200px; top:330px; width:314px;"><b>zoom.us/oauth/token</b>account_credentials grant (HTTPS). Token cached in memory until expiry.</div>
<div class="box" style="left:1200px; top:450px; width:314px;"><b>api.zoom.us/v2/users/&#123;user&#125;/token</b>type=zak for the room's own Zoom user (HTTPS).</div>
<div class="box" style="left:1200px; top:570px; width:314px;"><b>source.zoom.us</b>Zoom's Web Meeting SDK scripts and styles (HTTPS).</div>
<div class="box" style="left:1200px; top:690px; width:314px;"><b>Zoom meeting servers</b>The room joins as a participant: signature + meeting number + passcode (+ ZAK for other accounts' meetings).</div>

<svg viewBox="0 0 1600 1000"><defs><marker id="head" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#1B52E5"/></marker></defs>
  <path class="arrow" d="M392,215 L466,215"/><text class="lbl" x="398" y="206">HTTP :8080</text>
  <path class="arrow grey" d="M392,375 L466,375"/><text class="lbl" x="398" y="366">HTTP :8080/sign</text>
  <path class="arrow grey" d="M392,520 L466,520"/><text class="lbl" x="398" y="511">WebSocket :3001 (device → dashboard)</text>
  <path class="arrow" d="M1074,470 L1196,375"/><text class="lbl" x="1086" y="420">token (HTTPS)</text>
  <path class="arrow" d="M1074,480 L1196,490"/><text class="lbl" x="1086" y="472">ZAK (HTTPS)</text>
  <path class="arrow" d="M1074,500 L1196,610"/><text class="lbl" x="1086" y="560">SDK scripts (HTTPS)</text>
  <path class="arrow" d="M1074,520 L1196,730"/><text class="lbl" x="1086" y="640">join + media</text>
</svg>
</div></body></html>"""


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.set_content(HTML)
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.locator(".page").screenshot(path=str(HERE / "architecture.png"))
        page.pdf(path=str(HERE / "architecture.pdf"), width="1600px", height="1000px", print_background=True)
        browser.close()
    print(f"wrote architecture.png and architecture.pdf in {HERE}")


if __name__ == "__main__":
    main()
