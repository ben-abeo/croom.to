"""A stand-in for aiowebostv.WebOsClient: records every call, answers from scripted state, fires callbacks on demand."""
from types import SimpleNamespace


class FakeTvClient:
    instances = []
    refuse_next = False   # class-level: the next connect() of any instance is refused, once, as by a TV that is off

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
        if self.refuse or FakeTvClient.refuse_next:
            FakeTvClient.refuse_next = False
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
