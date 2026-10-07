# Google Meet as a signed-in room: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rooms join Google Meet as their own Workspace user through a persistent, signed-in browser profile, with a one-time `croom --sign-in-meet`, camera and microphone set from the config, a clear message when the sign-in lapses, and a guide for the Workspace side.

**Architecture:** `meeting.google_profile_dir` in the room config points at a profile folder owned by the service user; `GoogleMeetProvider` launches a persistent context on it (guest launch when empty), reads the pre-join camera and microphone buttons and clicks them into the configured state, and recognises Google's sign-in page. `croom --sign-in-meet` runs the bundled Chromium plainly on the room's screen for the sign-in; `croom --check-meet` uses the configured profile. The installer makes the folder; a fifth guide covers the Workspace users, their organizational unit and the sign-in.

**Tech Stack:** Python 3.12, Playwright (async API for the provider and check, sync API only to find the Chromium executable), aiohttp-free, bash installer, pytest with replica pages, guides rendered by `docs/guides/render_guide.py`.

**Spec:** `docs/superpowers/specs/2026-10-07-google-meet-room-account-design.md`

## Global Constraints

- Internal names stay `croom`; only what a person sees says Crystal Meet.
- No disguising of automation: the browser keeps Playwright's default flags; the only change of identity this round was dropping the false Chrome 120 user agent, already on main.
- The profile folder is `/var/lib/croom/meet-profile` on devices, owned by the service user, mode 700; empty `google_profile_dir` means guest mode exactly as today.
- Secrets never enter the repo: no account names beyond `roomN@crystalpm.com` examples, no passwords.
- Commit messages are plain, no attribution lines.
- Tests: `.venv/bin/pytest -q -p no:cacheprovider <path>`. Before the final commit run the gate: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh meet-account` and read `grep "GATE:"` (81 upstream failures is the baseline).
- Replica pages stand in for Meet in tests; the real Meet is only exercised on PiMeet-3 in Task 8.

## Review Focus

Failure modes the spec implies that a person would hit first; each has its test in the owning task:

1. A profile folder that cannot be created or secured (missing or read-only parent) must fail the provider's start with an error naming the folder, not a Playwright stack trace (Task 2, `test_unusable_profile_folder_is_a_clear_error`).
2. Running the sign-in while the room service holds the profile must be refused with the stop command, never two browsers on one profile (Task 4, `test_refuses_while_the_service_holds_the_profile`).
3. A pre-join page with a microphone button but no camera button (no camera plugged in) must still join (Task 3, `test_a_missing_toggle_does_not_stop_the_join`).
4. An empty `room.name` must leave the room named "Conference Room", never an empty name (Task 1, `test_meet_provider_without_a_profile_is_a_guest_named_conference_room`).
5. The check run with a config that has no profile must say it is a guest and still work (Task 5, `test_check_without_a_configured_profile_is_a_guest`).

---

### Task 1: Config key, room configs, provider constructor and factory

**Files:**
- Modify: `src/croom/core/config.py:46-54` (`MeetingConfig`), `:232-239` (`to_dict` meeting block)
- Modify: `deploy/rooms/room-1.yaml`, `room-2.yaml`, `room-3.yaml` (meeting section)
- Modify: `src/croom/meeting/providers/google_meet.py:67-74` (`__init__`)
- Modify: `src/croom/meeting/providers/__init__.py` (`build_provider`)
- Create: `tests/unit/meeting/test_meet_selection.py`
- Modify: `tests/unit/deploy/test_room_configs.py:32`

**Interfaces:**
- Produces: `MeetingConfig.google_profile_dir: str` (default `""`); `GoogleMeetProvider(profile_dir: Optional[str] = None, room_name: str = "Conference Room", headless: bool = False)` with read-only properties `profile_dir: Optional[Path]` and `room_name: str`; `GoogleMeetProvider.from_config(config) -> GoogleMeetProvider`; `build_provider("google_meet", config)` returns it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_meet_selection.py`:

```python
"""
The google_meet platform is built from the config: the signed-in profile folder
and the room's name (spec 2026-10-07 Google Meet, section 4.2).
"""

from pathlib import Path

from croom.core.config import Config
from croom.meeting.providers import build_provider
from croom.meeting.providers.google_meet import GoogleMeetProvider


def test_config_round_trips_the_profile_dir():
    config = Config.from_dict({"meeting": {"google_profile_dir": "/var/lib/croom/meet-profile"}})
    assert config.meeting.google_profile_dir == "/var/lib/croom/meet-profile"
    assert Config.from_dict(config.to_dict()).meeting.google_profile_dir == "/var/lib/croom/meet-profile"
    assert Config().meeting.google_profile_dir == ""


def test_meet_provider_gets_the_profile_and_the_room_name():
    config = Config.from_dict({"room": {"name": "Room 3"}, "meeting": {"google_profile_dir": "/tmp/meet-profile"}})
    provider = build_provider("google_meet", config)
    assert isinstance(provider, GoogleMeetProvider)
    assert provider.profile_dir == Path("/tmp/meet-profile")
    assert provider.room_name == "Room 3"


def test_meet_provider_without_a_profile_is_a_guest_named_conference_room():
    provider = build_provider("google_meet", Config.from_dict({"room": {"name": ""}}))
    assert provider.profile_dir is None
    assert provider.room_name == "Conference Room"
```

In `tests/unit/deploy/test_room_configs.py` add after the `zoom_credentials_path` assertion:

```python
    assert config.meeting.google_profile_dir == "/var/lib/croom/meet-profile"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_selection.py tests/unit/deploy/test_room_configs.py`
Expected: FAIL. `MeetingConfig` rejects `google_profile_dir`, `GoogleMeetProvider` has no `profile_dir`, the room configs lack the key.

- [ ] **Step 3: Add the config key**

In `src/croom/core/config.py` add to `MeetingConfig` after `zoom_credentials_path`:

```python
    google_profile_dir: str = ""  # /var/lib/croom/meet-profile on a room device; empty means a guest browser
```

and in `to_dict`'s `"meeting"` block after the `zoom_credentials_path` line:

```python
                "google_profile_dir": self.meeting.google_profile_dir,
```

In each of `deploy/rooms/room-1.yaml`, `room-2.yaml`, `room-3.yaml` add under `meeting:` after `zoom_credentials_path`:

```yaml
  google_profile_dir: /var/lib/croom/meet-profile
```

- [ ] **Step 4: Give the provider its constructor and factory**

In `src/croom/meeting/providers/google_meet.py` replace `__init__`:

