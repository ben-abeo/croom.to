"""What the TV tests share: a service on a fake TV, a wait for a background condition, and a sleep that never waits."""
import asyncio
import contextlib

from croom.core.config import TvConfig
from croom.tv.keystore import KeyStore
from croom.tv.service import TvService
from tests.unit.tv.fake_client import FakeTvClient


async def until(condition, timeout=2.0):
    """Let the loop run until the condition holds: the connection loop works in the background."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "the condition never held"
        await asyncio.sleep(0.01)


async def a_waker(mac):
    pass


def build_service(tmp_path, factory=FakeTvClient, key="key-9", loop_sleep=asyncio.sleep, keystore=KeyStore, key_path=None,
                  waker=a_waker, **overrides):
    """A TV service on a fake TV. `key` is the stored client key; None means the TV is not paired yet."""
    key_path = key_path or tmp_path / "tv-client-key"
    if key is not None:
        key_path.write_text(key)
    config = TvConfig(**dict({"enabled": True, "host": "10.0.0.41", "mac": "AA:BB:CC:DD:EE:FF"}, **overrides))
    return TvService(config, keystore(key_path), factory, waker, loop_sleep=loop_sleep)


def service_for(tmp_path, factory=FakeTvClient, key=None, **overrides):
    """A TV service whose waker records the MAC addresses it was asked to wake (`service.woken`), and, like a real
    wake, brings the TV on: the newest fake client says it is Active. `service.wake_works = False` makes a wake that
    does nothing. The stored key is `key`; with none, the key file is the test's to write."""
    woken = []

    async def waker(mac):
        woken.append(mac)
        if service.wake_works and FakeTvClient.instances:
            await FakeTvClient.instances[-1].update(power_state={"state": "Active"})
    FakeTvClient.instances.clear()
    service = build_service(tmp_path, factory=factory, key=key, waker=waker, **overrides)
    service.woken, service.wake_works = woken, True
    return service


def silent_tv(host, key):
    """A TV that is off: nothing answers at its address."""
    client = FakeTvClient(host, key)
    client.refuse = True
    return client


def in_standby(host, key):
    """A TV that is off with its socket up (Quick Start+): it answers, and says it is not on."""
    client = FakeTvClient(host, key)
    client.tv_state.power_state = {"state": "Active Standby"}
    return client


def forgetful_tv(host, key):
    """A TV that has forgotten this table: a stored key makes it show its prompt, and the prompt is declined."""
    client = FakeTvClient(host, key)
    client.decline = key is not None
    return client


class WebOsTvCommandError(Exception):
    """Named as the library's exception for a command the TV did not carry out."""


class DoingTv(FakeTvClient):
    """A TV that does what it is told, as a real one does: its state follows the commands, and what it is asked says what it
    has now. It pushes nothing of the change: whatever shows it to the service is the service's read-back."""

    APPS = {"HDMI_1": "com.webos.app.hdmi1", "HDMI_2": "com.webos.app.hdmi2"}

    async def volume_up(self):
        await super().volume_up()
        self.tv_state.volume += 1

    async def volume_down(self):
        await super().volume_down()
        self.tv_state.volume -= 1

    async def set_volume(self, volume):
        await super().set_volume(volume)
        self.tv_state.volume = volume

    async def set_mute(self, mute):
        await super().set_mute(mute)
        self.tv_state.muted = mute

    async def set_input(self, input_id):
        await super().set_input(input_id)
        self.tv_state.current_input, self.tv_state.current_app_id = input_id, self.APPS.get(input_id, input_id)

    async def launch_app(self, app):
        await super().launch_app(app)
        self.tv_state.current_input, self.tv_state.current_app_id = None, app      # an app is not an input

    async def set_channel(self, channel):
        await super().set_channel(channel)
        self.tv_state.channel_number = channel

    async def channel_up(self):
        await super().channel_up()
        self.tv_state.channel_number = str(int(self.tv_state.channel_number) + 1)

    async def channel_down(self):
        await super().channel_down()
        self.tv_state.channel_number = str(int(self.tv_state.channel_number) - 1)


class StandbyTv(DoingTv):
    """A TV in standby with its socket up (Quick Start+): it answers, says Active Standby, and comes on when it is told to
    turn on, `delay` seconds later (None: it never says that it has)."""
    delay = 0.0
    refuses = False                                       # the TV does not accept the command

    def __init__(self, host, key):
        super().__init__(host, key)
        self.tv_state.power_state = {"state": "Active Standby"}

    async def power_on(self):
        await super().power_on()                          # (records the call)
        if self.refuses:
            raise WebOsTvCommandError("Request failed with response {'payload': {'returnValue': False, 'errorText': 'not now'}}")
        if self.delay is None:
            return
        if self.delay:
            asyncio.get_running_loop().call_later(
                self.delay, lambda: asyncio.ensure_future(self.update(power_state={"state": "Active"})))
        else:
            await self.update(power_state={"state": "Active"})


def quick_start_tv(delay=0.0, refuses=False):
    """A factory for a TV in standby that turns on when told to (see StandbyTv)."""
    def build(host, key):
        client = StandbyTv(host, key)
        client.delay, client.refuses = delay, refuses
        return client
    return build


class LibraryLike(FakeTvClient):
    """What aiowebostv's client does when the task awaiting connect() is cancelled: its connect task goes on, its
    connect_result future is cancelled, and until that ends (or the client is hung up on) any connect() raises
    CancelledError at once. Its connect() waits for `gate` (the conftest's fixture: an event the test owns) and then
    fails, as a TV that does not answer does."""
    gate = None
    poisoned = False

    async def connect(self):
        if self.poisoned:
            raise asyncio.CancelledError()
        self.calls.append(("connect", self.client_key))
        try:
            await LibraryLike.gate.wait()                # a TV that does not answer: the attempt takes its time
        except asyncio.CancelledError:
            self.poisoned = True
            raise
        raise ConnectionError("no answer in time")

    async def disconnect(self):
        self.poisoned = False
        await super().disconnect()


@contextlib.asynccontextmanager
async def running(service):
    """The service started, and stopped when the test is done with it; the newest fake client, if the TV was reached."""
    await service.start()
    try:
        yield FakeTvClient.instances[-1] if FakeTvClient.instances else None
    finally:
        await service.stop()


def yielding_sleep(delays=None):
    """The connection loop's sleep, for tests: it notes the delay it was asked for and lets the other tasks run
    instead of waiting, so the loop goes round as fast as the test lets it and no test depends on a timer."""
    async def sleep(seconds):
        if delays is not None:
            delays.append(seconds)
        await asyncio.sleep(0)
    return sleep


def connects(client):
    """How many times this client was asked to connect."""
    return len([call for call in client.calls if call[0] == "connect"])


def prompt_is_up():
    """The newest fake client is waiting for the person at the TV."""
    return bool(FakeTvClient.instances) and getattr(FakeTvClient.instances[-1], "prompt_is_up", False)


async def within(awaitable, seconds=2.0):
    """Wait for something that must happen, so that a regression fails the test instead of hanging it."""
    return await asyncio.wait_for(awaitable, seconds)
