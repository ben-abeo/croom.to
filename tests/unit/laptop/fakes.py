"""Stand-ins for Laptop mode's tests: the switch, the devices and the TV, each recording what it was asked."""
from croom.laptop.switch import LAPTOP, ROOM


class FakeUsbSwitch:
    """The switch with the MeetUp on this Pi to begin with. `answers = False` makes a move that does not happen."""

    def __init__(self, available=True):
        self._available, self.on_pi, self.answers, self.moves, self.opened, self.closed = available, True, True, [], False, False

    def open(self):
        self.opened = True
        return self._available

    @property
    def available(self):
        return self._available

    @property
    def problem(self):
        return None if self._available else "the switch's GPIO pin cannot be used: busy"

    def holder(self):
        return ROOM if self.on_pi else LAPTOP

    async def move_to(self, holder):
        self.moves.append(holder)
        if self.answers:
            self.on_pi = holder == ROOM
        return self.holder() == holder

    def close(self):
        self.closed = True


class FakeDevices:
    def __init__(self):
        self.calls, self.homes = [], []

    async def release(self):
        self.calls.append("release")

    async def reclaim(self, home=True):
        self.calls.append("reclaim")
        self.homes.append(home)


class FakeTv:
    def __init__(self, words=None):
        self.calls, self.words = [], words

    async def show_input(self, which):
        self.calls.append(which)
        return self.words
