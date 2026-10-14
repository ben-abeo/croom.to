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
