"""
The room's sound level through PipeWire (spec 2026-10-08 sound and camera, section
4.2): pw-dump lists the sinks and the default one, wpctl reads and sets a sink's
level and mute. Nothing here touches the upstream audio service.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from croom.devices.errors import DeviceUnavailable

logger = logging.getLogger(__name__)

Runner = Callable[[List[str]], Awaitable[Tuple[int, str]]]
COMMAND_TIMEOUT_S = 2.0
CACHE_S = 3.0
VOLUME_LINE = re.compile(r"Volume:\s*([0-9.]+)(\s*\[MUTED\])?")


async def run_command(args: List[str]) -> Tuple[int, str]:
    """Run a command with a short timeout: (exit code, its stdout, or stderr when it failed).
    A timeout is code 124, a missing command 127; neither raises."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (OSError, ValueError) as e:
        return 127, str(e)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT_S)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"{args[0]} timed out after {COMMAND_TIMEOUT_S:.0f} s"
    text = out if process.returncode == 0 else (err or out)
    return process.returncode, text.decode("utf-8", "replace")


class RoomVolume:
    def __init__(self, preference: str = "auto", runner: Runner = run_command, clock=time.monotonic):
        self._preference = "" if preference in ("", "auto") else preference
        self._run = runner
        self._clock = clock
        self._sink_id: Optional[int] = None
        self._device: Optional[str] = None
        self._level = 0
        self._muted = False
        self._reason: Optional[str] = "not checked yet"
        self._checked_at: Optional[float] = None
        self._warned: Optional[str] = None
        # One command sequence at a time: a probe, a set and a step each read and write the state
        # across awaits, so overlapping calls queue instead of interleaving. Every public coroutine
        # takes the lock and calls the private helpers, which never take it.
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(cls, config) -> "RoomVolume":
        return cls(preference=config.audio.output_device or "auto")

    @property
    def available(self) -> bool:
        return self._sink_id is not None and self._reason is None

    def state(self) -> Dict[str, Any]:
        return {"available": self.available, "device": self._device, "level": self._level,
                "muted": self._muted, "reason": self._reason}

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def refresh(self, force: bool = False) -> Dict[str, Any]:
        """Find the sink and read its level; cached for a few seconds unless forced."""
        async with self._lock:
            return await self._refresh(force)

    async def _refresh(self, force: bool = False) -> Dict[str, Any]:
        """The body of refresh(), for a caller that holds the lock. A caller that had to wait for the
        lock re-checks the cache here, so it can use the probe it waited for."""
        now = self._clock()
        if not force and self._checked_at is not None and now - self._checked_at < CACHE_S:
            return self.state()
        self._checked_at = now
        code, out = await self._run(["pw-dump"])
        if code != 0:
            return self._fail(f"pw-dump failed: {out.strip() or code}")
        try:
            sinks, default_name = self._parse_dump(out)
        except ValueError as e:
            return self._fail(f"pw-dump output not understood: {e}")
        chosen = self._choose(sinks, default_name)
        if chosen is None:
            return self._fail(f"no sink matches {self._preference!r}" if self._preference else "no audio sink")
        sink_id, device = chosen
        code, out = await self._run(["wpctl", "get-volume", str(sink_id)])
        if code != 0:
            return self._fail(f"wpctl get-volume failed: {out.strip() or code}")
        level, muted = self._parse_volume(out)
        if level is None:
            return self._fail(f"wpctl get-volume output not understood: {out.strip()!r}")
        if self._reason is not None:
            logger.info(f"Speaker: {device} (PipeWire sink {sink_id}), level {level}")
        self._sink_id, self._device, self._level, self._muted = sink_id, device, level, muted
        self._reason = None
        self._warned = None
        return self.state()

    @staticmethod
    def _parse_dump(text: str) -> Tuple[List[Tuple[int, str, str]], Optional[str]]:
        """Sinks as (id, node.name, description) and the default sink's node.name, from pw-dump's JSON."""
        objects = json.loads(text)
        if not isinstance(objects, list):
            raise ValueError("not a list")
        sinks, default_name = [], None
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "PipeWire:Interface:Node":
                props = (obj.get("info") or {}).get("props") or {}
                if props.get("media.class") == "Audio/Sink":
                    name = props.get("node.name", "")
                    description = props.get("node.description") or props.get("node.nick") or name
                    sinks.append((int(obj.get("id")), name, description))
            elif obj.get("type") == "PipeWire:Interface:Metadata" and (obj.get("props") or {}).get("metadata.name") == "default":
                for entry in obj.get("metadata") or []:
                    if entry.get("key") == "default.audio.sink" and isinstance(entry.get("value"), dict):
                        default_name = entry["value"].get("name")
        return sinks, default_name

    def _choose(self, sinks, default_name) -> Optional[Tuple[int, str]]:
        if self._preference:
            wanted = self._preference.casefold()
            for sink_id, name, description in sinks:
                if wanted in description.casefold() or wanted in name.casefold():
                    return sink_id, description
            return None
        for sink_id, name, description in sinks:
            if name == default_name:
                return sink_id, description
        return (sinks[0][0], sinks[0][2]) if sinks else None

    @staticmethod
    def _parse_volume(text: str) -> Tuple[Optional[int], bool]:
        match = VOLUME_LINE.search(text)
        if not match:
            return None, False
        return round(min(1.0, float(match.group(1))) * 100), bool(match.group(2))

    def _fail(self, reason: str) -> Dict[str, Any]:
        if self._warned != reason:
            logger.warning(f"Speaker unavailable: {reason}")
            self._warned = reason
        self._sink_id = None
        self._reason = reason
        return self.state()

    # ------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------

    async def _sink(self) -> int:
        await self._refresh()
        if not self.available:
            raise DeviceUnavailable(self._reason or "no audio sink")
        return self._sink_id

    async def set_level(self, level: int) -> Dict[str, Any]:
        async with self._lock:
            return await self._set_level(level)

    async def _set_level(self, level: int) -> Dict[str, Any]:
        sink_id = await self._sink()
        level = max(0, min(100, int(level)))
        code, out = await self._run(["wpctl", "set-volume", str(sink_id), f"{level / 100:.2f}"])
        if code != 0:
            self._fail(f"wpctl set-volume failed: {out.strip() or code}")
            raise DeviceUnavailable(self._reason)
        self._level = level
        self._checked_at = None  # the next status reads it back
        return self.state()

    async def step(self, delta: int) -> Dict[str, Any]:
        async with self._lock:
            await self._refresh(force=True)
            return await self._set_level(self._level + int(delta))

    async def set_muted(self, muted: bool) -> Dict[str, Any]:
        async with self._lock:
            sink_id = await self._sink()
            code, out = await self._run(["wpctl", "set-mute", str(sink_id), "1" if muted else "0"])
            if code != 0:
                self._fail(f"wpctl set-mute failed: {out.strip() or code}")
                raise DeviceUnavailable(self._reason)
            self._muted = bool(muted)
            self._checked_at = None
            return self.state()
