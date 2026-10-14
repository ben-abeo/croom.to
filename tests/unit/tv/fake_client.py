"""A stand-in for aiowebostv.WebOsClient: records every call, answers from scripted state, fires callbacks on demand."""
import asyncio
from types import SimpleNamespace


class WebOsTvPairError(Exception):
    """Named as the library's exception for a pairing the TV did not accept: the service tells a declined prompt by
    the class name, so it never has to import the library."""


class FakeTvClient:
    instances = []
    refuse_next = False   # class-level: the next connect() of any instance is refused, once, as by a TV that is off

    def __init__(self, host, key):
        self.host, self.client_key = host, key
        self.calls, self.callbacks = [], []
        self.refuse = False           # connect() raises ConnectionError: the TV is off, or not on the network
        self.decline = False          # the TV shows its pairing prompt and the person declines it: connect() raises WebOsTvPairError
        self.prompt_timeout = False   # the prompt is not answered in time (the library waits ten seconds): connect() raises TimeoutError
        self.connected = False
        self.tv_info = SimpleNamespace(system_info={"modelName": "OLED65C6"}, hello_info={"deviceName": "Boardroom TV"})
        self.tv_state = SimpleNamespace(power_state={"state": "Active"}, volume=24, muted=False, current_input="HDMI_1",
                                        inputs={"HDMI_1": {"id": "HDMI_1", "label": "HDMI 1"}, "HDMI_2": {"id": "HDMI_2", "label": "HDMI 2"}},
                                        current_app_id="com.webos.app.hdmi1", channel_number="7", apps={})
        FakeTvClient.instances.append(self)

    async def connect(self):
        self.calls.append(("connect", self.client_key))
        if self.refuse or FakeTvClient.refuse_next:
            FakeTvClient.refuse_next = False
            raise ConnectionError("refused")
        if self.decline:
            raise WebOsTvPairError("403 User denied access")
        if self.prompt_timeout:
            raise TimeoutError()
        if self.client_key is None:
            self.client_key = "key-1"
            self.calls.append(("paired",))
        self.connected = True
        return True

    async def disconnect(self):
        self.calls.append(("disconnect",))
        await self.drop()

    async def drop(self):
        """The connection ends, whoever ended it: the library hands every callback a cleared state as it lets go."""
        self.connected = False
        self.tv_state.power_state = {}
        for cb in self.callbacks:
            await cb(self.tv_state)

    def is_connected(self):
        return self.connected

    async def register_state_update_callback(self, cb):
        self.callbacks.append(cb)

    async def update(self, **changes):
        for k, v in changes.items():
            setattr(self.tv_state, k, v)
        for cb in self.callbacks:
            await cb(self.tv_state)

    def _record(self, name, *args, **kwargs):
        self.calls.append((name,) + args + (tuple(sorted(kwargs.items())) if kwargs else ()))

    # The calls the service makes of the TV: the library's names, and answers of the library's kinds.

    async def power_off(self, *args, **kwargs):
        self._record("power_off", *args, **kwargs)

    async def power_on(self, *args, **kwargs):
        self._record("power_on", *args, **kwargs)
        return {}

    async def volume_up(self, *args, **kwargs):
        self._record("volume_up", *args, **kwargs)
        return {}

    async def volume_down(self, *args, **kwargs):
        self._record("volume_down", *args, **kwargs)
        return {}

    async def set_volume(self, *args, **kwargs):
        self._record("set_volume", *args, **kwargs)
        return {}

    async def set_mute(self, *args, **kwargs):
        self._record("set_mute", *args, **kwargs)
        return {}

    async def channel_up(self, *args, **kwargs):
        self._record("channel_up", *args, **kwargs)
        return {}

    async def channel_down(self, *args, **kwargs):
        self._record("channel_down", *args, **kwargs)
        return {}

    async def set_channel(self, *args, **kwargs):
        self._record("set_channel", *args, **kwargs)
        return {}

    async def set_input(self, *args, **kwargs):
        self._record("set_input", *args, **kwargs)
        return {}

    async def launch_app(self, *args, **kwargs):
        self._record("launch_app", *args, **kwargs)
        return {}

    async def send_message(self, *args, **kwargs):
        self._record("send_message", *args, **kwargs)
        return {}

    async def button(self, *args, **kwargs):
        self._record("button", *args, **kwargs)

    async def move(self, *args, **kwargs):
        self._record("move", *args, **kwargs)

    async def click(self, *args, **kwargs):
        self._record("click", *args, **kwargs)

    async def scroll(self, *args, **kwargs):
        self._record("scroll", *args, **kwargs)

    async def get_inputs(self):
        self._record("get_inputs")
        return list(self.tv_state.inputs.values())

    async def get_apps(self):
        self._record("get_apps")
        return list(self.tv_state.apps.values())

    async def get_volume(self):
        self._record("get_volume")
        return self.tv_state.volume

    async def get_muted(self):
        self._record("get_muted")
        return bool(self.tv_state.muted)

    async def get_input(self):
        self._record("get_input")
        return self.tv_state.current_app_id           # the library's get_input() is the running app's id

    async def get_current_app(self):
        self._record("get_current_app")
        return self.tv_state.current_app_id

    async def get_current_channel(self):
        self._record("get_current_channel")
        return {"channelNumber": self.tv_state.channel_number}

    async def get_power_state(self):
        self._record("get_power_state")
        return self.tv_state.power_state


class PromptingTvClient(FakeTvClient):
    """A TV that shows its pairing prompt and waits for the person: connect() returns when accept() is called, or
    False when disconnect() cuts the wait short, as the library's does."""

    def __init__(self, host, key):
        super().__init__(host, key)
        self.answered = asyncio.Event()
        self.hung_up = False

    @property
    def prompt_is_up(self):
        return any(call[0] == "connect" for call in self.calls)

    def accept(self):
        self.answered.set()

    async def connect(self):
        self.calls.append(("connect", self.client_key))
        await self.answered.wait()
        if self.hung_up:
            return False
        if self.client_key is None:
            self.client_key = "key-1"
            self.calls.append(("paired",))
        self.connected = True
        return True

    async def disconnect(self):
        self.hung_up = True
        self.answered.set()
        await super().disconnect()


class DeafPromptingTvClient(PromptingTvClient):
    """The same, but a disconnect() does not end the wait: the connection is made after all once the person accepts."""

    async def disconnect(self):
        await FakeTvClient.disconnect(self)
