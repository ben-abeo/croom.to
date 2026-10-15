# TV Remote and Touchpad Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The table replaces the LG C6's remote and a mouse on the room Pi: every remote key, power, inputs, channels, volume, a trackpad for the TV's cursor, a virtual mouse on the room Pi, and a full-screen touchpad mode, from both the idle and the meeting screens.

**Architecture:** A new `croom.tv` service on the room Pi talks to the TV over LG's webOS socket through `aiowebostv` (pairing with a stored client key, state subscriptions, controls, the pointer socket), wakes it with Wake-on-LAN, and keeps it on by policy. A `PiMouse` built on `evdev.UInput` is the second pointer target. The control service exposes `tv` in `/api/status`, `POST /api/tv/control|pair|forget`, `GET /api/tv/discover` and `WS /ws/pointer`. The room page gets a `tv` view (the remote with the d-pad disc that becomes the trackpad) reachable from Options and from the meeting screen, and a full-screen touchpad surface.

**Tech Stack:** Python 3.12, aiohttp (routes and WebSocket), `aiowebostv`, `wakeonlan`, `evdev`, vanilla JS/CSS, Playwright tests, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-tv-remote-and-touchpad-design.md`

**Base:** branch `tv-remote` from `main`. Tasks 1–5 (the service, the routes, the pointer relay, the installer) touch no room-page file. Before Task 6, merge branch `meeting-controls` into `tv-remote` (it brings the icon sprite, the meeting screen's secondary row and the sub-screen pattern the page tasks build on); the merge commit is reviewed by the whole-branch review.

## Global Constraints

- Internal names stay `croom`; Crystal Meet in copy; no attribution lines in commits; no secrets (the client key is a runtime file, never in the repo or the config).
- Tests: `.venv/bin/pytest -q -p no:cacheprovider -W error <path>`; never the whole suite from a task. The TV library, Wake-on-LAN and `evdev` are never touched by tests: every task injects a fake.
- `aiowebostv`, `wakeonlan` and `evdev` are imported lazily inside the modules that use them, so the agent runs where they are missing (a room without a TV) and the tests need no `/dev/uinput`.
- A control the TV cannot carry out raises `TvError` with words; routes answer 409 with those words, never 500.
- The table shows what the TV has: every control re-reads the state; the page draws from `tv` in the status.
- The room page keeps its fixed layout at 1280 by 800 (`fits()`), scrolling on a phone; icons on every button from the page's sprite.
- Nothing on the TV ever needs the TV's own remote after pairing.

## Review Focus

1. A Wake-on-LAN that never wakes the TV (wrong MAC, "Mobile TV On" off) must leave a join proceeding after the 15 s cap and the table saying the TV is off with words, not hang (Task 2 test `test_a_join_waits_at_most_fifteen_seconds_for_a_tv_that_never_wakes`).
2. A manual Off from the table must hold against `keep_on` until a manual On or a meeting join (Task 2 test `test_a_manual_off_holds_until_on_or_a_join`).
3. The pointer socket must survive a burst of moves faster than the TV accepts and a client that vanishes mid-drag (Task 5 tests `test_a_burst_of_moves_is_coalesced_not_queued` and `test_a_dropped_socket_releases_a_held_button`).
4. The `tv` view opened from a meeting must return to the meeting screen, and one opened from Options to Options, and the full-screen touchpad must return to whichever opened it (Task 7 test `test_the_touchpad_returns_to_the_screen_that_opened_it`).
5. An unpaired or unconfigured room must show the pairing card and never a dead remote (Task 6 test `test_an_unpaired_room_shows_the_pairing_card_not_a_dead_remote`).

---

### Task 1: The TV service: config, the client key, pairing and state

**Files:**
- Modify: `src/croom/core/config.py` (`TvConfig`, `Config.tv`, from_dict/to_dict)
- Create: `src/croom/tv/__init__.py`, `src/croom/tv/errors.py`, `src/croom/tv/client.py`, `src/croom/tv/keystore.py`, `src/croom/tv/service.py`
- Test: `tests/unit/tv/__init__.py`, `tests/unit/tv/fake_client.py`, `tests/unit/tv/test_service.py`, `tests/unit/core/test_config.py` (one test)

**Interfaces:**
- Produces: `TvConfig(enabled=False, host="", mac="", pi_input="HDMI_1", keep_on=True)`; `TvError(RuntimeError)`; `TvClient` protocol (the subset of `aiowebostv.WebOsClient` used: `connect()`, `disconnect()`, `is_connected()`, `client_key`, `tv_info`, `tv_state`, `register_state_update_callback(cb)`, `power_off()`, `power_on()`, `volume_up()`, `volume_down()`, `set_volume(n)`, `set_mute(bool)`, `channel_up()`, `channel_down()`, `set_channel(id)`, `get_inputs()`, `set_input(id)`, `get_apps()`, `launch_app(id)`, `send_message(text)`, `button(name)`, `move(dx, dy, down=0)`, `click()`, `scroll(dx, dy)`, `get_volume()`, `get_muted()`, `get_input()`, `get_current_app()`, `get_current_channel()`, `get_power_state()`) and `real_client_factory(host, key)` (lazy `from aiowebostv import WebOsClient`); `KeyStore(path)` with `read() -> Optional[str]`, `write(key)` (mode 600), `clear()`; `TvService(config, keystore, client_factory, waker, loop_sleep=asyncio.sleep)` with `name == "tv"`, `status() -> dict` (`configured, paired, on, reachable, name, volume, muted, input, inputs, app, channel, keep_on, held_off`), `pair()`, `forget()`, `from_config(config)`.
- The `FakeTvClient` in tests records calls, has scripted `tv_info`/`tv_state`, fires state callbacks on `fake.update(...)`, and can refuse to connect (`fake.refuse = True`) or require pairing (`key is None` → after `connect()` sets `client_key = "key-1"` and records `("paired",)`).

- [ ] **Step 1: Failing tests**

`tests/unit/tv/fake_client.py`:

```python
"""A stand-in for aiowebostv.WebOsClient: records every call, answers from scripted state, fires callbacks on demand."""
from types import SimpleNamespace


