"""
The room's sound level through PipeWire (spec 2026-10-08 sound and camera, section
4.2): pw-dump lists the sinks and the default one, wpctl reads and sets a sink's
level and mute. Nothing here touches the upstream audio service.

PipeWireLevel is what the speaker and the microphone (croom.devices.mic, spec
2026-10-29 Mic level) share: finding their node in pw-dump, making the room's choice
PipeWire's default, reading and writing its level with wpctl.
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


def parse_nodes(text: str, media_class: str, default_key: str) -> Tuple[List[Tuple[int, str, str]], Optional[str]]:
    """The nodes of one media class ("Audio/Sink", "Audio/Source") as (id, node.name, description), and the node.name the
    default metadata names under default_key ("default.audio.sink", "default.audio.source"), from pw-dump's JSON.

    pw-dump prints a value that looks like a number without quotes, so the names are made text here. JSON of another shape
    raises ValueError, TypeError, AttributeError or KeyError.
    """
    objects = json.loads(text)
    if not isinstance(objects, list):
        raise ValueError("not a list")
    nodes, default_name = [], None
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "PipeWire:Interface:Node":
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("media.class") == media_class:
                name = props.get("node.name")
                name = "" if name is None else str(name)
                description = props.get("node.description") or props.get("node.nick") or name
                nodes.append((int(obj.get("id")), name, str(description)))
        elif obj.get("type") == "PipeWire:Interface:Metadata" and (obj.get("props") or {}).get("metadata.name") == "default":
            for entry in obj.get("metadata") or []:
                if entry.get("key") == default_key and isinstance(entry.get("value"), dict):
                    default = entry["value"].get("name")
                    default_name = None if default is None else str(default)
    return nodes, default_name


class PipeWireLevel:
    """One PipeWire node the room page sets the level of: found in pw-dump by the room's preference (a part of its name or
    description) or as PipeWire's default, read and written with wpctl. RoomVolume (the speaker, a sink) and RoomMic (the
    microphone, a source) are this with their own words and limits."""

    MEDIA_CLASS = "Audio/Sink"
    DEFAULT_KEY = "default.audio.sink"
    NODE = "sink"                      # in the reasons and the log lines: "no audio sink", "PipeWire sink 57"
    LABEL = "Speaker"                  # the log lines' name for it
    DIRECTION = "output"               # "the default output"
    PREFERENCE = "audio.output_device"
    WRONG_DEFAULT = "the call may play on another speaker"   # what a default that could not be set means
    MAX_LEVEL = 100                    # the most the level goes to: wpctl's 1.0 is 100

    def __init__(self, preference: str = "auto", runner: Runner = run_command, clock=time.monotonic):
        self._preference = "" if preference in ("", "auto") else preference
        self._run = runner
        self._clock = clock
        self._node_id: Optional[int] = None
        self._device: Optional[str] = None
        self._level = 0
        self._raw_level = 0.0   # what wpctl reports, which PipeWire lets go above MAX_LEVEL; the page sees at most MAX_LEVEL
        self._muted = False
        self._reason: Optional[str] = "not checked yet"
        self._checked_at: Optional[float] = None
        self._warned: Optional[str] = None
        # The (chosen node, default node) pair a `wpctl set-default` was last run for: a failure, or a default that
        # did not follow, is not tried again every few seconds; a different pair (the default moved again, the
        # node came back with a new id) is.
        self._asked_default: Optional[Tuple[int, Optional[str]]] = None
        # One command sequence at a time: a probe, a set and a step each read and write the state
        # across awaits, so overlapping calls queue instead of interleaving. Every public coroutine
        # takes the lock and calls the private helpers, which never take it.
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._node_id is not None and self._reason is None

    def state(self) -> Dict[str, Any]:
        return {"available": self.available, "device": self._device, "level": self._level, "reason": self._reason}

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def refresh(self, force: bool = False) -> Dict[str, Any]:
        """Find the node and read its level; cached for a few seconds unless forced."""
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
            nodes, default_name = self._parse_dump(out)
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            # ValueError: not JSON at all; the others: JSON of another shape (an id of null, props that are not an object)
            return self._fail(f"pw-dump output not understood: {e}")
        chosen = self._choose(nodes, default_name)
        if chosen is None:
            return self._fail(f"no {self.NODE} matches {self._preference!r}" if self._preference else f"no audio {self.NODE}")
        node_id, name, device = chosen
        if self._preference and name != default_name:
            await self._make_default(node_id, device, default_name)
        else:
            self._asked_default = None
        code, out = await self._run(["wpctl", "get-volume", str(node_id)])
        if code != 0:
            return self._fail(f"wpctl get-volume failed: {out.strip() or code}")
        raw, muted = self._parse_volume(out)
        if raw is None:
            return self._fail(f"wpctl get-volume output not understood: {out.strip()!r}")
        level = round(min(self.MAX_LEVEL / 100, raw) * 100)
        if self._reason is not None:
            names = ", ".join(description for _, _, description in nodes)
            logger.info(f"{self.LABEL}: {device} (PipeWire {self.NODE} {node_id}), level {level}; {self.NODE}s: {names}")
        self._node_id, self._device, self._level, self._raw_level, self._muted = node_id, device, level, raw, muted
        self._reason = None
        self._warned = None
        return self.state()

    async def _make_default(self, node_id: int, device: str, default_name: Optional[str]) -> None:
        """Make the node the room chose PipeWire's default, which the browser uses for the call: without this, the room's
        preference would move the slider of one node while the call uses another.

        Once for each (node, default) pair: a failure warns once and leaves the chosen node in place, so the slider
        still works; it is not tried again until the default or the node changes.
        """
        if self._asked_default == (node_id, default_name):
            return
        self._asked_default = (node_id, default_name)
        code, out = await self._run(["wpctl", "set-default", str(node_id)])
        if code != 0:
            logger.warning(f"Could not make {device} (PipeWire {self.NODE} {node_id}) the default {self.DIRECTION}, so "
                           f"{self.WRONG_DEFAULT}: {out.strip() or code}")
            return
        logger.info(f"Made {device} (PipeWire {self.NODE} {node_id}) the default {self.DIRECTION}, as {self.PREFERENCE} asks "
                    f"(it was {default_name or 'unset'})")

    def _parse_dump(self, text: str) -> Tuple[List[Tuple[int, str, str]], Optional[str]]:
        return parse_nodes(text, self.MEDIA_CLASS, self.DEFAULT_KEY)

    def _choose(self, nodes, default_name) -> Optional[Tuple[int, str, str]]:
        """The node the slider drives, as (id, node.name, description)."""
        if self._preference:
            wanted = self._preference.casefold()
            for node in nodes:
                _, name, description = node
                if wanted in description.casefold() or wanted in name.casefold():
                    return node
            return None
        for node in nodes:
            if node[1] == default_name:
                return node
        return nodes[0] if nodes else None

    @staticmethod
    def _parse_volume(text: str) -> Tuple[Optional[float], bool]:
        """The level as wpctl prints it (1.0 is 100 %, and PipeWire allows more) and whether the node is muted."""
        match = VOLUME_LINE.search(text)
        if not match:
            return None, False
        return float(match.group(1)), bool(match.group(2))

    def _fail(self, reason: str) -> Dict[str, Any]:
        if self._warned != reason:
            logger.warning(f"{self.LABEL} unavailable: {reason}")
            self._warned = reason
        self._node_id = None
        self._reason = reason
        return self.state()

    # ------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------

    async def _node(self) -> int:
        await self._refresh()
        if not self.available:
            raise DeviceUnavailable(self._reason or f"no audio {self.NODE}")
        return self._node_id

    async def _write_level(self, level: int) -> int:
        """Clamp the level to 0 to MAX_LEVEL and write it to the node: the level written. DeviceUnavailable when it cannot be."""
        node_id = await self._node()
        level = max(0, min(self.MAX_LEVEL, int(level)))
        code, out = await self._run(["wpctl", "set-volume", str(node_id), f"{level / 100:.2f}"])
        if code != 0:
            self._fail(f"wpctl set-volume failed: {out.strip() or code}")
            raise DeviceUnavailable(self._reason)
        self._level, self._raw_level = level, level / 100
        self._checked_at = None  # the next status reads it back
        return level


class RoomVolume(PipeWireLevel):
    """The room's speaker: a sink, 0 to 100, with its mute."""

    @classmethod
    def from_config(cls, config) -> "RoomVolume":
        return cls(preference=config.audio.output_device or "auto")

    def state(self) -> Dict[str, Any]:
        return {"available": self.available, "device": self._device, "level": self._level,
                "muted": self._muted, "reason": self._reason}

    async def set_level(self, level: int) -> Dict[str, Any]:
        async with self._lock:
            return await self._set_level(level)

    async def _set_level(self, level: int) -> Dict[str, Any]:
        await self._write_level(level)
        return self.state()

    async def step(self, delta: int) -> Dict[str, Any]:
        """Louder or quieter from the level the speaker is at now. PipeWire allows more than 100 %, which the page
        shows as 100: Louder from there writes nothing (100 % would be quieter), Quieter goes down from 100."""
        async with self._lock:
            await self._refresh(force=True)
            delta = int(delta)
            if delta > 0 and self.available and self._raw_level >= 1.0:
                return self.state()
            return await self._set_level(self._level + delta)

    async def set_muted(self, muted: bool) -> Dict[str, Any]:
        async with self._lock:
            sink_id = await self._node()
            code, out = await self._run(["wpctl", "set-mute", str(sink_id), "1" if muted else "0"])
            if code != 0:
                self._fail(f"wpctl set-mute failed: {out.strip() or code}")
                raise DeviceUnavailable(self._reason)
            self._muted = bool(muted)
            self._checked_at = None
            return self.state()
