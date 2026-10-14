"""What the TV tests share: a service on a fake TV, a wait for a background condition, and a sleep that never waits."""
import asyncio

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
                  **overrides):
    """A TV service on a fake TV. `key` is the stored client key; None means the TV is not paired yet."""
    key_path = key_path or tmp_path / "tv-client-key"
    if key is not None:
        key_path.write_text(key)
    config = TvConfig(**dict({"enabled": True, "host": "10.0.0.41", "mac": "AA:BB:CC:DD:EE:FF"}, **overrides))
    return TvService(config, keystore(key_path), factory, a_waker, loop_sleep=loop_sleep)


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