class FakeTvClient:
    instances = []

    def __init__(self, host, key):
        self.host, self.client_key = host, key
        self.calls, self.callbacks = [], []
        self.refuse = False
        self.connected = False
        self.tv_info = SimpleNamespace(system_info={"modelName": "OLED65C6"}, hello_info={"deviceName": "Boardroom TV"})
        self.tv_state = SimpleNamespace(power_state={"state": "Active"}, volume=24, muted=False, current_input="HDMI_1",
                                        inputs={"HDMI_1": {"id": "HDMI_1", "label": "HDMI 1"}, "HDMI_2": {"id": "HDMI_2", "label": "HDMI 2"}},
                                        current_app_id="com.webos.app.hdmi1", channel_number="7", apps={})
        FakeTvClient.instances.append(self)

    async def connect(self):
        self.calls.append(("connect", self.client_key))
        if self.refuse:
            raise ConnectionError("refused")
        if self.client_key is None:
            self.client_key = "key-1"
            self.calls.append(("paired",))
        self.connected = True
        return True

    async def disconnect(self):
        self.connected = False
        self.calls.append(("disconnect",))

    def is_connected(self):
        return self.connected

    async def register_state_update_callback(self, cb):
        self.callbacks.append(cb)

    async def update(self, **changes):
        for k, v in changes.items():
            setattr(self.tv_state, k, v)
        for cb in self.callbacks:
            await cb(self.tv_state)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            self.calls.append((name,) + args + (tuple(sorted(kwargs.items())) if kwargs else ()))
            if name == "get_inputs":
                return list(self.tv_state.inputs.values())
            if name == "get_volume":
                return {"volume": self.tv_state.volume}
            if name == "get_power_state":
                return self.tv_state.power_state
            return {}
        return call
```

`tests/unit/tv/test_service.py`:

```python
import os
import pytest
from croom.core.config import TvConfig
from croom.tv.errors import TvError
from croom.tv.keystore import KeyStore
from croom.tv.service import TvService
from tests.unit.tv.fake_client import FakeTvClient


def service_for(tmp_path, **overrides):
    config = TvConfig(**dict({"enabled": True, "host": "10.0.0.41", "mac": "AA:BB:CC:DD:EE:FF"}, **overrides))
    woken = []

    async def waker(mac):
        woken.append(mac)
    FakeTvClient.instances.clear()
    service = TvService(config, KeyStore(tmp_path / "tv-client-key"), FakeTvClient, waker)
    service.woken = woken
    return service