```python
    def __init__(self, profile_dir: Optional[str] = None, room_name: str = "Conference Room",
                 headless: bool = False):
        super().__init__()
        # The signed-in Google profile (spec 2026-10-07, section 4.2); None means a guest browser.
        self._profile_dir: Optional[Path] = Path(profile_dir) if profile_dir else None
        self._room_name = room_name or "Conference Room"
        self._headless = headless
        # Where a screenshot goes when a join fails, so the TV need not be watched.
        self.failure_screenshot: Path = Path(tempfile.gettempdir()) / "croom-meet-failure.png"
        self._playwright = None
        self._browser: Optional["Browser"] = None
        self._context: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None

    @classmethod
    def from_config(cls, config) -> "GoogleMeetProvider":
        return cls(profile_dir=config.meeting.google_profile_dir or None,
                   room_name=config.room.name or "Conference Room")

    @property
    def profile_dir(self) -> Optional[Path]:
        return self._profile_dir

    @property
    def room_name(self) -> str:
        return self._room_name
```

In `src/croom/meeting/providers/__init__.py`, in `build_provider`, before `provider_cls = get_provider(platform)`:

```python
    if platform == "google_meet":
        from croom.meeting.providers.google_meet import GoogleMeetProvider

        return GoogleMeetProvider.from_config(config)
```

and extend the docstring's last sentence with "Google Meet gets the signed-in profile folder and the room's name (spec 2026-10-07 Google Meet, section 4.2)."

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_selection.py tests/unit/deploy/test_room_configs.py tests/unit/core/test_config.py tests/unit/meeting/test_zoom_selection.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/croom/core/config.py deploy/rooms/room-1.yaml deploy/rooms/room-2.yaml deploy/rooms/room-3.yaml src/croom/meeting/providers/google_meet.py src/croom/meeting/providers/__init__.py tests/unit/meeting/test_meet_selection.py tests/unit/deploy/test_room_configs.py
git commit -m "feat(meet): google_profile_dir config key; the Meet provider is built from the config"
```

---

### Task 2: Persistent, signed-in browser profile

**Files:**
- Modify: `src/croom/meeting/providers/google_meet.py:107-127` (`initialize`)
- Create: `tests/unit/meeting/test_meet_profile.py`

**Interfaces:**
- Consumes: `GoogleMeetProvider(profile_dir, room_name, headless)` from Task 1.
- Produces: `initialize()` launching `launch_persistent_context` when `profile_dir` is set (then `_browser` stays `None`), the guest launch otherwise; a `RuntimeError` naming the folder when it cannot be created or secured.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_meet_profile.py`:

```python
"""
With a profile folder the Meet provider keeps its browser session there, so a
one-time Google sign-in survives restarts (spec 2026-10-07, section 4.3).
"""

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.google_meet import GoogleMeetProvider  # noqa: E402


def page_file(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<!DOCTYPE html><html><head><title>Crystal Meet</title></head><body>hello</body></html>")
    return page


async def test_profile_launch_creates_a_private_folder_and_a_working_page(tmp_path):
    profile = tmp_path / "profile"
    provider = GoogleMeetProvider(profile_dir=str(profile), room_name="Room 3", headless=True)
    await provider.initialize()
    try:
        assert oct(profile.stat().st_mode & 0o777) == "0o700"
        await provider._page.goto(page_file(tmp_path).as_uri())
        assert await provider._page.title() == "Crystal Meet"
        assert provider._browser is None  # a persistent context owns its browser
    finally:
        await provider.shutdown()
    assert provider._context is None and provider._playwright is None
    assert any(profile.iterdir())  # Chromium wrote the profile there


async def test_guest_launch_still_works_without_a_profile(tmp_path):
    provider = GoogleMeetProvider(headless=True)
    await provider.initialize()
    try:
        await provider._page.goto(page_file(tmp_path).as_uri())
        assert await provider._page.title() == "Crystal Meet"
        assert provider._browser is not None
    finally:
        await provider.shutdown()


async def test_unusable_profile_folder_is_a_clear_error(tmp_path):
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    provider = GoogleMeetProvider(profile_dir=str(parent / "profile"), headless=True)
    try:
        with pytest.raises(RuntimeError) as failure:
            await provider.initialize()
        assert str(parent / "profile") in str(failure.value)
    finally:
        await provider.shutdown()
        parent.chmod(0o700)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_profile.py`
Expected: FAIL. The first test finds `_browser` set (guest launch ignores the profile) and the folder untouched; the third gets a Playwright error, not a `RuntimeError` naming the folder.

- [ ] **Step 3: Launch on the profile**

Add `import os` to the imports of `src/croom/meeting/providers/google_meet.py` and replace `initialize`:

```python
    async def initialize(self) -> None:
        """Open the browser: on the signed-in profile when one is configured, as a guest otherwise."""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        logger.info("Initializing Google Meet provider...")
        self._playwright = await async_playwright().start()

        if self._profile_dir is not None:
            try:
                self._profile_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self._profile_dir, 0o700)
            except OSError as e:
                raise RuntimeError(f"Google Meet profile folder {self._profile_dir} is not usable: {e}") from e
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir), headless=self._headless, args=self.BROWSER_ARGS, **self.context_options(),
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            logger.info(f"Google Meet provider initialized with the signed-in profile at {self._profile_dir}")
            return

        self._browser = await self._playwright.chromium.launch(headless=self._headless, args=self.BROWSER_ARGS)
        self._context = await self._browser.new_context(**self.context_options())
        self._page = await self._context.new_page()
        logger.info("Google Meet provider initialized as a guest")
```

`shutdown()` already closes the page, the context, the browser when there is one, and stops Playwright; nothing to change there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_profile.py tests/unit/meeting/test_meet_prejoin.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/meeting/providers/google_meet.py tests/unit/meeting/test_meet_profile.py
git commit -m "feat(meet): the room's browser keeps its Google session in a persistent profile"
```

---

### Task 3: Pre-join camera and microphone, the sign-in message, the lobby words

**Files:**
- Modify: `src/croom/meeting/providers/google_meet.py` (`_handle_prejoin`, `_click_join_button`, `_wait_for_connection`, new `_set_toggle` and `_needs_sign_in`)
- Modify: `tests/unit/meeting/test_meet_prejoin.py`

**Interfaces:**
- Produces: class attributes `TOGGLE_TIMEOUT_MS = 3000`, `CONNECT_TIMEOUT_MS = 30000`, `ADMIT_TIMEOUT_MS = 300000`, `LOBBY_PHRASES`; `_set_toggle(device: str, wanted_on: bool) -> None`; `_needs_sign_in() -> bool`; the expired-sign-in message text "The room's Google sign-in has expired or was never done; stop the service and run croom --sign-in-meet on the device".

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/meeting/test_meet_prejoin.py`:

```python

# A signed-in pre-join page: no name field, "Join now", a muted microphone and a camera that is on.
SIGNED_IN_FORM = """
<!DOCTYPE html><html><body>
<h2>Ready to join?</h2>
<button id="mic" aria-label="Turn on microphone (ctrl + d)">mic_off</button>
<button id="cam" aria-label="Turn off camera (ctrl + e)">videocam</button>
<button id="join">Join now</button>
<div id="meeting" hidden><button aria-label="Leave call">Leave</button></div>
<script>
const flip = (btn, device) => btn.addEventListener('click', () => {
  const on = btn.getAttribute('aria-label').startsWith('Turn off');
  btn.setAttribute('aria-label', (on ? 'Turn on ' : 'Turn off ') + device);
  btn.dataset.clicks = String(Number(btn.dataset.clicks || 0) + 1);
});
flip(document.getElementById('mic'), 'microphone (ctrl + d)');
flip(document.getElementById('cam'), 'camera (ctrl + e)');
document.getElementById('join').addEventListener('click', () => {
  setTimeout(() => { document.getElementById('meeting').hidden = false; }, 300);
});
</script>
</body></html>
"""

MIC_ONLY_FORM = SIGNED_IN_FORM.replace('<button id="cam" aria-label="Turn off camera (ctrl + e)">videocam</button>', "")

SIGN_IN_PAGE = """
<!DOCTYPE html><html><body>
<h1>Sign in</h1>
<p>to continue to Google Meet</p>
<input type="email" aria-label="Email or phone">
<button>Next</button>
</body></html>
"""

LOBBY_PAGE = """
<!DOCTYPE html><html><body>
<p>Asking to join…</p>
<p>You'll join the call when someone lets you in</p>
<div id="meeting" hidden><button aria-label="Leave call">Leave</button></div>
<script>setTimeout(() => { document.getElementById('meeting').hidden = false; }, 700);</script>
</body></html>
"""


async def signed_in(form, tmp_path, camera_on, mic_on):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(form)
        provider = GoogleMeetProvider(profile_dir=str(tmp_path / "profile"))
        provider._page = page
        provider.failure_screenshot = tmp_path / "meet-failure.png"
        try:
            await provider._handle_prejoin("Room 3", camera_on, mic_on)
            await provider._click_join_button()
            await page.wait_for_selector("#meeting:not([hidden])", timeout=5000)
            labels = await page.evaluate("() => [...document.querySelectorAll('#mic,#cam')].map(b => [b.id, b.getAttribute('aria-label'), b.dataset.clicks || '0'])")
            return {row[0]: (row[1], row[2]) for row in labels}
        finally:
            await browser.close()


async def test_turns_the_microphone_on_and_leaves_the_camera_alone_when_both_are_wanted_on(tmp_path):
    state = await signed_in(SIGNED_IN_FORM, tmp_path, camera_on=True, mic_on=True)
    assert state["mic"] == ("Turn off microphone (ctrl + d)", "1")
    assert state["cam"] == ("Turn off camera (ctrl + e)", "0")


async def test_turns_the_camera_off_and_leaves_the_microphone_alone_when_both_are_wanted_off(tmp_path):
    state = await signed_in(SIGNED_IN_FORM, tmp_path, camera_on=False, mic_on=False)
    assert state["cam"] == ("Turn on camera (ctrl + e)", "1")
    assert state["mic"] == ("Turn on microphone (ctrl + d)", "0")


async def test_a_missing_toggle_does_not_stop_the_join(tmp_path):
    state = await signed_in(MIC_ONLY_FORM, tmp_path, camera_on=True, mic_on=True)
    assert state["mic"] == ("Turn off microphone (ctrl + d)", "1")
    assert "cam" not in state


async def test_googles_sign_in_page_names_the_sign_in_command(tmp_path):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(SIGN_IN_PAGE)
        provider = GoogleMeetProvider(profile_dir=str(tmp_path / "profile"))
        provider._page = page
        provider.JOIN_FIND_TIMEOUT_MS = 300
        provider.TOGGLE_TIMEOUT_MS = 300
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider._click_join_button()
            assert "croom --sign-in-meet" in str(failure.value)
            assert "expired or was never done" in str(failure.value)
        finally:
            await browser.close()


async def test_waiting_to_be_admitted_is_the_lobby(tmp_path):
    from croom.meeting.providers.base import MeetingState
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(LOBBY_PAGE)
        provider = GoogleMeetProvider()
        provider._page = page
        provider.CONNECT_TIMEOUT_MS = 300
        provider.ADMIT_TIMEOUT_MS = 5000
        try:
            await provider._wait_for_connection()
            assert provider._state == MeetingState.IN_LOBBY
        finally:
            await browser.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_prejoin.py`
Expected: the five new tests FAIL: the microphone is never turned on, the camera test finds the camera untouched, the sign-in page gets the generic "Could not find Meet's join button", and the lobby test times out or errors because `CONNECT_TIMEOUT_MS` is not a class attribute.

- [ ] **Step 3: Implement**

In `src/croom/meeting/providers/google_meet.py` add after `JOIN_ENABLE_TIMEOUT_MS`:

```python
    TOGGLE_TIMEOUT_MS = 3000         # per selector while looking for a camera or microphone button
    CONNECT_TIMEOUT_MS = 30000       # for Meet's in-call controls after pressing Join
    ADMIT_TIMEOUT_MS = 300000        # how long a host may take to admit the room
    LOBBY_PHRASES = ("waiting for", "asking to join", "someone lets you in", "let you in")
    SIGN_IN_MESSAGE = ("The room's Google sign-in has expired or was never done; "
                       "stop the service and run croom --sign-in-meet on the device")
```

Replace the camera and microphone blocks in `_handle_prejoin` (everything from `# Toggle camera if needed` to the end of the method) with:

```python
        await self._set_toggle("camera", camera_on)
        await self._set_toggle("microphone", mic_on)

    async def _set_toggle(self, device: str, wanted_on: bool) -> None:
        """Put Meet's pre-join camera or microphone button in the wanted state. The label
        says the current state: "Turn on microphone" means it is off, "Turn off" means on."""
        button = await self._find_first(
            [f'button[aria-label*="{device}" i]', f'[role="button"][aria-label*="{device}" i]'],
            timeout=self.TOGGLE_TIMEOUT_MS,
        )
        if button is None:
            logger.warning(f"Meet pre-join {device} button not found; joining with Meet's default")
            return
        label = (await button.get_attribute("aria-label") or "").lower()
        if "turn on" not in label and "turn off" not in label:
            logger.warning(f"Meet pre-join {device} button has an unexpected label {label!r}; leaving it alone")
            return
        if ("turn off" in label) == wanted_on:
            return
        await button.click()
        await asyncio.sleep(0.5)
        label = (await button.get_attribute("aria-label") or "").lower()
        logger.info(f"Meet pre-join {device} is now {'on' if 'turn off' in label else 'off'} "
                    f"(wanted {'on' if wanted_on else 'off'})")
```

In the name-typing block of `_handle_prejoin`, change the `else:` warning to:

```python
        elif self._profile_dir is None:
            logger.warning("Meet pre-join name field not found; trying to join without a name")
        else:
            logger.debug("Signed in: Meet shows no name field")
```

In `_click_join_button`, replace `raise await self._failure("Could not find Meet's join button")` with:

