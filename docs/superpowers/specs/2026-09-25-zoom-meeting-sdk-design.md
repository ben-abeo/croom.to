# Zoom Meeting SDK joins for Crystal Meet rooms

Date: 2026-09-25. Status: approved by Ben in conversation; written for the implementation plan.

## 1. Goal

A Crystal Meet room joins Zoom meetings reliably from the room page's "Join now" and
"Join with a link", including meetings hosted by customers and vendors, without a person
signing in on the device and without Zoom's automated-guest wall. The meeting shows on
the TV in Zoom's standard full-screen layout; mute, camera and leave keep working from
the table page.

Success looks like: pressing Join now on a booking hosted by Crystal PM joins within
about fifteen seconds; the same for a meeting hosted by an outside account, where the
room appears as its own Zoom user ("Room 1"); a waiting room shows as "waiting" until
the host admits the room; a wrong passcode or a bad link gives the room page Zoom's
reason in plain words.

## 2. Where things stand

- The `zoom` provider drives Zoom's public web client with Playwright. Since the
  2026-09-25 fix it types the room name and presses Join, but Zoom then answers
  "Automated bots aren't allowed to join this meeting… sign in to join". Zoom's
  invisible reCAPTCHA scores the automated browser as a bot; this is policy, not a
  page-layout problem, and disguising the browser is not an option we take.