async def test_pairing_stores_the_key_privately_and_forget_removes_it(tmp_path):
    service = service_for(tmp_path)
    assert service.status()["paired"] is False and service.status()["configured"] is True
    await service.pair()
    key = tmp_path / "tv-client-key"
    assert key.read_text() == "key-1" and oct(key.stat().st_mode & 0o777) == "0o600"
    assert service.status()["paired"] is True and service.status()["reachable"] is True
    await service.forget()
    assert not key.exists() and service.status()["paired"] is False


async def test_start_connects_with_the_stored_key_and_reads_the_state(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        assert ("connect", "key-9") in client.calls
        s = service.status()
        assert s == {"configured": True, "paired": True, "on": True, "reachable": True, "name": "Boardroom TV", "volume": 24, "muted": False,
                     "input": "HDMI_1", "inputs": [{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}],
                     "app": "com.webos.app.hdmi1", "channel": "7", "keep_on": True, "held_off": False}
        await client.update(volume=30, muted=True)
        assert service.status()["volume"] == 30 and service.status()["muted"] is True
    finally:
        await service.stop()


async def test_an_unconfigured_room_has_a_quiet_service(tmp_path):
    service = service_for(tmp_path, enabled=False, host="")
    await service.start()
    assert service.status()["configured"] is False and FakeTvClient.instances == []
    with pytest.raises(TvError) as failure:
        await service.control("volume_up")
    assert "Options" in str(failure.value) or "not set up" in str(failure.value)
    await service.stop()


async def test_a_tv_that_refuses_is_unreachable_in_words_and_retried(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.RECONNECT_FIRST_S = 0.05
    FakeTvClient.refuse_next = True
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        client.refuse = True
        await service._connect_once()
        assert service.status()["reachable"] is False
        with pytest.raises(TvError) as failure:
            await service.control("volume_up")
        assert "10.0.0.41" in str(failure.value)
        client.refuse = False
        await service._connect_once()
        assert service.status()["reachable"] is True
    finally:
        await service.stop()
```

Config test (in the config test module): `Config.from_dict({"tv": {"enabled": True, "host": "10.0.0.41", "mac": "aa:bb", "pi_input": "HDMI_2", "keep_on": False}})` round-trips through `to_dict()` and `Config().tv.enabled is False`.

- [ ] **Step 2: Run to see them fail** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/tv tests/unit/core` — Expected: FAIL on import.

- [ ] **Step 3: Implement**

`core/config.py`:

```python
@dataclass
class TvConfig:
    """The room's LG webOS TV (spec 2026-10-13 TV remote)."""
    enabled: bool = False
    host: str = ""          # the TV's address; Find TVs on the table fills it
    mac: str = ""           # for Wake-on-LAN (Mobile TV On must be enabled on the TV)
    pi_input: str = "HDMI_1"   # the input the room Pi is on
    keep_on: bool = True    # wake the TV at start, before a join, and after an unexpected off
```

`Config.tv: TvConfig = field(default_factory=TvConfig)`, `if "tv" in data: config.tv = TvConfig(**data["tv"])`, and the `"tv": {...}` block in `to_dict()`.

`tv/errors.py`: `class TvError(RuntimeError): """The TV could not do it: words for the table."""`

`tv/keystore.py`:

```python
class KeyStore:
    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> Optional[str]:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def write(self, key: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(key, encoding="utf-8")
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
```

`tv/client.py`: the `TvClient` Protocol and `real_client_factory(host, key)` (lazy import of `aiowebostv.WebOsClient`).

`tv/service.py` (the shape; fill it to make the tests pass):

```python
class TvService(Service):
    RECONNECT_FIRST_S = 2.0
    RECONNECT_MAX_S = 30.0

    def __init__(self, config: TvConfig, keystore: KeyStore, client_factory, waker, sleep=asyncio.sleep):
        super().__init__("tv")
        self._config, self._keys, self._factory, self._wake, self._sleep = config, keystore, client_factory, waker, sleep
        self._client = None
        self._state: Dict[str, Any] = {"on": False, "reachable": False, "name": "", "volume": None, "muted": False, "input": None,
                                       "inputs": [], "app": None, "channel": None}
        self._held_off = False
        self._task: Optional[asyncio.Task] = None

    @classmethod
    def from_config(cls, config) -> "TvService":
        from croom.tv.client import real_client_factory
        from croom.tv.wake import wake
        return cls(config.tv, KeyStore(config.resolve_data_dir() / "tv-client-key"), real_client_factory, wake)

    @property
    def configured(self) -> bool:
        return bool(self._config.enabled and self._config.host)

    def status(self) -> Dict[str, Any]:
        return dict(configured=self.configured, paired=self._keys.read() is not None, keep_on=self._config.keep_on, held_off=self._held_off, **self._state)
```

`start()` starts the reconnect loop when configured and paired; `_connect_once()` builds a client with the stored key, connects, registers the callback, reads `tv_info`/`tv_state` into `_state`, sets `reachable`; on failure sets `reachable=False` and `on=False`; `pair()` builds a client with key None, connects (the TV prompts), stores `client.client_key`, keeps the client; `forget()` disconnects and clears. `stop()` cancels the loop and disconnects. `_take_state(tv_state)` maps `power_state` (`state == "Active"` → on), `volume`, `muted`, `current_input`, `inputs` (dict → list of `{id, label}`), `current_app_id`, `channel_number`. `control()` is Task 2; for now it raises `TvError("The TV is not set up: use Find TVs and Pair under Options › TV")` when unconfigured and `TvError(f"The TV is not answering at {host}")` when unreachable.

- [ ] **Step 4: Run** `tests/unit/tv tests/unit/core` — Expected: PASS.

- [ ] **Step 5: Commit** — `feat(tv): a TV service for the room's LG webOS TV: config, a private client key, pairing and live state`.

---

### Task 2: Controls, Wake-on-LAN and keeping the TV on

**Files:**
- Create: `src/croom/tv/wake.py`
- Modify: `src/croom/tv/service.py`, `src/croom/meeting/service.py` (a `before_join` hook list), `src/croom/core/agent.py` (register the TV service and hook it to the meeting service)
- Test: `tests/unit/tv/test_service.py`, `tests/unit/tv/test_wake.py`

**Interfaces:**
- Produces: `TvService.control(name, value=None) -> dict` (the new `status()`), names: `power` (value `"on"`/`"off"`), `volume_up`, `volume_down`, `volume` (0–100), `mute` (toggle; value bool sets), `channel_up`, `channel_down`, `channel` (a number string), `input` (an input id or `"pi"`), `app` (an app id), `key` (a remote key name from `KEYS`), `digit` (`"0"`–`"9"`), `toast` (text); `TvService.ensure_on(timeout=15.0, manual=False) -> bool`; `TvService.before_join()`; `KEYS` (the set of names `aiowebostv` accepts, listed in the spec); `MeetingService.add_before_join(hook)` and the call `await hook()` at the start of `join_meeting` (each hook wrapped so a failure logs and the join goes on); `wake(mac)` (lazy `wakeonlan.send_magic_packet`).

- [ ] **Step 1: Failing tests**

```python
async def test_controls_map_to_the_tv_and_re_read_the_state(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await service.control("volume_up"); await service.control("volume", 55); await service.control("mute")
        await service.control("channel", "12"); await service.control("input", "pi"); await service.control("key", "HOME")
        await service.control("digit", "7"); await service.control("toast", "Hello")
        names = [c[0] for c in client.calls if c[0] not in ("connect", "get_inputs", "get_volume", "get_power_state")]
        assert names == ["volume_up", "set_volume", "set_mute", "set_channel", "set_input", "button", "button", "send_message"]
        assert ("set_input", "HDMI_1") in client.calls and ("button", "7") in client.calls and ("set_mute", True) in client.calls
        with pytest.raises(TvError):
            await service.control("key", "SELF_DESTRUCT")
        with pytest.raises(TvError):
            await service.control("volume", 150)
    finally:
        await service.stop()


async def test_power_off_holds_against_keep_on_and_on_wakes(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await service.control("power", "off")
        assert ("power_off",) in client.calls and service.status()["held_off"] is True
        await client.update(power_state={"state": "Off"})
        await service.ensure_on(timeout=0.2)                        # keep_on would wake; the hold says no
        assert service.woken == []
        await service.control("power", "on")
        assert service.woken == ["AA:BB:CC:DD:EE:FF"] and service.status()["held_off"] is False
    finally:
        await service.stop()


async def test_a_manual_off_holds_until_on_or_a_join(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        await service.control("power", "off")
        await FakeTvClient.instances[-1].update(power_state={"state": "Off"})
        await service.before_join()                                 # a meeting wants the TV
        assert service.woken == ["AA:BB:CC:DD:EE:FF"] and service.status()["held_off"] is False
    finally:
        await service.stop()


async def test_a_join_waits_at_most_fifteen_seconds_for_a_tv_that_never_wakes(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.WAKE_TIMEOUT_S = 0.3
    service.WAKE_POLL_S = 0.05
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await client.update(power_state={"state": "Off"})
        client.refuse = True
        import time
        t0 = time.monotonic()
        assert await service.before_join() is False                 # the join goes on without the TV
        assert time.monotonic() - t0 < 1.0 and service.woken == ["AA:BB:CC:DD:EE:FF"]
        assert service.status()["on"] is False
    finally:
        await service.stop()


async def test_an_unexpected_off_is_woken_when_keep_on(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.REWAKE_AFTER_S = 0.05
    await service.start()
    try:
        await FakeTvClient.instances[-1].update(power_state={"state": "Off"})
        for _ in range(40):
            if service.woken:
                break
            await asyncio.sleep(0.02)
        assert service.woken == ["AA:BB:CC:DD:EE:FF"]
    finally:
        await service.stop()
```

`test_wake.py`: `wake("AA:BB:CC:DD:EE:FF")` calls `wakeonlan.send_magic_packet` once with that MAC (monkeypatch the module attribute); a malformed MAC raises `TvError`.

`tests/unit/meeting/test_service.py` (one test): a `before_join` hook runs before the provider's join, and a hook that raises does not stop the join.

- [ ] **Step 2: Run to see them fail.**

- [ ] **Step 3: Implement** `control()` as a dispatch table over the client (volume 0–100 validated; `key` validated against `KEYS`; `input` `"pi"` → `pi_input`; every control followed by a state read); `ensure_on(timeout, manual)`: when `on` → True; when `held_off and not manual` → False; else `wake(mac)` then poll `_connect_once()` every `WAKE_POLL_S` (1 s) until `WAKE_TIMEOUT_S` (15 s) → returns whether `on`; `control("power","on")` → `held_off=False; ensure_on(manual=True)`; `control("power","off")` → `power_off()`, `held_off=True`, state `on=False`; `before_join()` → `held_off=False; return await ensure_on()` when `keep_on`, else True; the state callback: when `on` flips to False and `keep_on and not held_off` → schedule `ensure_on()` after `REWAKE_AFTER_S` (5 s). `MeetingService.add_before_join(hook)` + the loop at the top of `join_meeting`. `agent.py`: register `TvService.from_config(self.config)` as `"tv"` when `config.tv.enabled`, before the meeting service's dependencies; `meeting.add_before_join(tv.before_join)`; pass `tv=` to `ControlService.from_config` (Task 4 adds the parameter; here add the wiring guarded by `hasattr`).

- [ ] **Step 4: Run** `tests/unit/tv tests/unit/meeting/test_service.py` — Expected: PASS.

- [ ] **Step 5: Commit** — `feat(tv): the TV's controls from the table, Wake-on-LAN, and a TV that stays on unless the table turned it off`.

---

### Task 3: Discovery and `croom --check-tv`

**Files:**
- Create: `src/croom/tv/discover.py`, `src/croom/tv/check.py`
- Modify: `src/croom/core/agent.py` (the `--check-tv` flag beside `--check-zoom`)
- Test: `tests/unit/tv/test_discover.py`, `tests/unit/tv/test_check.py`

**Interfaces:**
- Produces: `discover_tvs(timeout=3.0, send=None, fetch=None) -> List[dict]` (`[{"host", "name", "location"}]`): sends an SSDP M-SEARCH (`ST: urn:lge-com:service:webos-second-screen:1`, `MX: 2`) to 239.255.255.250:1900 through an `asyncio` datagram endpoint, collects `LOCATION` headers from answers, fetches each description XML with aiohttp and reads `friendlyName`; `send`/`fetch` are injectable (the tests pass a fake transport that answers with canned SSDP responses and a fake fetch returning XML). `check_tv(config, out) -> int` prints: configured host and MAC, paired or not, reachable and the TV's name/state, the inputs, and "Find TVs" results when unconfigured.

- [ ] **Step 1: Failing tests** — two canned SSDP answers (one LG at 10.0.0.41 with a description XML naming "Boardroom TV", one non-LG ignored by its `ST`); `discover_tvs` returns the LG one; `check_tv` prints "Boardroom TV" and "paired" with a fake service, and the Find TVs list when unconfigured.
- [ ] **Step 2: see them fail. Step 3: implement. Step 4: run `tests/unit/tv`. Step 5: commit** — `feat(tv): Find TVs on the network and croom --check-tv`.

---

### Task 4: The control service: the status block and the TV routes

**Files:**
- Modify: `src/croom/control/service.py` (`tv=` parameter and `from_config`, `_status()["tv"]`, routes), `src/croom/core/agent.py` (pass `tv=`)
- Test: `tests/unit/control/test_service.py` (a `StubTv` with `status()`, `control()`, `pair()`, `forget()`, `discover()` recording calls; `fail_control`)

**Interfaces:**
- Produces: `/api/status` → `tv` (the service's `status()`, or `{"configured": False, "paired": False}` without a service); `POST /api/tv/control {"name", "value"?}` → 200 `{"tv": status}`, 400 bad body, 409 `TvError` or any failure in words, 503 without a TV service; `POST /api/tv/pair` → 200 status / 409 words; `POST /api/tv/forget` → 200; `GET /api/tv/discover` → `{"tvs": [...]}`.

- [ ] **Step 1: Failing tests** (shape as the meeting routes' tests: status carries `tv`; control maps and 409s in words; pair/forget/discover). **Step 2: fail. Step 3: implement** (mirror `_run_control` from the meeting routes). **Step 4: run `tests/unit/control/test_service.py`. Step 5: commit** — `feat(control): the TV in the status and four TV routes`.

---

### Task 5: The pointer socket and the room Pi's virtual mouse

**Files:**
- Create: `src/croom/tv/pointer.py` (`PiMouse`, `PointerRelay`)
- Modify: `src/croom/control/service.py` (`WS /ws/pointer`), `installer/install.sh` (udev rule, `input` group, `uinput` module, `python3-dev`), `pyproject.toml` (`aiowebostv`, `wakeonlan`, `evdev` in dependencies)
- Test: `tests/unit/tv/test_pointer.py`, `tests/unit/control/test_service.py` (the WebSocket through `aiohttp`'s test client), `tests/unit/installer` (the rule text)

**Interfaces:**
- Produces: `PiMouse(uinput_factory=None)` with `move(dx, dy)`, `press(button)`, `release(button)`, `click(button="left")`, `scroll(dy)`, `close()`; the factory makes an object with `write(type, code, value)` and `syn()` (the real one: `evdev.UInput({EV_REL: [REL_X, REL_Y, REL_WHEEL], EV_KEY: [BTN_LEFT, BTN_RIGHT]}, name="Crystal Meet touchpad")`, lazily imported; `PiMouse.unavailable` reason when `/dev/uinput` cannot be opened). `PointerRelay(tv_service, pi_mouse)` with `async handle(message: dict)`: `{"target": "tv"|"pi", "type": "move"|"tap"|"right"|"scroll"|"down"|"up", "dx", "dy"}`; TV moves are coalesced: at most one `move` in flight, later deltas summed into the next; Pi moves apply at once; `"keys"` is refused with `TvError("Typing into the room Pi comes in a later version")`; `release_all()` on socket close releases a held button. The WebSocket handler reads JSON text frames, answers errors as `{"error": words}` frames, and never closes the socket for a bad frame.

- [ ] **Step 1: Failing tests**

```python
async def test_pi_moves_clicks_and_scrolls_reach_the_virtual_mouse():
    writes = []
    class Fake:
        def write(self, t, c, v): writes.append((t, c, v))
        def syn(self): writes.append("syn")
        def close(self): pass
    mouse = PiMouse(uinput_factory=lambda: Fake())
    relay = PointerRelay(tv_service=None, pi_mouse=mouse)
    await relay.handle({"target": "pi", "type": "move", "dx": 3, "dy": -2})
    await relay.handle({"target": "pi", "type": "tap"})
    await relay.handle({"target": "pi", "type": "scroll", "dy": -1})
    from evdev import ecodes as e   # the test may import evdev's constants only if installed; otherwise use the module's own constants table
    assert writes[:3] == [(e.EV_REL, e.REL_X, 3), (e.EV_REL, e.REL_Y, -2), "syn"]
    assert (e.EV_KEY, e.BTN_LEFT, 1) in writes and (e.EV_KEY, e.BTN_LEFT, 0) in writes and (e.EV_REL, e.REL_WHEEL, -1) in writes


async def test_a_burst_of_moves_is_coalesced_not_queued(tmp_path):
    service = ...  # a started TvService on a FakeTvClient whose move() awaits an asyncio.Event the test holds
    relay = PointerRelay(service, PiMouse(uinput_factory=lambda: Fake()))
    for i in range(50):
        await relay.handle({"target": "tv", "type": "move", "dx": 1, "dy": 0})
    release the event; await relay.drain()
    assert the client saw 2 move calls: the first (1, 0) and one summed (49, 0)


async def test_a_dropped_socket_releases_a_held_button():
    ... relay.handle({"target": "pi", "type": "down"}); await relay.release_all(); assert (EV_KEY, BTN_LEFT, 0) in writes


async def test_keys_are_refused_in_words():
    with pytest.raises(TvError): await relay.handle({"target": "pi", "type": "keys", "text": "a"})
```

(Use the module's own constants table `pointer.CODES` instead of importing evdev in tests, so the tests run without evdev.)

Control service test: open `/ws/pointer` with the aiohttp test client, send a move frame, assert the StubTv/StubMouse saw it; send garbage, get an `{"error": ...}` frame and the socket stays open.

Installer test: `install.sh` contains the udev rule line `KERNEL=="uinput", GROUP="input", MODE="0660"`, `usermod -aG input`, `uinput` in `/etc/modules-load.d/`, and `python3-dev` in the apt list.

- [ ] **Step 2: fail. Step 3: implement. Step 4: run `tests/unit/tv tests/unit/control/test_service.py tests/unit/installer`. Step 5: commit** — `feat(tv): the pointer socket drives the TV's cursor and a virtual mouse on the room Pi`.

---

### Merge point

Before Task 6: `git merge meeting-controls` into `tv-remote` (resolve conflicts, run `tests/unit/control` and `tests/unit/meeting` in full, commit the merge). The controller does this between tasks; the implementer of Task 6 starts from the merged tree.

---

### Task 6: The `tv` view: the remote

**Files:**
- Modify: `src/croom/control/static/index.html` (the `tv` view, Options › TV, the meeting secondary row's TV button), `app.js` (`VIEW_MODES.tv` by opener like the camera view, the remote's rendering from `status.tv`, `tvControl(name, value)`), `style.css` (the remote's grid, the d-pad disc), `icons.svg` (tv, power, home, menu, arrow keys, volume, hash)
- Modify: `tests/unit/control/test_page.py` (`PageServer(tv=StubTv())`, `tests/unit/control/test_service.py`'s `StubTv` reused)
- Test: `tests/unit/control/test_page.py`

**Interfaces:**
- Produces: view `tv` (`VIEW_MODES` by opener: `tvMode` like `cameraMode`), `#open-tv-options` (Options), `#open-tv-meeting` (the secondary row), `#back-from-tv`; `#tv-status` line; the remote's controls with `data-tv="<name>:<value>"` attributes (`power:toggle`, `input:menu`, `key:HOME`, `key:MENU`, `volume_up`, `volume_down`, `mute`, `channel_up`, `channel_down`, `key:UP|DOWN|LEFT|RIGHT|ENTER|BACK|EXIT|INFO|GUIDE|PLAY|PAUSE|STOP|REWIND|FASTFORWARD|RED|GREEN|YELLOW|BLUE`, `digit:0`–`9`, `key:DASH`); `#tv-inputs` row (hidden until Input is tapped) with one button per input and the Pi's one labelled "Crystal Meet"; the pairing card `#tv-pairing` with Find TVs (`GET /api/tv/discover` → a list to tap, which posts `/api/tv/control {"name": "configure", "value": host}`? — no: configuration is a file; the pairing card lists found TVs with their addresses for the config and a **Pair** button that posts `/api/tv/pair`); Power's second tap for Off.

- [ ] **Step 1: Failing tests** — `test_the_remote_sends_the_right_controls` (every button posts the control named by its data attribute; the StubTv records), `test_an_unpaired_room_shows_the_pairing_card_not_a_dead_remote`, `test_the_tv_view_opens_from_options_and_from_a_meeting_and_returns_to_each`, `test_power_off_needs_a_second_tap`, `test_the_input_row_marks_the_pi` and `fits()` on the view at 1280 by 800.
- [ ] **Step 2: fail. Step 3: implement per spec section 5.2 and 5.4. Step 4: run `tests/unit/control/test_page.py`. Step 5: commit** — `feat(control): the TV remote on the table, from Options and from a meeting`.

---

### Task 7: The pointer modes and the full-screen touchpad

**Files:**
- Modify: `app.js` (the pointer client over `/ws/pointer`, the disc as trackpad, the full-screen surface), `index.html` (`#touchpad` surface with its strip, target switch and Done), `style.css`
- Test: `tests/unit/control/test_page.py` (a fake WebSocket server in `PageServer` recording frames, or the real route with a StubTv/StubMouse)

**Interfaces:**
- Produces: `#tv-pointer` (Pointer toggle), `#tv-pi-mouse`, `#tv-fullscreen`; the disc gains `data-mode="keys"|"pointer"`; `#touchpad` (`hidden` unless up) with `#touchpad-target` (TV / Room Pi switch), `#touchpad-done`, `#touchpad-link` (connection mark); the client coalesces to one `move` per animation frame with integer deltas and carried remainders, gain 1.6 with a soft acceleration curve (`dx * (1 + min(|v|, 40) / 40)`), a tap under 180 ms and 8 px → `tap`, two fingers → `scroll` (and a two-finger tap → `right` for the Pi), a press over 350 ms then move → `down` … `up` (drag). The surface pauses `armIdleReturn` and returns to the opener on Done or 10 idle minutes.

- [ ] **Step 1: Failing tests** — `test_a_finger_path_on_the_disc_sends_moves_and_a_tap_clicks` (synthetic pointer events; the frames recorded by the server stub: moves with the summed deltas, then a tap), `test_two_fingers_scroll_and_two_finger_tap_right_clicks_the_pi`, `test_the_touchpad_returns_to_the_screen_that_opened_it` (from the meeting, Done → meeting; from Options → tv view → Back → Options), `test_the_touchpad_shows_a_mark_when_the_socket_drops`.
- [ ] **Step 2: fail. Step 3: implement. Step 4: run the page file. Step 5: commit** — `feat(control): the d-pad disc as a trackpad, a virtual mouse on the room Pi, and the full-screen touchpad`.

---

### Task 8: Docs

**Files:** `README.md` (the features table row, the config table row for `tv`, the room page row), `docs/guides/crystal-meet-room-setup/index.html` (+ PDF: a "The TV" section: the TV's settings to enable (Mobile TV On, LG Connect Apps), the `tv:` config, Find TVs and Pair from the table, the remote, the touchpad modes, `croom --check-tv`, troubleshooting cards for an unreachable TV and a TV that will not wake), `installer/install.sh` help text.
**Test:** `tests/unit/docs` (needles: the spec and plan paths, `croom --check-tv`, `Mobile TV On`, `/ws/pointer`).

- [ ] Steps as in the other docs tasks; commit — `docs: the TV remote and the touchpad in the room guide and README`.

---

## Self-review

- Spec coverage: section 3 → Tasks 1–4; section 4 → Task 5; section 5.1–5.3 → Tasks 6–7; 5.4 design direction → Tasks 6–7's CSS; section 6 → Tasks 1, 4, 5; section 7 tests → named per task; the `keys` refusal → Task 5. HDMI-CEC is explicitly not built (spec section 2).
- Placeholders: Tasks 3–8 give the test names and the shapes rather than every line of code: the implementer writes them in the style of Tasks 1–2 and the meeting routes; the controller reviews each against the spec. No "TBD".
- Type consistency: `TvService.status()/control()/pair()/forget()/before_join()/ensure_on()`; `PointerRelay.handle()/release_all()/drain()`; `PiMouse.move/press/release/click/scroll`; the wire names in section 6 of the spec.
- Review Focus: each line names its test.