```python
            if await self._needs_sign_in():
                raise RuntimeError(self.SIGN_IN_MESSAGE)
            raise await self._failure("Could not find Meet's join button")
```

and add the helper next to `_page_words`:

```python
    async def _needs_sign_in(self) -> bool:
        """True on Google's sign-in page: the profile's session is gone or was never made."""
        if "accounts.google.com" in (self._page.url or "").lower():
            return True
        return (await self._page_words()).lower().startswith("sign in")
```

Replace `_wait_for_connection`:

```python
    async def _wait_for_connection(self) -> None:
        """Wait for Meet's in-call controls; a knock that is waiting for the host is the lobby."""
        try:
            await self._page.wait_for_selector('[aria-label*="Leave" i]', timeout=self.CONNECT_TIMEOUT_MS)
            return
        except Exception:
            pass
        words = (await self._page_words()).lower()
        if any(phrase in words for phrase in self.LOBBY_PHRASES):
            self._set_state(MeetingState.IN_LOBBY)
            logger.info("Asked to join; waiting for the host to admit the room")
            try:
                await self._page.wait_for_selector('[aria-label*="Leave" i]', timeout=self.ADMIT_TIMEOUT_MS)
                return
            except Exception:
                raise await self._failure("The host did not admit the room in time")
        raise await self._failure("Failed to join meeting")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_prejoin.py tests/unit/meeting/test_meet_profile.py tests/unit/meeting/test_meet_check.py`
Expected: PASS. If `test_waiting_to_be_admitted_is_the_lobby` fails because the page's words do not contain a lobby phrase, check `LOBBY_PHRASES` against the replica: "asking to join" is there.

- [ ] **Step 5: Commit**

```bash
git add src/croom/meeting/providers/google_meet.py tests/unit/meeting/test_meet_prejoin.py
git commit -m "feat(meet): pre-join camera and microphone follow the config; a lapsed sign-in names the fix; knocking is the lobby"
```

---

### Task 4: `croom --sign-in-meet` and the browsers-path default

**Files:**
- Create: `src/croom/meeting/browser_env.py`
- Create: `src/croom/meeting/meet_signin.py`
- Modify: `src/croom/meeting/meet_check.py` (call `ensure_browsers_path()`)
- Modify: `src/croom/core/agent.py` (flag and handling)
- Create: `tests/unit/meeting/test_meet_signin.py`

**Interfaces:**
- Produces: `ensure_browsers_path(env=os.environ, candidate=Path("/opt/croom/browsers")) -> Optional[str]`; `sign_in_meet(profile_dir: Path, out=sys.stdout, display: Optional[str] = None, executable: Optional[str] = None, runner=subprocess.run) -> int`; `chromium_executable() -> str`; the `--sign-in-meet` flag.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_meet_signin.py`:

```python
"""
`croom --sign-in-meet` opens the room's own Chromium plainly, on the room's
screen, so a person signs the room in once (spec 2026-10-07, section 4.4).
"""

import io
import subprocess
import sys
from types import SimpleNamespace

from croom.meeting.browser_env import ensure_browsers_path
from croom.meeting.meet_signin import SIGN_IN_URL, sign_in_meet


def test_refuses_without_a_display(tmp_path):
    out = io.StringIO()
    assert sign_in_meet(tmp_path / "profile", out=out, display=None, executable="/opt/chrome", runner=lambda *a, **k: None) == 1
    assert "DISPLAY=:0" in out.getvalue()
    assert not (tmp_path / "profile").exists()


def test_refuses_while_the_service_holds_the_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("")
    out = io.StringIO()
    calls = []
    assert sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=lambda *a, **k: calls.append(a)) == 1
    assert "sudo systemctl stop croom" in out.getvalue()
    assert calls == []


def test_runs_the_bundled_chromium_plainly_and_reports_success(tmp_path):
    profile = tmp_path / "profile"
    calls = []

    def runner(cmd, check=False, env=None):
        calls.append((cmd, env))
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "Cookies").write_text("session")
        return SimpleNamespace(returncode=0)

    out = io.StringIO()
    code = sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=runner)
    assert code == 0, out.getvalue()
    [(cmd, env)] = calls
    assert cmd[0] == "/opt/chrome" and cmd[-1] == SIGN_IN_URL == "https://accounts.google.com/"
    assert f"--user-data-dir={profile}" in cmd and "--no-first-run" in cmd
    assert not any(arg.startswith("--remote-debugging") or arg == "--enable-automation" for arg in cmd)
    assert env["DISPLAY"] == ":0"
    assert oct(profile.stat().st_mode & 0o777) == "0o700"
    text = out.getvalue()
    assert "Sign in there as the room's Google user" in text and "Profile saved" in text and "--check-meet" in text


def test_reports_when_no_session_was_saved(tmp_path):
    out = io.StringIO()
    code = sign_in_meet(tmp_path / "profile", out=out, display=":0", executable="/opt/chrome",
                        runner=lambda *a, **k: SimpleNamespace(returncode=0))
    assert code == 1
    assert "No session was saved" in out.getvalue()


def test_command_line_flag_exists():
    result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--help"], capture_output=True, text=True)
    assert "--sign-in-meet" in result.stdout


def test_browsers_path_defaults_to_the_installed_folder(tmp_path):
    env = {}
    assert ensure_browsers_path(env, candidate=tmp_path) == str(tmp_path)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path)
    env = {"PLAYWRIGHT_BROWSERS_PATH": "/elsewhere"}
    assert ensure_browsers_path(env, candidate=tmp_path) == "/elsewhere"
    assert ensure_browsers_path({}, candidate=tmp_path / "missing") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_signin.py`
Expected: FAIL at import, `croom.meeting.browser_env` does not exist.

- [ ] **Step 3: Write the two modules**

Create `src/croom/meeting/browser_env.py`:

```python
"""
Playwright finds the bundled Chromium through PLAYWRIGHT_BROWSERS_PATH; the room
installer puts it under /opt/croom/browsers and the service unit sets the variable.
A command run by hand gets the same default when that folder exists, so only
DISPLAY has to be typed.
"""

import os
from pathlib import Path
from typing import MutableMapping, Optional

DEFAULT_BROWSERS_PATH = Path("/opt/croom/browsers")


def ensure_browsers_path(env: MutableMapping[str, str] = os.environ,
                         candidate: Path = DEFAULT_BROWSERS_PATH) -> Optional[str]:
    """The browsers folder Playwright will use, after defaulting it; None when unknown."""
    if env.get("PLAYWRIGHT_BROWSERS_PATH"):
        return env["PLAYWRIGHT_BROWSERS_PATH"]
    if candidate.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
        return str(candidate)
    return None