- Zoom's supported route for a custom client is the Meeting SDK. For the web, the
  Client View (`ZoomMtg`, version 6.5.0 at the time of writing) renders Zoom's meeting
  UI in a page; joining needs a signature (a JWT signed with the SDK app's Client
  Secret). Since March 2, 2026, meetings hosted outside the SDK app's own account also
  need the participant to join as a Zoom user with a ZAK token (or as an app with an
  OBF token, which needs a human participant's OAuth consent and does not fit a room).
- `MeetingService.start()` instantiates providers by platform name with no arguments;
  `ZoomProvider` holds the URL patterns the room page relies on (`zoom.us/j/<id>`,
  `zoom.us/wc/<id>`, `?pwd=`).
- The device already keeps one credential file per integration
  (`/etc/croom/google-service-account.json`, mode 600, installed by `--credentials`),
  has a check command pattern (`croom --check-calendar`), and two brand guides rendered
  by `docs/guides/render_guide.py`.

## 3. Decisions taken with Ben

- Route: the Zoom Meeting SDK (Client View in the room's browser), not a signed-in
  personal session and not browser disguises.
- The rooms often join meetings hosted by customers or vendors, so the first version
  includes the ZAK path: one Zoom user per room plus a Server-to-Server OAuth credential
  that lets the device fetch that user's ZAK headlessly.
- Someone else administers Zoom: the guide splits the admin's part from the device part.
- Nothing joins by itself; the join is still a person pressing Join on the room page.

## 4. Design

### 4.1 Zoom side (done once by the Zoom admin; documented in the guide)

1. A Meeting SDK app: App Marketplace › Develop › Build App › General App; Features ›
   Embed › enable Meeting SDK. Copy the Client ID and Client Secret from App
   Credentials (the development credentials are enough for use inside the account).
2. A Server-to-Server OAuth app (Build App › Server-to-Server OAuth) with the scope
   that reads user tokens (`user:read:token:admin`, or the classic `user:read:admin`).
   Copy its Account ID, Client ID and Client Secret, and activate the app.
3. One Zoom user per room, for example `room1@crystalpm.com`, on a Basic (free) seat,
   with its display name set to the room's name. External hosts see that name.

The rooms' meetings hosted on Crystal PM's own account need only the SDK app; the
Server-to-Server credential and room users are what make outside-hosted meetings work.

### 4.2 Credentials and configuration on each device

- File: `/etc/croom/zoom-credentials.json`, owner the service user, mode 600:

  ```json
  {
    "sdk_client_id": "REPLACE",
    "sdk_client_secret": "REPLACE",
    "account_id": "REPLACE",
    "s2s_client_id": "REPLACE",
    "s2s_client_secret": "REPLACE",
    "room_user": "room1@crystalpm.com"
  }
  ```

  `sdk_client_id` and `sdk_client_secret` are required. `account_id`,
  `s2s_client_id`, `s2s_client_secret` and `room_user` are all-or-nothing: with them
  the room joins any meeting as its Zoom user; without them it joins only meetings
  hosted on the account that owns the SDK app.
- `MeetingConfig` gains `zoom_credentials_path: str = ""`; `to_dict` includes it. The
  room configs set it to `/etc/croom/zoom-credentials.json`.
- Not-configured rule, `zoom_not_configured_reason(path) -> Optional[str]`: no path;
  file missing or unreadable; not JSON or not an object; `sdk_client_id` or
  `sdk_client_secret` missing or still `REPLACE`; the Server-to-Server quartet partly
  present or any of it still `REPLACE`. Each reason is one sentence saying what to fix.
- Provider selection in `MeetingService.start()` for platform `zoom`: when the reason
  is None, `ZoomSdkProvider.from_config(config)`; otherwise the existing web-client
  `ZoomProvider` with exactly one WARNING `Zoom Meeting SDK not configured: <reason>;
  using the web client, which Zoom blocks for automated guests`. The registry gains
  `build_provider(platform, config)`; `get_provider` stays for callers that have no
  config.

### 4.3 Tokens (`croom/meeting/zoom_auth.py`, standard library plus aiohttp)

- `meeting_sdk_signature(client_id, client_secret, meeting_number, role=0, now=None,
  ttl_seconds=7200) -> str`: a JWT with header `{"alg": "HS256", "typ": "JWT"}` and
  payload `appKey` (client id), `sdkKey` (client id, for older SDK builds), `mn`
  (meeting number as a string of digits), `role`, `iat`, `exp` and `tokenExp` (both
  `iat + ttl_seconds`; Zoom requires 1800 to 172800 seconds), signed HMAC-SHA256 with
  the client secret, base64url without padding.
- `ZoomApi(account_id, client_id, client_secret, oauth_url="https://zoom.us/oauth/token",
  api_url="https://api.zoom.us/v2")`:
  - `access_token()`: POST `oauth_url` with `grant_type=account_credentials` and
    `account_id`, HTTP Basic `client_id:client_secret`; cached until 60 seconds before
    Zoom's `expires_in`.
  - `user_zak(user, ttl_seconds=7200)`: GET `{api_url}/users/{user}/token?type=zak&ttl=…`
    with the bearer token; returns the `token` field.
  - Failures raise `ZoomAuthError` with plain words: token refused (400/401): "Zoom
    refused the server-to-server credentials: check the account id, client id and client
    secret, and that the app is activated"; ZAK 404: "Zoom has no user <user> on this
    account"; ZAK 400 or 403 mentioning scope: "the server-to-server app lacks the user
    token scope (user:read:token:admin); add it and re-activate"; anything else: the
    HTTP status and Zoom's message. Network errors: "could not reach Zoom".
- `load_zoom_credentials(path) -> ZoomCredentials` (dataclass with `has_room_user`).

### 4.4 The provider (`croom/meeting/providers/zoom_sdk.py`)

`ZoomSdkProvider(MeetingProvider)`, name `zoom`, display name `Zoom`. URL handling
(`can_handle_url`, `extract_meeting_id`) delegates to `ZoomProvider`'s patterns so the
room page's links, bare meeting ids and `?pwd=` keep working.

- `initialize()`: launches the headed Chromium with the same flags as the web-client
  provider plus `--autoplay-policy=no-user-gesture-required`, a context with camera and
  microphone permissions, and starts a loopback-only aiohttp site on `127.0.0.1` with an
  ephemeral port. Routes: `GET /meeting` (the page), `GET /static/meeting.js`,
  `GET /join/{token}` (JSON join parameters, served once, then 404), `GET /left` (a
  plain "Left the meeting" page). Every response carries
  `Cross-Origin-Opener-Policy: same-origin` and
  `Cross-Origin-Embedder-Policy: credentialless`; requests from any peer other than
  `127.0.0.1` get 403.
- `join_meeting(url, display_name, camera_on, mic_on)`: parses the meeting number and
  passcode; mints the signature; when the credentials have a room user, fetches its ZAK
  (a `ZoomAuthError` becomes `MeetingState.ERROR` with its words, nothing is opened);
  registers one-time join parameters `{meetingNumber, passWord, userName, signature,
  zak (optional), micOn, cameraOn, sdkVersion}`; exposes `crystalMeetEvent(state,
  detail)` to the page; navigates to `http://127.0.0.1:<port>/meeting#<token>` (the token
  travels in the fragment, so it reaches no server log). States: `joining` on
  navigation; `waiting` → `IN_LOBBY`; `connected` → `CONNECTED`; `error` → `ERROR`
  with the detail; no `connected` within 90 seconds (300 while waiting) → `ERROR`
  "Zoom did not connect; the page says: <visible text>".
- `leave_meeting()`: calls the page's `crystalMeet.leave()`; when the page reaches
  `/left` or five seconds pass, navigates to `about:blank` and sets `IDLE`.
- `toggle_mute()`: `crystalMeet.mute(!muted)`, which calls `ZoomMtg.mute({userId,
  mute})` for the current user (`ZoomMtg.getCurrentUser`); returns the new muted state.
- `toggle_camera()`: presses the Client View toolbar button whose accessible name
  contains "Start Video" or "Stop Video" (the SDK has no own-video call) and flips the
  recorded state; the button's absence raises "Zoom's video button was not found".
- `shutdown()`: closes the browser with a five-second guard (then kills it) and stops
  the site.
- Test hooks: `headless` and `extra_init_script` constructor arguments, and the SDK
  script URLs as a class attribute, so tests run the real page against a stubbed
  `window.ZoomMtg` with the CDN blocked.

### 4.5 The page (`croom/meeting/providers/zoom_sdk_page/meeting.html` and `meeting.js`)

- The HTML loads Zoom's CDN scripts for SDK 6.5.0 (react, react-dom, react-redux, redux,
  redux-thunk, lodash, `zoom-meeting-6.5.0.min.js` from `https://source.zoom.us/6.5.0/`)
  and then `/static/meeting.js`. If `window.ZoomMtg` is missing after load, the page
  reports `error` "Zoom's SDK did not load from source.zoom.us; check the device's
  internet access".
- `meeting.js`: reads the token from `location.hash`, fetches `/join/<token>`, then
  `ZoomMtg.setZoomJSLib("https://source.zoom.us/6.5.0/lib", "/av")`, `preLoadWasm()`,
  `prepareWebSDK()`, `ZoomMtg.init({leaveUrl: "/left", disablePreview: true,
  disableInvite: true, disableRecord: true, showMeetingHeader: false, isSupportChat:
  false, leaveOnPageUnload: true, patchJsMedia: true, disableCORP:
  !window.crossOriginIsolated})`, then `ZoomMtg.join({signature, meetingNumber,
  passWord, userName, userEmail: "", zak?})`. It listens to
  `inMeetingServiceListener("onMeetingStatus")` (1 connecting → `joining`, 2 connected →
  `connected`, 3 disconnected → `left`, 4 reconnecting → `reconnecting`) and
  `onUserIsInWaitingRoom` → `waiting`; after `connected` it mutes when `micOn` is false.
  Join errors report `error` with Zoom's `errorMessage` or `reason` and the code.
- `window.crystalMeet = {state, detail, mute(muted) -> Promise<boolean>, leave()}`; every
  state change calls `window.crystalMeetEvent(state, detail)` when the provider exposed
  it.

### 4.6 The check command

`croom --check-zoom [-c CONFIG]` (`croom/meeting/zoom_check.py`, `check_zoom(config, out,
api_factory=None) -> int`), exit 0 when every configured piece works, 1 otherwise:

```
Zoom Meeting SDK: app abc123XYZ, signature minted
Zoom account: server-to-server token obtained
Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)
Ready: this room can join meetings hosted by any Zoom account.
```

Without the Server-to-Server part the third line reads `Zoom room user: not configured;
this room can join only meetings hosted on your own Zoom account.` and the exit is 0.
Failures print the not-configured reason or the `ZoomAuthError` words, one line each.

### 4.7 Installer, configs, README

- `installer/install.sh --zoom-credentials FILE`: validated at parse time, installed to
  `$CONFIG_DIR/zoom-credentials.json` the same way as the Google key (`install`, mode
  600, kept when the source is already the target). Completion adds `Check Zoom:
  /opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml`.
- `deploy/rooms/room-N.yaml`: `meeting.zoom_credentials_path:
  /etc/croom/zoom-credentials.json`; the rooms README lists the file and the per-room
  `room_user`.
- README: the Zoom pieces in "Set up a room" and "Configure a room", the guide link, and
  an implementation-notes row.

### 4.8 The guide

`docs/guides/crystal-meet-zoom/index.html` → `docs/guides/crystal-meet-zoom.pdf` through
`render_guide.py`, in the brand. Sections: at a glance; before you begin (who does what,
the three secrets); step 1 the Meeting SDK app; step 2 the Server-to-Server app and its
scope; step 3 a Zoom user per room; step 4 the credentials file on the device, the
installer flag, restart, the check; step 5 test with a meeting hosted by your account and
one hosted by an outside account, then mute, camera and leave from the table page;
troubleshooting (credentials refused, no such user, scope missing, "not authorized"
for an outside host, wrong passcode, waiting room, SDK failed to load).

## 5. Testing

- Signature: decodes with the secret in the test (a small HS256 verifier), claims exact,
  `mn` a digit string, `exp` and `tokenExp` equal `iat + ttl`.
- `ZoomApi` against an aiohttp fake of `POST /oauth/token` and `GET /users/{u}/token`:
  the Basic header and grant parameters, caching of the access token, the ZAK request's
  `type` and `ttl`, and each failure's words (401 token, 404 user, 400 scope, network).
- Credentials loading and every not-configured reason.
- Provider selection: with credentials the SDK provider, without them the web client
  and exactly one WARNING.
- The page in the venv's Chromium with `source.zoom.us` blocked and a stub `ZoomMtg`
  injected before load: init options, join parameters (signature, meeting number,
  passcode, name, ZAK), the events reaching Python, `mute` calling `ZoomMtg.mute` with
  the current user's id, `leave` calling `leaveMeeting`, and the "SDK did not load"
  error when the stub is absent.
- The provider end to end with the stubbed page: join → `CONNECTED`, waiting room →
  `IN_LOBBY` then `CONNECTED`, join error → `ERROR` with Zoom's words, ZAK failure →
  `ERROR` without opening the page, leave → `IDLE`; one-time join parameters (second
  fetch 404); non-loopback requests 403; the cross-origin headers present.
- Check command words and exit codes with a fake API; the command-line flag.
- Installer, room configs, README and guide tests in the established pattern.
- Suite gate as before (81 pre-existing upstream failures, none new).
- Acceptance with Ben: a meeting hosted on Crystal PM's account; a meeting hosted by an
  outside account (this settles whether a ZAK from the Server-to-Server app satisfies
  Zoom's rule; if not, the fallback is fetching the ZAK through the SDK app's own OAuth
  authorization, a follow-up); a waiting room; mute, camera and leave from the page.

## 6. Files

| File | Responsibility |
|---|---|
| `src/croom/core/config.py` | `MeetingConfig.zoom_credentials_path`. |
| `src/croom/meeting/zoom_auth.py` | Credentials file, not-configured rule, signature, `ZoomApi`. |
| `src/croom/meeting/providers/zoom_sdk.py` | The provider, its loopback site and browser control. |
| `src/croom/meeting/providers/zoom_sdk_page/meeting.html`, `meeting.js` | The SDK page and bridge. |
| `src/croom/meeting/providers/__init__.py`, `src/croom/meeting/service.py` | `build_provider`, selection and the one warning. |
| `src/croom/meeting/zoom_check.py`, `src/croom/core/agent.py` | `croom --check-zoom`. |
| `pyproject.toml` | Package data for the page files. |
| `installer/install.sh`, `deploy/rooms/*`, `README.md` | Flag, config key, docs. |
| `docs/guides/crystal-meet-zoom/*`, `docs/guides/crystal-meet-zoom.pdf` | The guide. |
| `tests/unit/meeting/test_zoom_auth.py`, `test_zoom_sdk_page.py`, `test_zoom_sdk_provider.py`, `test_zoom_check.py`, `test_zoom_selection.py`; installer, deploy, docs tests | Tests above. |

## 7. Out of scope

Automatic joining; joining as a Zoom Rooms system; webinars and registration links
(`tk`); screen sharing from the room; OBF tokens; the Zoom web-client provider beyond
its role as the fallback; Google Meet's guest wall (a Workspace admin setting).
