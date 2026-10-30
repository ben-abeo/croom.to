"""
The room microphone's level through PipeWire (spec 2026-10-29 room settings, Mic level): the source the
room picks up the call with (audio.input_device, or PipeWire's default source), 0 to 150 where 100 is the
device's normal level, read and written with wpctl as the speaker's sink is (croom.devices.volume).

The level a person chooses on the page is kept in the control settings ("mic_level") and put back by
reapply(): the meeting apps' automatic gain control in the browser moves the source's level by itself, so
the devices service calls it when a meeting joins and every few seconds while it is connected.
"""

import logging
import time
from typing import Any, Dict, Optional

from croom.devices.errors import DeviceUnavailable
from croom.devices.volume import PipeWireLevel, Runner, run_command

logger = logging.getLogger(__name__)

STORE_KEY = "mic_level"


class RoomMic(PipeWireLevel):
    MEDIA_CLASS = "Audio/Source"
    DEFAULT_KEY = "default.audio.source"
    NODE = "source"
    LABEL = "Microphone"
    DIRECTION = "input"
    PREFERENCE = "audio.input_device"
    WRONG_DEFAULT = "the call may hear the room through another microphone"
    MAX_LEVEL = 150

    def __init__(self, preference: str = "auto", runner: Runner = run_command, clock=time.monotonic, store=None):
        super().__init__(preference, runner=runner, clock=clock)
        self._store = store
        self._chosen: Optional[int] = self._load()

    @classmethod
    def from_config(cls, config, store=None) -> "RoomMic":
        return cls(preference=config.audio.input_device or "auto", store=store)

    def _load(self) -> Optional[int]:
        """The level kept in the settings, or None when none was chosen (or what is there is not a level)."""
        value = self._store.get(STORE_KEY) if self._store is not None else None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= self.MAX_LEVEL:
            return None
        return value

    @property
    def chosen(self) -> Optional[int]:
        """The level the room keeps the microphone at during a meeting, or None when nobody has chosen one."""
        return self._chosen

    def state(self) -> Dict[str, Any]:
        """The level is the chosen one when there is one: that is what the room keeps the source at, whatever the call's gain
        control has just moved it to (device_level, the level as last read)."""
        level = self._chosen if self._chosen is not None else self._level
        return {"available": self.available, "device": self._device, "level": level, "device_level": self._level,
                "reason": self._reason}

    # ------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------

    async def set_level(self, level: int) -> Dict[str, Any]:
        async with self._lock:
            return await self._choose_level(level)

    async def step(self, delta: int) -> Dict[str, Any]:
        """Louder or quieter from the chosen level, or from the level the source is at now when none is chosen. Above 150 %
        (set by something else), Louder writes nothing and Quieter goes down from 150."""
        async with self._lock:
            await self._refresh(force=True)
            delta = int(delta)
            if self._chosen is None and delta > 0 and self.available and self._raw_level >= self.MAX_LEVEL / 100:
                return self.state()
            base = self._chosen if self._chosen is not None else self._level
            return await self._choose_level(base + delta)

    async def _choose_level(self, level: int) -> Dict[str, Any]:
        level = await self._write_level(level)
        self._chosen = level
        self._keep(level)
        return self.state()

    def _keep(self, level: int) -> None:
        """Write the chosen level to the settings. A write that fails is warned about and the level is held in memory: the room
        goes on putting it back until the agent restarts."""
        if self._store is None:
            return
        try:
            self._store.save(STORE_KEY, level)
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"Could not save the microphone level {level}: {e}; it is kept until the room software restarts")
            self._store.hold(STORE_KEY, level)

    async def reapply(self) -> Dict[str, Any]:
        """Put the chosen level back if the source has moved from it (a meeting app's gain control does that). Nothing when no
        level is chosen. Never raises for a missing source: it is looked for again next time. The lock makes a person's change
        and this take turns, and this always writes the level chosen last."""
        async with self._lock:
            if self._chosen is None:
                return self.state()
            await self._refresh(force=True)
            if not self.available:
                return self.state()
            if abs(self._raw_level - self._chosen / 100) < 0.005:
                return self.state()
            was = self._level
            try:
                await self._write_level(self._chosen)
            except DeviceUnavailable:
                return self.state()
            logger.debug(f"Microphone level put back to {self._chosen} (it had moved to {was})")
            return self.state()
