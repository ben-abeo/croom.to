"""
The capture of Google Meet's page (spec 2026-10-30 Meet controls, section 3): during a call, the TV opens Meet's panels one after another
(the chat, the people panel, the reactions picker, More options, Change layout, the room's own tile menu, the host controls) and records
each one's elements and a screenshot, then closes it; any box on the screen is recorded as it is. The Phase B controls are built from what
Meet really draws, and a new capture shows what changed when Meet changes.

What the capture presses is only what opens or closes a panel, found by its accessibility label (Meet's keyboard shortcut is the second
way in), Escape, and a Close button; a label that admits, denies, removes, mutes everyone, sends, ends or leaves is never pressed, whatever
finds it. A panel it opened is closed before the next step and, if that failed, once more at the end (a `finally`). The whole run is
limited to a minute.

Names (from the people panel's list items, the participant tiles and labels that hold one), email addresses and the chat's messages are
replaced in every recorded text and label by "Person 1", "person1@example.com" and "Message 1", the same placeholder for the same words
throughout. The screenshots show the TV as it was: the Check page's switch leaves them out.

The file is a zip in the room's data directory (the newest three are kept): capture.json and a PNG for each step.
"""

import asyncio
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from croom.meeting.meet_page import GATHER_PRIVATE, PAGE_STATE, READ_DIALOGS, RECORD_ELEMENTS

logger = logging.getLogger(__name__)

JOIN_MEET_FIRST = "Join a Google Meet call first"
ALREADY_CAPTURING = "A capture of Google Meet is running already"
NO_CAPTURE = "No capture of Google Meet yet"
UP = "The capture's minute was up"
FILE_PATTERN = "meet-capture-*.zip"
KEEP = 3            # the newest captures kept in the data directory
TEXT_KEPT = 120     # an element's text is kept to this many characters, after it is redacted whole

