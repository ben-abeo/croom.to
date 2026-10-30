"""
The Devices page's choices (spec 2026-10-29, section 3): the speakers and microphones PipeWire has now (pw-dump, read as the speaker and
the Mic level read it), and the cameras at /dev/video* (by the name each node gives), each list with Automatic first.

A speaker or microphone is written as its PipeWire node.name, which the room's preference matches (croom.devices.volume); a camera as its
/dev/video node, which RoomCamera opens. The value the config has now stays in its list even when the device is not there now (unplugged,
or written by hand as a part of a name), so that opening the page and saving something else never changes it.
"""

import asyncio
import copy
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from croom.devices.volume import parse_nodes, run_command

logger = logging.getLogger(__name__)

Choice = Tuple[str, str]   # (value, label)
Found = Dict[str, List[Choice]]
Runner = Callable[[List[str]], Awaitable[Tuple[int, str]]]

AUTOMATIC: Choice = ("auto", "Automatic")
SPEAKER, MICROPHONE, CAMERA = "audio.output_device", "audio.input_device", "video.device"
NOT_THERE = "not connected now"


def _audio(text: str, media_class: str, default_key: str) -> List[Choice]:
    nodes, _ = parse_nodes(text, media_class, default_key)
    seen, choices = set(), []
    for _, name, description in nodes:
        if name and name not in seen:
            seen.add(name)
            choices.append((name, description or name))
    return choices


def _cameras(v4l2) -> List[Choice]:
    if v4l2 is None:
        from croom.devices.v4l2 import V4l2Controls
        v4l2 = V4l2Controls()
    choices = []
    for path in v4l2.nodes():
        name = v4l2.camera_name(path)
        if name:
            choices.append((path, f"{name} ({path})"))
    return choices


async def find_devices(run: Runner = run_command, v4l2=None) -> Found:
    """{setting path: [(value, label)]} for what the room has now: the speakers, the microphones and the cameras (without Automatic).
    A list that can't be read is empty, and the page still offers Automatic and the value the config has."""
    found: Found = {SPEAKER: [], MICROPHONE: [], CAMERA: []}
    code, out = await run(["pw-dump"])
    if code == 0:
        try:
            found[SPEAKER] = _audio(out, "Audio/Sink", "default.audio.sink")
            found[MICROPHONE] = _audio(out, "Audio/Source", "default.audio.source")
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            logger.warning(f"Devices page: pw-dump output not understood: {e}")
    else:
        logger.warning(f"Devices page: pw-dump failed: {out.strip() or code}")
    try:
        found[CAMERA] = await asyncio.to_thread(_cameras, v4l2)
    except Exception as e:  # noqa: BLE001 - the page offers Automatic and the config's value whatever goes wrong here
        logger.warning(f"Devices page: the cameras could not be listed: {type(e).__name__}: {e}")
    return found


def offer(fields: List[Dict[str, Any]], values: Dict[str, Any], found: Optional[Found]) -> List[Dict[str, Any]]:
    """The Devices page's fields with the room's devices as each device setting's choices: Automatic, what was found, and the config's
    value when it is neither (labelled as not connected now)."""
    fields = copy.deepcopy(fields)
    for field in fields:
        path = field.get("path")
        if path not in (SPEAKER, MICROPHONE, CAMERA):
            continue
        choices = [AUTOMATIC] + [c for c in (found or {}).get(path, []) if c[0] != AUTOMATIC[0]]
        current = values.get(path)
        if isinstance(current, str) and current and current not in {value for value, _ in choices}:
            # A part of a name, written by hand, is matched as the speaker and the microphone match it: say which device it is now
            wanted = current.casefold()
            match = next((label for value, label in choices[1:] if wanted in value.casefold() or wanted in label.casefold()), None)
            choices.append((current, f"{current} (now {match})" if match and path != CAMERA else f"{current} ({NOT_THERE})"))
        field["choices"] = [{"value": value, "label": label} for value, label in choices]
    return fields
