# Crystal Meet

Crystal PM's conference rooms: a Raspberry Pi behind each TV joins Zoom and
Google Meet calls, a touchscreen on the table opens the room page to join and
control the call, a small screen by the door shows whether the room is free,
and one dashboard watches every room. It is a fork of
[croom.to](https://github.com/amirhmoradi/croom.to); the upstream README
follows this section. The code keeps upstream's `croom` names for the package,
the command, the service units and the folders under `/etc` and `/opt`.

## How the pieces fit

| Piece | Where it runs | What it does |
|---|---|---|
| Room device, the `croom` agent | A Raspberry Pi behind the TV, one per room | Joins meetings in a headed Chromium, reads the room's Google Calendar, serves the room page and the door sign, reports to the dashboard. |
| Room page `http://<device>:8080/` | Any browser on the office network, usually a tablet on the table | Today's bookings, Join now, Mute, Turn camera off, Leave, and a box to paste a Zoom or Meet link. |
| Door sign `http://<device>:8080/sign` | A small screen by the door, PoE or Wi-Fi | Green when free, amber ten minutes before a booking, red while in use or booked. |
| Dashboard, `src/croom-dashboard` | One server; this fork runs it on a Windows PC under WSL2 | Fleet overview, device status, enrollment tokens on the Provisioning page. |

Neither screen is cabled to the Pi: each only needs a browser, power and the
network. The device joins nothing by itself; someone presses Join now.

## Set up a room

Follow the three guides in this order:

1. [Set up a Crystal Meet room](docs/guides/crystal-meet-room-setup.pdf): prepare
   the Pi, create the room on the dashboard, install with the room's config,
   first run, point the table screen and the door sign at the device.
2. [Connect Crystal Meet rooms to Google Calendar](docs/guides/crystal-meet-google-calendar.pdf):
   room resources in Google Workspace, one service account and key, share each
   room calendar with it, put the key and the calendar address on the device.
3. [Connect Crystal Meet rooms to Zoom](docs/guides/crystal-meet-zoom.pdf): a
   Meeting SDK app, a Server-to-Server app for tokens, one Zoom user per room,
   and the credentials file on the device, so rooms join any Zoom meeting.

The commands the guides walk through, for reference:

```bash
git clone https://github.com/ben-abeo/croom.to.git && cd croom.to
cp deploy/rooms/room-1.yaml ~/room.yaml            # then edit the four REPLACE values
sudo bash installer/install.sh --config ~/room.yaml --credentials ~/google-service-account.json --zoom-credentials ~/zoom-credentials.json
sudo systemctl start croom
/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml
/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml
```

Installer options: `--config FILE` installs a prepared room config as
`/etc/croom/config.yaml`; `--credentials FILE` installs a Google service
account key as `/etc/croom/google-service-account.json` and
`--zoom-credentials FILE` the Zoom credentials as
`/etc/croom/zoom-credentials.json`, both readable only by the service user;
`--enable-ui` and `--no-service` are upstream options; the
`CROOM_REPO` environment variable overrides the pip source, which defaults to
this fork's `main` branch. Run the installer with `sudo` from the desktop user
account: the service runs as that user so Chromium can use the TV and the
room's audio. It installs the agent into `/opt/croom/venv`, Playwright's
Chromium into `/opt/croom/browsers`, and a `croom.service` unit that waits for
the desktop before starting.

## Configure a room

`deploy/rooms/` holds one config per room with placeholders, and its README
lists the values to replace. The sections that matter:

| Section | Keys | Notes |
|---|---|---|
| `room` | `name`, `location`, `timezone` | The name is shown on the page, the sign and the dashboard. |
| `meeting` | `platforms: [zoom, google_meet]`, `zoom_credentials_path` | Which links the room can join; joins are limited to those platforms' hostnames. Zoom joins go through Zoom's Meeting SDK with the credentials file (`deploy/rooms/zoom-credentials.example.json`); without it the public web client is used, and Zoom blocks automated guests there. |
| `calendar` | `providers: [google]`, `google_credentials_path`, `google_calendar_id`, `sync_interval_seconds` | The room's calendar address looks like `c_1885...@resource.calendar.google.com`. A placeholder or a missing key logs one line and the room works with pasted links only. |
| `control` | `enabled`, `host`, `port` | The room page and sign on port 8080, open on the LAN by design. |
| `dashboard` | `url`, `enrollment_token`, `heartbeat_interval_seconds` | The token comes from the dashboard's Provisioning page and works once. |

Never commit a real token or key.

## Run the dashboard

```bash
docker run -d --name croom_postgres -e POSTGRES_USER=croom -e POSTGRES_PASSWORD=croom -e POSTGRES_DB=croom \
  -p 127.0.0.1:5432:5432 -v croom_pgdata:/var/lib/postgresql/data --restart unless-stopped postgres:16-alpine
cd src/croom-dashboard/backend && npm install && npm run dev     # API and WebSocket on :3001; settings in backend/.env (not committed)
cd src/croom-dashboard/frontend && npm install && npm run dev    # web app on :3000, proxies /api and /ws to :3001
```

Open `http://localhost:3000`, sign in with the admin account the backend
creates on first start, and use Provisioning to create one token per room.
Devices enrol with `POST /api/provisioning/enroll` and then keep a WebSocket
open for heartbeats, status and meeting events. Under WSL2, turn on mirrored
networking or forward ports 3000 and 3001 so the Pis can reach the dashboard.
Restart the Vite dev server after changing `tailwind.config.js`.

## Develop and test

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/playwright install chromium
.venv/bin/pytest -q -p no:cacheprovider --deselect tests/unit/video/test_v4l2_camera.py::TestV4L2Camera::test_start
.venv/bin/croom -v -c ~/.config/croom/config.yaml                 # run the agent on this machine (WSL2 with WSLg works)
.venv/bin/python docs/guides/crystal-meet-room-setup/build.py     # rebuild a guide's PDF after editing its index.html
```

The upstream suite carries 87 tests that fail because they were written for
code that never ran, and the deselected test stalls; leave them. Everything
this fork added lives under `tests/unit/control`, `tests/unit/calendar`,
`tests/unit/deploy`, `tests/unit/installer` and `tests/unit/docs` and must
pass. The browser tests drive the room page and the sign in the venv's
Chromium; the docs tests render the PDFs. The Zoom web SDK version is pinned once, as
`SDK_VERSION` in `src/croom/meeting/providers/zoom_sdk_site.py`; bump it there when Zoom
retires that version from its CDN.

## Implementation notes

Each piece was designed in a spec, built from a plan test-first, and reviewed
by a fresh reviewer before merging. The spec holds the why and the contracts,
the plan holds the how, step by step with the code.

| Work | Spec | Plan |
|---|---|---|
| Agent startup fix and dashboard enrollment: the services join the `Service` framework; REST enroll plus a WebSocket for heartbeats | [spec](docs/superpowers/specs/2026-09-24-agent-startup-and-dashboard-enrollment-design.md) | [plan](docs/superpowers/plans/2026-09-24-agent-startup-and-dashboard-enrollment.md) |
| Room control page and API: `croom.control` with `/api/status`, `/api/calendar/events` and `/api/meeting/{join,leave,mute,camera}` | [spec](docs/superpowers/specs/2026-09-24-room-control-page-design.md) | [plan](docs/superpowers/plans/2026-09-24-room-control-page.md) |
| Crystal Meet brand, three room configs and the installer, the door sign, the setup guide | [spec](docs/superpowers/specs/2026-09-24-crystal-meet-brand-and-rooms-design.md) | [plan](docs/superpowers/plans/2026-09-24-crystal-meet-brand-and-rooms.md) |
| Google Calendar credentials, the check command, the calendar guide | [spec](docs/superpowers/specs/2026-09-25-google-calendar-credentials-design.md) | [plan](docs/superpowers/plans/2026-09-25-google-calendar-credentials.md) |
| Zoom joins through the Meeting SDK: signature, per-room Zoom user's ZAK for outside hosts, loopback page, `croom --check-zoom`, the Zoom guide | [spec](docs/superpowers/specs/2026-09-25-zoom-meeting-sdk-design.md) | [plan](docs/superpowers/plans/2026-09-25-zoom-meeting-sdk.md) |

Decisions worth knowing before changing things:

- Internal names stay `croom`; only what a person sees says Crystal Meet.
- The room page and sign are open on the LAN by design; joins are restricted to the configured platforms' hostnames and every POST must be JSON.
- The dark theme is Tailwind's stock charcoal (`#111827` page, `#1F2937` cards) with the Crystal PM blue accent and Lexend; navy was rejected for dark mode.
- Brand assets (logo, Lexend, its OFL licence) are bundled under `src/croom/control/static` and in each guide folder, so nothing loads from the internet.
- Nothing joins or leaves by itself; Join now opens ten minutes before a booking.
- With a calendar address configured, only that calendar is read; the room resource's declined (double-booked) and cancelled bookings are dropped; a link typed into an event wins over an automatically added Meet.
- Zoom is joined through Zoom's Meeting SDK, never by driving the public web client, which blocks automated guests. Meetings on your own account need only the SDK app's signature; meetings hosted elsewhere need the room's Zoom user and its ZAK, fetched with the Server-to-Server credential.

Known limitations, mostly inherited from upstream:

- Stopping the meeting service can hang while closing the headed Chromium; the service unit's restart covers it.
- Google Meet joins as a guest, so someone in the meeting must admit the room; Zoom links need their passcode in the link.
- One headed browser window opens per configured platform when the agent starts.
- `--no-service` is parsed but not honoured; the touch UI (`croom-ui`) and Microsoft 365 are untested in this fork.
- The browser path and boot-ordering fixes in the installer are verified by tests of the generated unit files, not yet on a Pi.
- Joining Zoom meetings hosted by other accounts relies on a ZAK fetched through the Server-to-Server app; this is not yet verified against a live outside-hosted meeting. If Zoom refuses, the fallback is the SDK app's own OAuth authorization.

---

*The upstream croom.to README follows.*


<div align="center">

# 🎥 Croom

### Turn Any Raspberry Pi Into a Professional Video Conferencing System

**The open-source alternative to Cisco Webex Room Kit — for 1/50th the price**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-5%20|%204-red.svg)](https://www.raspberrypi.com/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![GitHub Stars](https://img.shields.io/github/stars/amirhmoradi/croom.to?style=social)](https://github.com/amirhmoradi/croom.to)
[![Made in France](https://img.shields.io/badge/Made%20in-France-blue.svg)](https://en.wikipedia.org/wiki/French_Tech)
[![Digital Sovereignty](https://img.shields.io/badge/Digital-Sovereignty-purple.svg)](#-digital-sovereignty--data-privacy)

[Features](#-features) • [Quick Start](#-quick-start) • [Sovereignty](#-digital-sovereignty--data-privacy) • [Documentation](#-documentation) • [Contributing](#-contributing)

---

<img src="docs/assets/hero-banner.png" alt="Croom Dashboard" width="800"/>

*Transform conference rooms with enterprise-grade video conferencing at a fraction of the cost*

</div>

---

## 🇫🇷 Digital Sovereignty & Data Privacy

> **Croom is a French initiative** committed to digital resilience, data privacy, and technological independence.

In a world where video conferencing has become critical infrastructure, organizations deserve **control over their communication systems**. Croom was created to break free from:

- **Vendor Lock-in**: No dependency on single cloud providers
- **Data Exploitation**: Your meetings, your data — processed locally on your hardware
- **Unpredictable Pricing**: No per-seat licenses that scale against you
- **Opaque Systems**: Fully open source, audit everything

### Our Principles

| Principle | How Croom Delivers |
|-----------|---------------------|
| **Data Sovereignty** | All processing happens on YOUR hardware. No cloud required. |
| **Privacy by Design** | AI runs locally via Hailo/Coral. No data leaves your network. |
| **Open Source** | MIT licensed. Inspect, modify, and audit every line of code. |
| **Vendor Independence** | Works with Meet, Teams, Zoom — switch platforms freely. |
| **European Values** | GDPR-ready architecture. Built with privacy regulations in mind. |

### Self-Hosted & Air-Gapped Ready

Croom can operate **completely offline** in air-gapped environments:
- No internet required for core functionality
- Local AI processing with Hailo-8L or Coral TPU
- On-premise management dashboard
- Full functionality without external dependencies

## 💰 Why Croom?

| | Cisco Room Kit | Poly Studio | **Croom** |
|---|:---:|:---:|:---:|
| **Hardware Cost** | $3,000 - $15,000 | $2,000 - $8,000 | **< $250** |
| **Monthly License** | $15/device | $12/device | **Free forever** |
| **10 Rooms (Year 1)** | $31,800+ | $21,440+ | **$2,500** |
| **Multi-platform** | Limited | Limited | **✅ Meet, Teams, Zoom** |
| **AI Features** | ✅ | ✅ | **✅** |
| **Open Source** | ❌ | ❌ | **✅** |

> **Save $29,000+ per year** on a 10-room deployment while getting the same enterprise features.

## ✨ Features

<table>
<tr>
<td width="50%">

### 🖥️ Multi-Platform Support
- **Google Meet** - Full support with calendar integration
- **Microsoft Teams** - Join any Teams meeting
- **Zoom** - Works with Zoom web client
- Auto-detect platform from meeting URL

</td>
<td width="50%">

### 🤖 Edge AI Processing
- **Auto-framing** - Keeps participants in frame
- **Noise reduction** - Crystal clear audio
- **Occupancy counting** - Room analytics
- Works with Hailo-8L, Coral, or CPU fallback

</td>
</tr>
<tr>
<td width="50%">

### 📱 Touch Screen UI
- Beautiful room control interface
- One-tap meeting join
- Calendar view for scheduled meetings
- Camera/mic controls

</td>
<td width="50%">

### 🏢 Fleet Management
- Centralized dashboard for all devices
- Real-time device status monitoring
- Remote configuration & updates
- Usage analytics & reporting

</td>
</tr>
<tr>
<td width="50%">

### 🔧 Zero-Touch Provisioning
- QR code based device enrollment
- Automatic configuration sync
- No manual setup per device
- Scale to hundreds of rooms

</td>
<td width="50%">

### 🔒 Enterprise Security
- End-to-end encryption
- Role-based access control
- Audit logging
- On-premise deployment option

</td>
</tr>
</table>

## 🎙️ Meeting Intelligence with Vexa Integration

Croom integrates with [**Vexa**](https://github.com/Vexa-ai/vexa) — the open-source, self-hosted meeting transcription platform — for advanced meeting intelligence features while keeping **all data on your infrastructure**.

### What Vexa Adds to Croom

| Feature | Description |
|---------|-------------|
| **Real-time Transcription** | Live transcripts during meetings (100+ languages) |
| **Meeting Summaries** | AI-generated summaries and action items |
| **Searchable Archives** | Find any discussion across all your meetings |
| **Translation** | Real-time translation between 100 languages |
| **Self-Hosted** | Run on your infrastructure — no cloud dependency |

### Why Vexa + Croom?

Both projects share the same values:
- **Open Source** (Vexa: Apache 2.0, Croom: MIT)
- **Self-Hosted First** — Your data never leaves your network
- **Privacy by Design** — No third-party data processing
- **Enterprise Ready** — Built for organizations that take security seriously

```bash
# Deploy Vexa alongside Croom
git clone https://github.com/Vexa-ai/vexa
cd vexa && make all  # CPU mode (add GPU=1 for GPU acceleration)
```

### Integration Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Your Infrastructure                          │
│                                                                 │
│  ┌─────────────┐          ┌─────────────┐                      │
│  │   Croom    │◄────────►│    Vexa     │                      │
│  │   Device    │ WebSocket│  Instance   │                      │
│  │             │          │             │                      │
│  │ • Camera    │          │ • Whisper   │                      │
│  │ • Audio     │──────────│ • Transcr.  │                      │
│  │ • Display   │  Audio   │ • Summarize │                      │
│  └─────────────┘  Stream  └─────────────┘                      │
│                                                                 │
│  No data leaves your network. Everything runs locally.         │
└─────────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### One-Line Installation

```bash
curl -sSL https://croom.to/install.sh | sudo bash
```

### Manual Installation

```bash
# Clone the repository
git clone https://github.com/amirhmoradi/croom.to.git
cd croom

# Install Croom
pip install -e .

# Start the agent
croom --config /etc/croom/config.yaml
```

### Docker (Dashboard)

```bash
docker-compose up -d
```

Open `http://localhost:3000` to access the management dashboard.

## 🛠️ Hardware Requirements

### Recommended Setup (~$200)

| Component | Model | Price |
|-----------|-------|-------|
| Computer | Raspberry Pi 5 (4GB) | $60 |
| Case | Argon ONE V3 | $25 |
| Camera | Logitech C920 | $60 |
| AI Accelerator | Hailo-8L AI Kit | $70 |
| Storage | 32GB microSD | $10 |
| **Total** | | **~$225** |

### Minimum Setup (~$100)

| Component | Model | Price |
|-----------|-------|-------|
| Computer | Raspberry Pi 4 (4GB) | $55 |
| Camera | Generic USB Webcam | $20 |
| Storage | 32GB microSD | $10 |
| Power + Cables | | $15 |
| **Total** | | **~$100** |

## 📐 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Croom Device                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  Touch UI   │  │   Agent     │  │  AI Engine  │             │
│  │   (Qt6)     │  │  (Python)   │  │  (Hailo/    │             │
│  │             │  │             │  │   Coral)    │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
└────────────────────────────┬────────────────────────────────────┘
                             │ WebSocket
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Management Dashboard                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │   React     │  │   Node.js   │  │ PostgreSQL  │             │
│  │  Frontend   │◄─┤   Backend   │◄─┤  Database   │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

## 📖 Documentation

| Guide | Description |
|-------|-------------|
| [📘 User Guide](docs/guides/user-guide.md) | End-user instructions |
| [📗 Admin Guide](docs/guides/administrator-guide.md) | IT administrator setup |
| [📙 Deployment Guide](docs/guides/deployment-guide.md) | Large-scale rollout |
| [📕 API Reference](docs/api/README.md) | REST & WebSocket APIs |
| [🗺️ Roadmap](docs/roadmap/enterprise-roadmap.md) | Future development plans |

### Product Requirements
- [PRD-001: Management Dashboard](docs/prd/001-management-dashboard.md)
- [PRD-005: Touch Screen UI](docs/prd/005-touch-screen-room-ui.md)
- [PRD-006: Edge AI Features](docs/prd/006-edge-ai-features.md)
- [PRD-008: Cross-Platform Architecture](docs/prd/008-cross-platform-architecture.md)

## 🤝 Contributing

We love contributions! Croom is built by the community, for the community.

### Ways to Contribute

- 🐛 **Report Bugs** - Found an issue? [Open a bug report](https://github.com/amirhmoradi/croom.to/issues/new?template=bug_report.md)
- 💡 **Request Features** - Have an idea? [Submit a feature request](https://github.com/amirhmoradi/croom.to/issues/new?template=feature_request.md)
- 📝 **Improve Docs** - Help us make documentation better
- 💻 **Submit PRs** - Code contributions are welcome!
- ⭐ **Star the Repo** - Show your support!

See our [Contributing Guide](CONTRIBUTING.md) for detailed instructions.

### Development Setup

```bash
# Clone and setup
git clone https://github.com/amirhmoradi/croom.to.git
cd croom

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Start development servers
make dev
```

## 🌟 Community

- 💬 [GitHub Discussions](https://github.com/amirhmoradi/croom.to/discussions) - Ask questions, share ideas
- 🐛 [Issue Tracker](https://github.com/amirhmoradi/croom.to/issues) - Report bugs, request features
- 📧 [Mailing List](mailto:croom-help@googlegroups.com) - Stay updated

### Contributors

<a href="https://github.com/amirhmoradi/croom.to/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=amirhmoradi/croom.to" />
</a>

## 🏢 Enterprise Offerings (Coming Soon)

While Croom is and will always be **100% open source**, we're planning optional enterprise services for organizations that need additional support:

### Open Source (Free Forever)
- Full Croom functionality
- Community support via GitHub
- Self-hosted deployment
- All core features included

### Enterprise Support (Planned)
| Service | Description |
|---------|-------------|
| **Priority Support** | SLA-backed response times, dedicated support channel |
| **Professional Services** | Deployment assistance, custom integrations |
| **Training** | Administrator and end-user training programs |
| **Managed Updates** | Tested update packages, security patches |

### Enterprise Features (Planned)
| Feature | Description |
|---------|-------------|
| **SSO Integration** | SAML/OIDC with your identity provider |
| **Advanced Analytics** | Meeting quality scoring, usage reports, ROI dashboards |
| **Compliance Packages** | Pre-configured for GDPR, HIPAA, SOC 2 |
| **Multi-Tenant Management** | MSP/reseller dashboard for managing multiple orgs |
| **Hardware Bundles** | Pre-configured, tested hardware kits |

### Vexa Enterprise Integration (Planned)
| Feature | Description |
|---------|-------------|
| **Managed Vexa Cluster** | Hosted transcription infrastructure |
| **Meeting Intelligence Suite** | Advanced analytics, sentiment analysis, coaching |
| **Compliance Recording** | Automated retention policies, legal hold |
| **API Access** | Programmatic access to transcripts and insights |

> **Interested in enterprise offerings?** [Contact us](mailto:enterprise@croom.to) or [open a discussion](https://github.com/amirhmoradi/croom.to/discussions) to share your requirements.

## 📜 License

Croom is [MIT licensed](LICENSE). Use it freely in personal and commercial projects.

## 🙏 Acknowledgments

- Built on the foundation of the original [PiMeet](https://github.com/pmansour/pimeet) project
- Inspired by enterprise solutions like Cisco Webex Room Kit and Poly Studio
- Meeting intelligence powered by [Vexa](https://github.com/Vexa-ai/vexa) — open-source transcription
- Thanks to all [contributors](https://github.com/amirhmoradi/croom.to/graphs/contributors) who make this possible

---

## 📜 Upstream Inspiration

Croom is built upon and extends the excellent work of the original **[PiMeet](https://github.com/pmansour/pimeet)** project created by [Peter Mansour](https://github.com/pmansour).

### About the Original PiMeet

The original PiMeet project pioneered the concept of turning a Raspberry Pi into a dedicated video conferencing appliance. It demonstrated that affordable, single-board computers could effectively replace expensive commercial room systems for basic video conferencing needs.

**Original PiMeet Features:**
- Raspberry Pi-based video conferencing
- Google Meet support via Chromium
- Basic HDMI-CEC display control
- Simple bash-based automation

### What Croom Adds

Croom takes the original vision and expands it into a full enterprise-grade platform:

| Capability | Original PiMeet | Croom |
|------------|-----------------|-------|
| Meeting Platforms | Google Meet | Meet, Teams, Zoom, Webex |
| Hardware Support | Raspberry Pi 4 | Pi 4/5, x86_64 PCs, NUCs |
| AI Features | None | Auto-framing, speaker tracking, noise reduction |
| Management | Manual SSH | Fleet dashboard, zero-touch provisioning |
| Security | Basic | RBAC, audit logs, SSO, encryption |
| UI | None | Touch screen room control interface |
| Transcription | None | Vexa integration for meeting intelligence |

### Contributing Back

We believe in giving back to the open-source community. Improvements that benefit the original project scope will be contributed upstream where appropriate. See our [upstream contributions guide](docs/roadmap/upstream-contributions.md) for details.

**Thank you, Peter, for creating the foundation that made Croom possible!**

---

## 🔍 SEO & Search Keywords

<details>
<summary><b>Click to expand: What people search for when looking for solutions like Croom</b></summary>

### Conference Room Solutions
- **Open source video conferencing room system** - Croom is a fully open-source alternative to proprietary room systems
- **Raspberry Pi conference room** - Transform any Pi into a professional meeting room device
- **DIY video conferencing appliance** - Build your own enterprise-grade conferencing system
- **Self-hosted meeting room device** - Keep all your data on-premise with Croom
- **Linux conference room system** - Native Linux support for Pi and x86_64

### Alternatives & Comparisons
- **Cisco Webex Room Kit alternative** - Save 90%+ with equivalent features
- **Poly Studio alternative open source** - Full-featured replacement at fraction of cost
- **Zoom Rooms alternative self-hosted** - Multi-platform support without vendor lock-in
- **Microsoft Teams Room alternative** - Works with Teams, Meet, Zoom, and Webex
- **Neat Bar alternative** - Edge AI features without the price tag
- **Logitech Rally alternative** - Professional conferencing without expensive hardware

### Technical Features
- **Edge AI video conferencing** - Local AI processing with Hailo, Coral, NVIDIA
- **Auto-framing camera software** - Keep participants in frame automatically
- **Speaker tracking open source** - AI-powered active speaker detection
- **Meeting room noise reduction** - Crystal clear audio processing
- **HDMI-CEC meeting room** - Automatic display power management
- **WebRTC conference room** - Browser-based meeting platform support

### Enterprise & IT
- **Enterprise video conferencing fleet management** - Centralized dashboard for all devices
- **Zero-touch provisioning video conferencing** - QR code enrollment, automatic setup
- **GDPR compliant video conferencing** - European data sovereignty by design
- **SOC 2 video conferencing solution** - Enterprise security compliance ready
- **On-premise video conferencing** - Air-gapped deployment support
- **Meeting room analytics dashboard** - Usage reports and device monitoring

### Hardware & Setup
- **Raspberry Pi 5 video conferencing** - Optimized for latest Pi hardware
- **Raspberry Pi Google Meet** - Native Meet support with calendar integration
- **Raspberry Pi Microsoft Teams** - Full Teams meeting support
- **Raspberry Pi Zoom room** - Zoom web client integration
- **Intel NUC conference room** - x86_64 support for more powerful setups
- **USB webcam conference room software** - Works with any V4L2-compatible camera

### Use Cases
- **Small business video conferencing** - Affordable solution for SMBs
- **Huddle room video system** - Perfect for small meeting spaces
- **Education video conferencing** - Schools and universities deployment
- **Healthcare video conferencing HIPAA** - Compliance-ready for medical use
- **Government video conferencing** - Digital sovereignty for public sector
- **Remote office video conferencing** - Branch office deployment at scale

### Integration & Ecosystem
- **Meeting transcription self-hosted** - Vexa integration for AI transcription
- **Google Calendar meeting room** - Automatic calendar sync
- **Microsoft 365 meeting room** - Azure AD and calendar integration
- **Open source meeting room booking** - Calendar provider agnostic
- **Video conferencing API** - REST and WebSocket APIs for integration

</details>

---

<div align="center">

**⭐ Star us on GitHub — it motivates us a lot!**

[Report Bug](https://github.com/amirhmoradi/croom.to/issues) · [Request Feature](https://github.com/amirhmoradi/croom.to/issues) · [Join Discussion](https://github.com/amirhmoradi/croom.to/discussions)

---

🇫🇷 **A French Initiative for Digital Sovereignty**

*Breaking vendor lock-in. Protecting data privacy. Empowering organizations.*

Made with ❤️ by the Croom Community

</div>