```

Create `src/croom/meeting/meet_signin.py`:

```python
"""
`croom --sign-in-meet`: open the room's own Chromium plainly, with no automation,
on the room's screen, so a person signs the room in to its Google Workspace
account once (spec 2026-10-07 Google Meet, section 4.4). The session stays in
the profile folder the Meet provider uses.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, TextIO

from croom.meeting.browser_env import ensure_browsers_path

SIGN_IN_URL = "https://accounts.google.com/"


def chromium_executable() -> str:
    """The bundled Chromium's binary, found the way Playwright finds it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return p.chromium.executable_path


def sign_in_meet(profile_dir: Path, out: TextIO = sys.stdout, display: Optional[str] = None,
                 executable: Optional[str] = None, runner: Callable = subprocess.run) -> int:
    if not display:
        print("Run this on the room device with its screen, for example: "
              "DISPLAY=:0 croom --sign-in-meet -c /etc/croom/config.yaml", file=out)
        return 1
    if (profile_dir / "SingletonLock").exists():
        print(f"The room service is using {profile_dir}; stop it first: sudo systemctl stop croom", file=out)
        return 1
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    ensure_browsers_path()
    executable = executable or chromium_executable()
    print("A browser opens on the room's screen. Sign in there as the room's Google user, "
          "then close the browser window.", file=out)
    print(f"Profile: {profile_dir}", file=out)
    runner([executable, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1920,1080", SIGN_IN_URL], check=False, env={**os.environ, "DISPLAY": display})
    if (profile_dir / "Default" / "Cookies").exists():
        print("Profile saved. Test it with: croom --check-meet <meeting link> -c /etc/croom/config.yaml, "
              "then start the service: sudo systemctl start croom", file=out)
        return 0
    print("No session was saved: the browser closed before the sign-in finished. Run this again.", file=out)
    return 1
```

In `src/croom/meeting/meet_check.py` add `from croom.meeting.browser_env import ensure_browsers_path` to the imports and, as the first line of `check_meet`'s body, `ensure_browsers_path()`.

- [ ] **Step 4: Add the flag**

In `src/croom/core/agent.py`, after the `--profile` `add_argument`, add:

```python
    parser.add_argument(
        "--sign-in-meet",
        action="store_true",
        help="Open the room's browser on its screen to sign in to Google once (needs DISPLAY and meeting.google_profile_dir), then exit",
    )
```

and after the `if args.check_meet:` block:

```python
    if args.sign_in_meet:
        import os
        from pathlib import Path
        from croom.meeting.meet_signin import sign_in_meet
        profile = load_config(args.config).meeting.google_profile_dir
        if not profile:
            print("Set meeting.google_profile_dir in the config first; the room configs use /var/lib/croom/meet-profile", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(sign_in_meet(Path(profile), display=os.environ.get("DISPLAY")))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_signin.py tests/unit/meeting/test_meet_check.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/croom/meeting/browser_env.py src/croom/meeting/meet_signin.py src/croom/meeting/meet_check.py src/croom/core/agent.py tests/unit/meeting/test_meet_signin.py
git commit -m "feat(meet): croom --sign-in-meet opens the room's browser plainly for the one-time Google sign-in"
```

---

### Task 5: The check uses the configured profile

**Files:**
- Modify: `src/croom/meeting/meet_check.py` (new `resolve_profile`)
- Modify: `src/croom/core/agent.py` (`--check-meet` handling)
- Modify: `tests/unit/meeting/test_meet_check.py`

**Interfaces:**
- Produces: `resolve_profile(explicit: Optional[str], configured: str) -> Optional[Path]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/meeting/test_meet_check.py`:

```python


def test_check_profile_comes_from_the_config_unless_given():
    from pathlib import Path
    from croom.meeting.meet_check import resolve_profile
    assert resolve_profile(None, "/var/lib/croom/meet-profile") == Path("/var/lib/croom/meet-profile")
    assert resolve_profile("/tmp/other", "/var/lib/croom/meet-profile") == Path("/tmp/other")
    assert resolve_profile(None, "") is None


async def test_check_without_a_configured_profile_is_a_guest(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(PREJOIN, encoding="utf-8")
    out = io.StringIO()
    code = await check_meet(page.as_uri(), out=out, headless=True, settle_ms=300, profile=None)
    assert code == 0
    assert "as a guest" in out.getvalue() and "Profile:" not in out.getvalue()


def test_check_command_reads_the_profile_from_the_config():
    import inspect
    from croom.core import agent
    source = inspect.getsource(agent)
    assert "resolve_profile(args.profile, load_config(args.config).meeting.google_profile_dir)" in source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_check.py`
Expected: FAIL, `resolve_profile` does not exist; the agent source lacks the call.

- [ ] **Step 3: Implement**

In `src/croom/meeting/meet_check.py` add after the imports:

```python
def resolve_profile(explicit: Optional[str], configured: str) -> Optional[Path]:
    """The profile the check opens: an explicit --profile wins, then the config's, else a guest."""
    if explicit:
        return Path(explicit)
    return Path(configured) if configured else None
```

In `src/croom/core/agent.py` replace the `--check-meet` block with:

```python
    if args.check_meet:
        import tempfile
        from pathlib import Path
        from croom.meeting.meet_check import check_meet, resolve_profile
        profile = resolve_profile(args.profile, load_config(args.config).meeting.google_profile_dir)
        raise SystemExit(asyncio.run(check_meet(args.check_meet, screenshot=Path(tempfile.gettempdir()) / "croom-meet-check.png",
                                                profile=profile)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_check.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/meeting/meet_check.py src/croom/core/agent.py tests/unit/meeting/test_meet_check.py
git commit -m "feat(meet): croom --check-meet uses the configured profile"
```

---

### Task 6: Installer makes the profile folder and names the sign-in

**Files:**
- Modify: `installer/install.sh` (`create_directories`, `print_completion`)
- Modify: `tests/unit/installer/test_install_script.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/installer/test_install_script.py`:

```python


def test_profile_folder_is_private_to_the_service_user(tmp_path):
    result = run_bash(
        f"source {SCRIPT}; INSTALL_DIR={tmp_path / 'opt'}; CONFIG_DIR={tmp_path / 'etc'}; DATA_DIR={tmp_path / 'lib'}; "
        f"LOG_DIR={tmp_path / 'log'}; CROOM_USER=$(id -un); create_directories"
    )
    assert result.returncode == 0, result.stderr
    profile = tmp_path / "lib" / "meet-profile"
    assert profile.is_dir() and oct(profile.stat().st_mode & 0o777) == "0o700"


def test_completion_names_the_meet_sign_in_when_the_room_config_has_a_profile(tmp_path):
    room = tmp_path / "room.yaml"
    room.write_text("meeting:\n  google_profile_dir: /var/lib/croom/meet-profile\n")
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG={room}; CROOM_USER=pi; print_completion")
    assert result.returncode == 0, result.stderr
    assert "sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --sign-in-meet -c /etc/croom/config.yaml" in result.stdout
    assert "sudo systemctl stop croom" in result.stdout
    plain = tmp_path / "plain.yaml"
    plain.write_text("meeting:\n  platforms: [zoom]\n")
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG={plain}; CROOM_USER=pi; print_completion")
    assert "--sign-in-meet" not in result.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/installer/test_install_script.py -k "profile_folder or meet_sign_in"`
Expected: FAIL, no `meet-profile` folder; no sign-in lines.

- [ ] **Step 3: Implement**

In `installer/install.sh`, in `create_directories`, after `mkdir -p "$INSTALL_DIR/models"`:

```bash
    # The room's signed-in Google Meet profile: the service user's alone (spec 2026-10-07, section 4.6)
    mkdir -p "$DATA_DIR/meet-profile"
    chmod 700 "$DATA_DIR/meet-profile"
```

In `print_completion`, after the Zoom block (`if [[ -n "$ZOOM_CREDENTIALS_FILE" ]]; then ... fi`):

```bash
    if [[ -n "$ROOM_CONFIG" ]] && grep -q "google_profile_dir" "$ROOM_CONFIG"; then
        echo ""
        echo "Sign the room in to Google Meet once, with a keyboard on this device:"
        echo "  sudo systemctl stop croom"
        echo "  sudo -u $CROOM_USER DISPLAY=:0 $INSTALL_DIR/venv/bin/croom --sign-in-meet -c $CONFIG_DIR/config.yaml"
        echo "  sudo systemctl start croom"
    fi
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/installer/test_install_script.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add installer/install.sh tests/unit/installer/test_install_script.py
git commit -m "feat(installer): the Meet profile folder, and the sign-in steps in the completion text"
```

---

### Task 7: The Meet guide, README and the other docs

**Files:**
- Create: `docs/guides/crystal-meet-google-meet/index.html`, `build.py`, logo and fonts copied from the Zoom guide
- Create: `docs/guides/crystal-meet-google-meet.pdf` (rendered)
- Create: `tests/unit/docs/test_google_meet_guide.py`
- Modify: `README.md`, `deploy/rooms/README.md`, `docs/guides/crystal-meet-room-setup/index.html` and its PDF
- Modify: `tests/unit/docs/test_readme.py`, `tests/unit/docs/test_room_setup_guide.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/docs/test_google_meet_guide.py`:

```python
"""
The Google Meet guide builds to a multi-page PDF, is self-contained, and quotes
the real commands, names and paths.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "docs" / "guides"
GUIDE = GUIDES / "crystal-meet-google-meet"

pytest.importorskip("playwright.sync_api")


def test_meet_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 2, counts


def test_meet_guide_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset


def test_meet_guide_quotes_the_real_commands_and_names():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    for needle in ("room1@crystalpm.com", "Organizational units", "session control", "2-Step Verification",
                   "sudo systemctl stop croom", "--sign-in-meet -c /etc/croom/config.yaml", "DISPLAY=:0",
                   "sudo systemctl start croom", "croom --check-meet", "google_profile_dir",
                   "/var/lib/croom/meet-profile", "Join now", "Ask to join", "expired or was never done",
                   "You can't join this video call"):
        assert needle in html, needle


def test_meet_guide_uses_the_shared_renderer():
    assert "from render_guide import render" in (GUIDE / "build.py").read_text(encoding="utf-8")
```

In `tests/unit/docs/test_readme.py` replace `"Follow the four guides",` with:

```python
        "Follow the five guides",
        "docs/guides/crystal-meet-google-meet.pdf",
        "--sign-in-meet",
```

In `tests/unit/docs/test_room_setup_guide.py` append:

```python


def test_guide_sends_the_reader_to_the_meet_guide_for_the_sign_in():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "Let Crystal Meet rooms join Google Meet" in html
    assert "joins as a guest" not in html
```

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs/test_google_meet_guide.py tests/unit/docs/test_readme.py tests/unit/docs/test_room_setup_guide.py`
Expected: FAIL, the guide folder is missing and the needles are absent.

- [ ] **Step 2: Create the guide folder, assets and build script**

```bash
mkdir -p docs/guides/crystal-meet-google-meet
cp docs/guides/crystal-meet-zoom/crystalpm-logo-white.svg docs/guides/crystal-meet-google-meet/
cp -r docs/guides/crystal-meet-zoom/fonts docs/guides/crystal-meet-google-meet/
```

Create `docs/guides/crystal-meet-google-meet/build.py`:

```python
"""
Render the Google Meet guide to PDF.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-google-meet.pdf)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from render_guide import render  # noqa: E402

if __name__ == "__main__":
    render(HERE, Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "crystal-meet-google-meet.pdf",
           "How-to guide · Let Crystal Meet rooms join Google Meet")
```

- [ ] **Step 3: Write the guide**

Build `docs/guides/crystal-meet-google-meet/index.html` from the Zoom guide's head, taken up to and including its `</head>` line (`awk '{print} /^<\/head>$/ {exit}' docs/guides/crystal-meet-zoom/index.html`, then change the `<title>` to `Let Crystal Meet rooms join Google Meet`), followed by this body:

```html
<body>

<section class="banner">
  <img src="crystalpm-logo-white.svg" alt="Crystal PM">
  <p class="kicker top">Crystal PM</p>
  <p class="kicker">How-to guide</p>
  <h1>Let Crystal Meet rooms join Google Meet</h1>
  <p>Give each room its own Google Workspace account and sign it in once, so Meet treats the room like a colleague instead of an unknown guest.</p>
</section>

<p class="intro">Google Meet refuses guests that arrive through an automated browser, which is how a Crystal Meet room joins. A room that is signed in as a Workspace user of its own is accepted, joins your company's meetings without knocking, and asks to join everyone else's under its own name. The Workspace half takes an admin about fifteen minutes for all rooms; the sign-in on each room's Pi takes five minutes with a keyboard.</p>

<div class="glance">
  <div class="card"><p class="kicker">Step 1</p><h3>A user per room</h3><p>room1@, room2@, room3@, named after the rooms.</p></div>
  <div class="card"><p class="kicker">Step 2</p><h3>Their own unit</h3><p>Sessions that never expire, no 2-step prompt.</p></div>
  <div class="card"><p class="kicker">Step 3</p><h3>Sign the room in</h3><p>Once per Pi, on its screen.</p></div>
  <div class="card"><p class="kicker">Step 4</p><h3>Test it</h3><p>An internal meeting, then an outside one.</p></div>
</div>

<div class="callout warn">
  <p class="kicker">Before you begin</p>
  <p><strong>Access.</strong> A Google Workspace super admin, or an admin who can create users and change security settings for an organizational unit.</p>
  <p><strong>Cost.</strong> One Workspace seat per room. A single shared rooms account on all Pis is cheaper, but then every room appears under one name in calls and nobody can tell which room is which; this guide assumes one account per room.</p>
  <p><strong>Secrets.</strong> Each room user's password is typed once on its Pi and then kept with the room's other secrets. The signed-in session lives on the Pi in <span class="chip">/var/lib/croom/meet-profile</span>, readable by the room service only.</p>
</div>

<section class="block">
<div class="step"><span class="badge">1</span><h2>Create a Workspace user for each room</h2></div>
<ul>
  <li><strong>Add the user.</strong> In the Admin console open <span class="ui">Directory › Users › Add new user</span>. First name <span class="chip">Room</span>, last name <span class="chip">1</span>, primary email <span class="chip">room1@crystalpm.com</span>. Set a strong password yourself rather than letting Google generate one you have to change on first sign-in.</li>
  <li><strong>Repeat</strong> for <span class="chip">room2@crystalpm.com</span> and <span class="chip">room3@crystalpm.com</span>. The display name is what hosts see when the room joins, so keep it the room's name.</li>
  <li><strong>Nothing else is needed.</strong> No Gmail, Drive or Calendar access is required for joining; any Workspace edition's seat works.</li>
</ul>
<p class="see">You should now see three users named Room 1, Room 2 and Room 3 in the user list.</p>
</section>

<section class="block">
<div class="step"><span class="badge">2</span><h2>Put them in a unit whose sessions never expire</h2></div>
<ul>
  <li><strong>Make the unit.</strong> <span class="ui">Directory › Organizational units › Create organizational unit</span>, named <span class="chip">Meeting rooms</span>. Then select the three room users in the user list and use <span class="ui">Change organizational unit</span> to move them into it.</li>
  <li><strong>Keep them signed in.</strong> <span class="ui">Security › Access and data control › Google session control</span>, select the Meeting rooms unit on the left, and set the web session duration to never expire (or the longest choice offered). Without this Google signs the rooms out on its schedule and someone has to sign them in again.</li>
  <li><strong>No second factor on the Pi.</strong> <span class="ui">Security › Authentication › 2-Step Verification</span>, the Meeting rooms unit: allow users to turn it on, but do not enforce it, so the one-time sign-in needs only the password.</li>
  <li><strong>Optional.</strong> Under <span class="ui">Apps › Google Workspace</span> you can turn Gmail and Drive off for the unit; Meet and Calendar stay on.</li>
</ul>
<p class="see">You should now see the three users inside Meeting rooms, with session control and 2-Step Verification set for that unit.</p>
</section>

<section class="block">
<div class="step"><span class="badge">3</span><h2>Sign each room in, once</h2></div>
<ul>
  <li><strong>Check the config.</strong> The room configs from <span class="chip">deploy/rooms/</span> already carry <span class="chip">google_profile_dir: /var/lib/croom/meet-profile</span> under <span class="ui">meeting</span>. A room installed before this guide needs that line added to <span class="chip">/etc/croom/config.yaml</span>.</li>
  <li><strong>Plug a keyboard and mouse into the Pi</strong>, then from SSH stop the room service so the browser is free: <span class="chip">sudo systemctl stop croom</span>.</li>
  <li><strong>Open the sign-in.</strong> Type <span class="chip wrap">sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --sign-in-meet -c /etc/croom/config.yaml</span> (your username instead of pi). The room's own browser opens on the TV at Google's sign-in page.</li>
  <li><strong>Sign in on the TV</strong> as that room's user, for example <span class="chip">room1@crystalpm.com</span>, accept Google's first-run prompts, and when the account page shows, close the browser window. The command prints "Profile saved".</li>
  <li><strong>Start the service again:</strong> <span class="chip">sudo systemctl start croom</span>.</li>
</ul>
<p class="see">You should now see "Profile saved" in the terminal, and the room service running again.</p>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>The profile folder belongs to the room service's user and nobody else. Running the sign-in again later only re-uses it; if the session ever lapses, the room page says the sign-in has expired or was never done, and these same three commands fix it.</p>
</div>
</section>

<section class="block">
<div class="step"><span class="badge">4</span><h2>Test it</h2></div>
<ul>
  <li><strong>An internal meeting.</strong> Start a meeting from a crystalpm.com account, paste its link into the room page's <span class="ui">Join with a link</span>, or book it on the room's calendar and press <span class="ui">Join now</span>. The room is in the call within about fifteen seconds; Meet shows it a <span class="ui">Join now</span> button, so nobody has to admit it.</li>
  <li><strong>An outside meeting.</strong> Join a meeting hosted by a personal Google account or another company. The room asks to join under its own name, the room page says it is waiting, and the host admits it with <span class="ui">Ask to join</span>'s prompt.</li>
  <li><strong>Camera and microphone.</strong> Both start in the state the room config asks for (<span class="ui">camera_default_on</span>, <span class="ui">mic_default_on</span>), whatever Meet remembered last time. Mute and camera buttons on the table page still work in the call.</li>
  <li><strong>From a terminal.</strong> <span class="chip wrap">sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --check-meet &lt;meeting link&gt; -c /etc/croom/config.yaml</span> opens the link from the signed-in profile and prints what Meet showed, with a screenshot under <span class="chip">/tmp</span>. Stop the service first, as for the sign-in.</li>
</ul>
<p class="see">You should now see the room join the internal meeting on its own and the outside meeting after being admitted.</p>
</section>

<div class="page-break"></div>
<section class="block">
<p class="kicker">If something is off</p>
<h2>Troubleshooting</h2>
<div class="trouble">
  <div class="card"><h3>The room page says the sign-in has expired or was never done</h3><p>Google's sign-in page came up instead of the meeting. Repeat step 3. If it keeps happening every couple of weeks, the session control in step 2 is not applied to the Meeting rooms unit.</p></div>
  <div class="card"><h3>The room page quotes "You can't join this video call"</h3><p>The room is joining as a guest: either <span class="chip">google_profile_dir</span> is missing from <span class="chip">/etc/croom/config.yaml</span>, or the sign-in in step 3 was closed before it finished. Check the config, then repeat step 3.</p></div>
  <div class="card"><h3>The wrong name shows in the call</h3><p>Meet shows the Google account's name. Change the user's first and last name in the Admin console, or sign the Pi in as the right room user.</p></div>
  <div class="card"><h3>Camera or microphone off in the call</h3><p>The room sets both from the config before joining; if a device is missing, Meet's own default applies and the log says which button was not found. Check the camera and speakerphone are plugged into the Pi and seen by <span class="chip">arecord -l</span> and <span class="chip">v4l2-ctl --list-devices</span>.</p></div>
  <div class="card"><h3>The sign-in command says the service is using the profile</h3><p>Stop the room service first with <span class="chip">sudo systemctl stop croom</span>, run the command again, and start the service when done.</p></div>
  <div class="card"><h3>Google asks for a phone or a second factor on the Pi</h3><p>2-Step Verification is enforced for the room users. Set the Meeting rooms unit to allow but not enforce it (step 2), wait a few minutes, and try again.</p></div>
</div>
</section>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>The room's browser is never disguised: it announces itself as an automated browser, which is exactly why a signed-in account is needed. To take a room's access away, suspend its user in the Admin console.</p>
</div>

</body>
</html>
```

Build it: `.venv/bin/python docs/guides/crystal-meet-google-meet/build.py`, then render page images with `pymupdf` as for the other guides and check nothing overflows.

- [ ] **Step 4: README, deploy notes, room setup guide**

Apply these replacements to `README.md` (each `old` occurs exactly once):

```python
import pathlib
p = pathlib.Path("README.md"); s = p.read_text()
def sub(old, new):
    global s
    assert s.count(old) == 1, old[:50]; s = s.replace(old, new)

sub("Follow the four guides in this order:", "Follow the five guides in this order:")
sub("""4. [Connect Crystal Meet rooms to Zoom](docs/guides/crystal-meet-zoom.pdf): a""",
    """4. [Let Crystal Meet rooms join Google Meet](docs/guides/crystal-meet-google-meet.pdf):
   one Workspace user per room in a unit whose sessions never expire, and the
   one-time sign-in on each Pi, so Meet accepts the room instead of refusing an
   automated guest.
5. [Connect Crystal Meet rooms to Zoom](docs/guides/crystal-meet-zoom.pdf): a""")
sub("""PLAYWRIGHT_BROWSERS_PATH=/opt/croom/browsers DISPLAY=:0 /opt/croom/venv/bin/croom --check-meet https://meet.google.com/abc-defg-hij   # what Meet shows a guest""",
    """sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --sign-in-meet -c /etc/croom/config.yaml   # once per room, service stopped
sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --check-meet https://meet.google.com/abc-defg-hij -c /etc/croom/config.yaml""")
sub("""- Google Meet joins as a guest, so someone in the meeting must admit the room; Zoom links need their passcode in the link. A failed Meet join logs what Meet showed and saves a screenshot under `/tmp`; `croom --check-meet URL` reports the same from a terminal.""",
    """- Zoom links need their passcode in the link. A failed Meet join logs what Meet showed and saves a screenshot under `/tmp`; `croom --check-meet URL -c CONFIG` reports the same from a terminal.""")
sub("""- Zoom is joined through Zoom's Meeting SDK, never by driving the public web client, which blocks automated guests.""",
    """- Google Meet refuses guests that arrive through an automated browser, so each room joins Meet signed in as its own Workspace user from a persistent browser profile (`meeting.google_profile_dir`); the browser is never disguised. `croom --sign-in-meet` does the one-time sign-in on the room's screen.
- Zoom is joined through Zoom's Meeting SDK, never by driving the public web client, which blocks automated guests.""")
sub("""| The dashboard on a Raspberry Pi: Docker Compose packaging, production mode in the backend, the dashboard installer and guide | [spec](docs/superpowers/specs/2026-10-05-dashboard-on-a-pi-design.md) | [plan](docs/superpowers/plans/2026-10-05-dashboard-on-a-pi.md) |""",
    """| The dashboard on a Raspberry Pi: Docker Compose packaging, production mode in the backend, the dashboard installer and guide | [spec](docs/superpowers/specs/2026-10-05-dashboard-on-a-pi-design.md) | [plan](docs/superpowers/plans/2026-10-05-dashboard-on-a-pi.md) |
| Google Meet as a signed-in room: the persistent profile, `croom --sign-in-meet`, pre-join camera and microphone, the Meet guide | [spec](docs/superpowers/specs/2026-10-07-google-meet-room-account-design.md) | [plan](docs/superpowers/plans/2026-10-07-google-meet-room-account.md) |""")
p.write_text(s); print("README updated")
```

In `deploy/rooms/README.md`, after the `calendar.google_calendar_id` bullet's paragraph, add a bullet:

```markdown
- `meeting.google_profile_dir`: where the room keeps its signed-in Google Meet
  session, `/var/lib/croom/meet-profile`. Leave it; the guide "Let Crystal Meet
  rooms join Google Meet" covers the room's Workspace user and the one-time
  `croom --sign-in-meet` on the device.
```

In `docs/guides/crystal-meet-room-setup/index.html`:

- change `Google Meet joins as a guest, so someone in the meeting admits the room when it asks.` to `Google Meet needs the room signed in once as its own Workspace user; the guide "Let Crystal Meet rooms join Google Meet" covers it.`
- in the troubleshooting card, change `Google Meet waits until someone in the meeting admits "the room", and guests must be allowed by your Google Workspace admin (Meet safety settings).` to `Google Meet needs the room signed in (the guide "Let Crystal Meet rooms join Google Meet"); the room page says so when the sign-in is missing or expired.`

Rebuild its PDF: `.venv/bin/python docs/guides/crystal-meet-room-setup/build.py`.

- [ ] **Step 5: Run the docs tests**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs tests/unit/deploy`
Expected: PASS, including `test_readme_links_every_spec_and_plan`.

- [ ] **Step 6: Commit**

```bash
git add docs/guides/crystal-meet-google-meet docs/guides/crystal-meet-google-meet.pdf tests/unit/docs/test_google_meet_guide.py README.md deploy/rooms/README.md docs/guides/crystal-meet-room-setup/index.html docs/guides/crystal-meet-room-setup.pdf tests/unit/docs/test_readme.py tests/unit/docs/test_room_setup_guide.py
git commit -m "docs: the Google Meet guide; README and room docs point at the signed-in route"
```

---

### Task 8: The suite gate and the Pi acceptance

**Files:** none new.

- [ ] **Step 1: Run the full gate**

Run: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh meet-account | grep "GATE:"`
Expected: `GATE: PASSED` with no new failures beyond the 81 upstream ones.

- [ ] **Step 2: Hand Ben the acceptance on PiMeet-3**

After the branch is merged and pushed, the commands for the Pi, in order:

```bash
sudo /opt/croom/venv/bin/pip install -q --upgrade --force-reinstall --no-deps git+https://github.com/ben-abeo/croom.to.git
sudo grep -q google_profile_dir /etc/croom/config.yaml || sudo sed -i 's|^  zoom_credentials_path:.*|&\n  google_profile_dir: /var/lib/croom/meet-profile|' /etc/croom/config.yaml
sudo install -d -o cpm -g cpm -m 700 /var/lib/croom/meet-profile
sudo systemctl stop croom
sudo -u cpm DISPLAY=:0 /opt/croom/venv/bin/croom --sign-in-meet -c /etc/croom/config.yaml
sudo -u cpm DISPLAY=:0 /opt/croom/venv/bin/croom --check-meet <meeting link> -c /etc/croom/config.yaml
sudo systemctl start croom
rm -rf /home/cpm/meet-profile
```

Expected: "Profile saved"; the check reports "Join control: found: 'Join now'" for an internal meeting; a press of Join on the room page connects with camera and microphone on; an outside-hosted meeting shows waiting, then connects when admitted; the log shows "Meet pre-join microphone is now on". Record what was proven and what was not in the final message.