# A label the capture never presses, whatever pattern found it: one that lets someone in or keeps them out, removes, mutes everyone, sends
# (a message; "Send a reaction" only opens the picker), ends or leaves.
NEVER_PRESSED = re.compile(r"\b(admit|deny|remove|mute everyone|mute all|end|leave|hang up|exit|quit|kick)\b|\bsend\b(?! a reaction)", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Step:
    """One thing the capture records. `opener` is the accessibility label of the button that opens it (a regular expression, any case);
    `shortcut` Meet's key for it, tried when the button is not found or opens nothing; `via` the label of the button whose menu holds the
    opener (then a menu item); `own` prefers the opener whose label names the room or "you" (the room's own tile)."""
    name: str
    words: str
    opener: Optional[str] = None
    shortcut: Optional[str] = None
    via: Optional[str] = None
    own: bool = False


STEPS: Tuple[Step, ...] = (
    Step("call", "The call"),
    Step("chat", "Chat", r"^chat with everyone|^chat\b|^open chat|^in-call messages", "Control+Alt+c"),
    Step("people", "People", r"^show everyone|^people\b|^participants\b", "Control+Alt+p"),
    Step("reactions", "Reactions", r"^send a reaction|^reactions?\b|^show reactions"),
    Step("more_options", "More options", r"^more options$"),
    Step("layout", "Change layout", r"^change layout|^adjust view", via=r"^more options$"),
    Step("own_tile", "The room's tile menu", r"^more options for ", own=True),
    Step("host_controls", "Host controls", r"^host controls|^meeting safety"),
    Step("dialogs", "Boxes on the screen"),
)


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def _first_line(error: BaseException) -> str:
    return next((line.strip() for line in str(error).splitlines() if line.strip()), "") or type(error).__name__


class Redactor:
    """What is people's on Meet's page, learned as the capture goes, and put in place of every time it is seen: a name by "Person N", an
    email address by "personN@example.com", a chat message by "Message N" (the same N for the same words)."""

    def __init__(self) -> None:
        self.names: Dict[str, str] = {}      # by the name in lower case
        self.messages: Dict[str, str] = {}
        self.emails: Dict[str, str] = {}     # by the address in lower case

    def learn(self, names: Iterable[str] = (), messages: Iterable[str] = ()) -> None:
        for name in map(_clean, names):
            name = re.sub(r"\s*\(you\)$", "", name, flags=re.I)   # Meet's mark of the room's own tile stays, as Meet's word
            if len(name) >= 2 and not EMAIL.fullmatch(name) and name.lower() not in self.names:
                self.names[name.lower()] = f"Person {len(self.names) + 1}"
        for message in map(_clean, messages):
            if message and message not in self.messages and message.lower() not in self.names:   # a sender's name is a name
                self.messages[message] = f"Message {len(self.messages) + 1}"

    def _email(self, match: "re.Match") -> str:
        address = match.group(0).lower()
        if address not in self.emails:
            self.emails[address] = f"person{len(self.emails) + 1}@example.com"
        return self.emails[address]

    @staticmethod
    def _whole(words: str) -> "re.Pattern":
        return re.compile(r"(?<!\w)" + re.escape(words) + r"(?!\w)", re.I)

    def __call__(self, text: str) -> str:
        for message in sorted(self.messages, key=len, reverse=True):     # a message may hold a name or an address: it goes whole first
            text = self._whole(message).sub(self.messages[message], text)
        text = EMAIL.sub(self._email, text)
        for name in sorted(self.names, key=len, reverse=True):           # "Pat Doe" before "Pat"
            text = self._whole(name).sub(self.names[name], text)
        return text


# What a recording keeps as it is: Meet's structure, never people's words.
_AS_IS = {"tag", "role", "data", "box", "parent", "name", "opened", "closed", "modal", "pressed", "expanded", "checked", "selected",
          "haspopup", "disabled", "screenshot"}


def redacted(value: Any, redact: Redactor, key: Optional[str] = None) -> Any:
    """`value` with every string in it redacted, but for the fields that are Meet's structure; an element's text is cut to TEXT_KEPT
    characters after it is redacted whole (a message cut in the middle would not be found)."""
    if isinstance(value, str):
        if key in _AS_IS:
            return value
        text = redact(value)
        return text[:TEXT_KEPT] if key == "text" else text
    if isinstance(value, dict):
        return {k: redacted(v, redact, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v, redact, key) for v in value]
    return value


def _key(panel: Dict[str, Any]) -> str:
    """A panel as PAGE_STATE and GATHER_PRIVATE know it (JSON.stringify([role, label, modal]))."""
    return json.dumps([panel.get("role", ""), panel.get("label", ""), panel.get("modal", "")], separators=(",", ":"))


def app_version() -> str:
    """The room software's version: its release's commit when the installer wrote one, else the package's."""
    import croom
    for folder in Path(croom.__file__).resolve().parents[1:4]:
        try:
            commit = (folder / "VERSION").read_text().strip()
        except OSError:
            continue
        if commit:
            return f"{croom.__version__} ({commit})"
    return croom.__version__


class MeetCapture:
    """One capture of Meet's call page: `await MeetCapture(page).run()` is capture.json's content; `images` the screenshots by file name
    and `pressed` what was pressed (labels and keys), in order."""

    OPEN_WAIT_S = 2.0        # a panel shows within this after its button is pressed
    CLOSE_WAIT_S = 1.5       # and goes within this after Escape or Close
    POLL_S = 0.1
    CLICK_TIMEOUT_MS = 2000
    ANSWER_S = 10            # a page script or a screenshot answers within this
    LIMIT_S = 60             # the whole capture

    def __init__(self, page, redact: Optional[Redactor] = None, screenshots: bool = True, room_name: str = ""):
        self.page = page
        self.redact = redact if redact is not None else Redactor()
        self.screenshots = bool(screenshots)
        self.room_name = room_name or ""
        self.images: Dict[str, bytes] = {}
        self.pressed: List[str] = []
        self._facts: Dict[str, str] = {}
        self._deadline = float("inf")
        self._left_open: List[Tuple[Step, Dict[str, Any], Dict[str, Any]]] = []   # (step, the page before it, its record)

    async def run(self) -> Dict[str, Any]:
        # The minute is kept by each step looking at the clock before it opens anything and while it waits for a panel, never by cancelling
        # it: a step stopped in the middle of a press could not be trusted to close what it opened. Closing is never cut short.
        self._deadline = time.monotonic() + self.LIMIT_S
        records: List[Dict[str, Any]] = []
        try:
            for number, step in enumerate(STEPS, 1):
                record: Dict[str, Any] = {"name": step.name}
                records.append(record)
                if self._over():
                    record.update(opened=False, why=UP)
                    continue
                await self._step(number, step, record)
        finally:
            for step, before, record in self._left_open:   # what a step could not close: once more, whatever happened since
                try:
                    record["closed"] = await self._close(step, before)
                except Exception as e:  # noqa: BLE001 - the page gone: nothing is open on it
                    logger.warning(f"Could not close Meet's {step.words} after the capture: {_first_line(e)}")
                if not record.get("closed"):
                    logger.warning(f"The capture left Meet's {step.words} open on the TV")
        user_agent = self._facts.get("user_agent", "")
        chromium = re.search(r"(?:Headless)?Chrome/([\d.]+)", user_agent)
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "versions": {"chromium": chromium.group(1) if chromium else "", "meet_lang": self._facts.get("lang", ""), "app": app_version()},
            "document": redacted({"title": self._facts.get("title", ""), "path": self._facts.get("path", "")}, self.redact),
            "screenshots": self.screenshots,
            "pressed": redacted(list(self.pressed), self.redact),
            "steps": redacted(records, self.redact),
        }

    # --- one step

    async def _step(self, number: int, step: Step, record: Dict[str, Any]) -> None:
        """Record one step into `record`; a panel it opened is closed before it returns, or kept in _left_open for the end."""
        if step.opener is None:
            try:
                if step.name == "dialogs":
                    boxes = await self._eval(READ_DIALOGS)
                    if not boxes:
                        record.update(opened=False, why="No box on Meet's screen")
                        return
                    record["boxes"] = boxes
                record["opened"] = True
                await self._take(number, step, record, [])
            except Exception as e:  # noqa: BLE001 - recorded, and the capture goes on
                record.update(opened=False, why=f"Could not record it: {_first_line(e)}")
            return
        try:
            before = await self._state(step)
        except Exception as e:  # noqa: BLE001 - the page did not answer: nothing was pressed
            record.update(opened=False, why=f"Could not read Meet's page: {_first_line(e)}")
            return
        try:
            by, why = await self._open(step, before) if not self._over() else (None, UP)
            if by is None:
                record.update(opened=False, why=why)
            else:
                new = self._new_panels(before, await self._state(step))
                record.update(opened=True, by=by, panels=new)
                await self._take(number, step, record, [_key(panel) for panel in new])
        except Exception as e:  # noqa: BLE001 - recorded, closed below, and the capture goes on
            record.setdefault("opened", False)
            record["why"] = f"Could not record it: {_first_line(e)}"
        finally:
            try:
                record["closed"] = await self._close(step, before)
            except Exception as e:  # noqa: BLE001 - tried again at the end
                logger.warning(f"Could not close Meet's {step.words}: {_first_line(e)}")
                record["closed"] = False
            if record["closed"] is False:
                self._left_open.append((step, before, record))

    async def _take(self, number: int, step: Step, record: Dict[str, Any], keys: List[str]) -> None:
        """Record what the page draws now, learn the people's words on it, and take the screenshot."""
        page = await self._eval(RECORD_ELEMENTS)
        if not self._facts:
            self._facts = {key: str(page.get(key) or "") for key in ("title", "path", "lang", "user_agent")}
        record["elements"] = page.get("elements") or []
        private = await self._eval(GATHER_PRIVATE, [step.name, keys])
        self.redact.learn(private.get("names") or [], private.get("messages") or [])
        if self.screenshots:
            name = f"{number:02d}-{step.name}.png"
            self.images[name] = await asyncio.wait_for(self.page.screenshot(), self.ANSWER_S)
            record["screenshot"] = name

    # --- opening

    async def _open(self, step: Step, before: Dict[str, Any]) -> Tuple[Optional[str], str]:
        """Open the step's panel: by its button, then by Meet's shortcut. (how it was opened, "") or (None, why not)."""
        if step.via:
            via = await self._press(step.via)
            if via is None:
                return None, "Meet shows no More options button"
            if not await self._wait_open(step, before):
                return None, f"{via} opened nothing"
            menu = {_key(panel) for panel in self._new_panels(before, await self._state(step))}
            item = await self._press(step.opener, roles=("menuitem", "menuitemradio", "option", "button"))
            if item is None:
                return None, f"Meet's {via} menu has no {step.words} item"
            if not await self._wait_open(step, before, besides=menu, by_opener=False):
                return None, f"{item} opened nothing"
            return f"{via} › {item}", ""
        label = await self._press(step.opener, own=step.own)
        if label is not None:
            if await self._wait_open(step, before):
                return label, ""
            why = f"{label} opened nothing"
        else:
            why = f"Meet shows no {step.words} button"
        if step.shortcut and not self._over():
            await self._key(step.shortcut)
            if await self._wait_open(step, before):
                return step.shortcut, ""
            why += f"; {step.shortcut} opened nothing"
        return None, why

    async def _wait_open(self, step: Step, before: Dict[str, Any], besides: Iterable[str] = (), by_opener: bool = True) -> bool:
        """Whether a panel the page did not have before shows (besides those named), or the opener says it is open, within OPEN_WAIT_S."""
        besides = set(besides)
        end = min(time.monotonic() + self.OPEN_WAIT_S, self._deadline)
        while True:
            now = await self._state(step)
            if [p for p in self._new_panels(before, now) if _key(p) not in besides] or (by_opener and self._opener_open(before, now)):
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(self.POLL_S)

    # --- closing

    async def _close(self, step: Step, before: Dict[str, Any]) -> bool:
        """Close what the step opened: Escape, then a Close button, then its own button again when it says it is open; True when the page
        has no panel it did not have before and the button is as it was."""
        if self._closed(before, await self._state(step)):
            return True
        for attempt in (self._escape, self._close_button, self._toggle_back):
            if await attempt(step, before) and await self._wait_closed(step, before):
                return True
        return self._closed(before, await self._state(step))

    async def _escape(self, step: Step, before: Dict[str, Any]) -> bool:
        await self._key("Escape")
        return True

    async def _close_button(self, step: Step, before: Dict[str, Any]) -> bool:
        return await self._press(r"^close\b") is not None

    async def _toggle_back(self, step: Step, before: Dict[str, Any]) -> bool:
        if not self._opener_open(before, await self._state(step)):
            return False   # pressed again, a button that is not open would open its panel
        return await self._press(step.via or step.opener, own=step.own) is not None

    async def _wait_closed(self, step: Step, before: Dict[str, Any]) -> bool:
        end = time.monotonic() + self.CLOSE_WAIT_S
        while True:
            if self._closed(before, await self._state(step)):
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(self.POLL_S)

    def _over(self) -> bool:
        return time.monotonic() >= self._deadline

    # --- the page

    async def _state(self, step: Step) -> Dict[str, Any]:
        return await self._eval(PAGE_STATE, step.via or step.opener)

    @staticmethod
    def _new_panels(before: Dict[str, Any], now: Dict[str, Any]) -> List[Dict[str, Any]]:
        had = {_key(panel) for panel in before.get("panels") or []}
        return [panel for panel in now.get("panels") or [] if _key(panel) not in had]

    @staticmethod
    def _opener_open(before: Dict[str, Any], now: Dict[str, Any]) -> bool:
        opener = now.get("opener")
        return bool(opener) and "true" in (opener.get("pressed"), opener.get("expanded")) and opener != before.get("opener")

    def _closed(self, before: Dict[str, Any], now: Dict[str, Any]) -> bool:
        opener, was = now.get("opener"), before.get("opener")
        return not self._new_panels(before, now) and (opener is None or was is None or opener == was)

    async def _eval(self, script: str, arg: Any = None) -> Any:
        return await asyncio.wait_for(self.page.evaluate(script, arg), self.ANSWER_S)

    async def _key(self, key: str) -> None:
        self.pressed.append(key)
        await asyncio.wait_for(self.page.keyboard.press(key), self.ANSWER_S)

    async def _press(self, pattern: str, roles: Tuple[str, ...] = ("button",), own: bool = False) -> Optional[str]:
        """Press the first drawn element of those roles whose accessible name matches `pattern` (any case), or with `own` the one that names
        the room or "you" when there is one; its label, or None when there is none. Never one that NEVER_PRESSED names."""
        found: List[Tuple[Any, str]] = []
        for role in roles:
            candidates = self.page.get_by_role(role, name=re.compile(pattern, re.I))
            for i in range(await candidates.count()):
                element = candidates.nth(i)
                if not await element.is_visible():
                    continue
                label = _clean(await element.get_attribute("aria-label") or await element.inner_text())
                if NEVER_PRESSED.search(label):
                    logger.warning(f"The capture does not press {label!r}")
                    continue
                found.append((element, label))
            if found:
                break
        if not found:
            return None
        element, label = found[0]
        if own:
            room = self.room_name.lower()
            element, label = next(((e, l) for e, l in found if (room and room in l.lower()) or re.search(r"\byou\b", l, re.I)), found[0])
        self.pressed.append(label)
        await element.click(timeout=self.CLICK_TIMEOUT_MS)
        return label


def save_capture(capture: Dict[str, Any], images: Dict[str, bytes], directory: Path, now: Optional[datetime] = None) -> Path:
    """Write the capture as meet-capture-<UTC time>.zip in `directory` (capture.json, then the screenshots) and keep the newest KEEP."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"meet-capture-{stamp}.zip"
    part = path.with_name(path.name + ".part")
    with zipfile.ZipFile(part, "w") as archive:
        archive.writestr("capture.json", json.dumps(capture, indent=1, ensure_ascii=False), compress_type=zipfile.ZIP_DEFLATED)
        for name, data in images.items():
            archive.writestr(name, data)   # a PNG is compressed already
    part.replace(path)
    for old in sorted(directory.glob(FILE_PATTERN))[:-KEEP]:
        old.unlink(missing_ok=True)
    return path


def newest_capture(directory: Optional[Path]) -> Optional[Path]:
    """The newest capture in `directory`, or None."""
    if directory is None or not Path(directory).is_dir():
        return None
    found = sorted(Path(directory).glob(FILE_PATTERN))
    return found[-1] if found else None
