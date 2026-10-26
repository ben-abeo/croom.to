"""
`croom --check-tv`: prove the room's TV can be reached from this device, in plain words (spec 2026-10-13 TV remote and
touchpad, section 3). It looks and changes nothing: the TV is not woken, not paired, not switched, and nothing is left connected.
(The one thing that can change is the key: a TV that did not know the stored one shows its pairing prompt, and if somebody
accepts it during the look the TV issues a new key, which is kept as it is at every connect, and the check says so.)
With no TV configured it lists the LG and Samsung TVs on the network, as Find TVs does on the table. Exit 0 when the TV answered,
1 otherwise.
"""

import asyncio
import contextlib
import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional, TextIO, Tuple

from croom.core.config import Config
from croom.tv.discover import discover_tvs
from croom.tv.errors import TvError, TvTimeout
from croom.tv.keystore import KeyStore
from croom.tv.service import REFUSED, TvService, shows_the_pi
from croom.tv.wake import mac_problem

CHECK_TIMEOUT_S = 5.0       # the look at the TV has this long to be answered
LET_GO_TIMEOUT_S = 3.0      # and hanging up on it this long

Discover = Callable[[], Awaitable[List[Dict[str, str]]]]


async def check_tv(config: Config, out: TextIO = sys.stdout, service_factory: Optional[Callable[[Config], TvService]] = None,
                   discover: Optional[Discover] = None) -> int:
    """Run the check against the configured TV and print the outcome. `service_factory` makes the TV service (the config's own
    by default) and `discover` is the search of the network (Find TVs), which the tests replace."""
    service = None
    if service_factory is None:
        try:
            # Restore the selection made on the table before inspecting the YAML defaults.
            service = TvService.from_config(config)
        except OSError as e:
            print(f"The TV service could not be set up: {e}", file=out)
            return 1
    tv = config.tv
    host = str(tv.host or "").strip()
    if not host:
        print("No TV is configured. On the table use Options > TV > Find TVs and select yours.", file=out)
        await _find_tvs(out, discover or discover_tvs)
        return 1
    warnings = 0

    def warn(words: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"Warning: {words}", file=out)

    mac_text, problem = _mac(tv.mac)
    print(f"TV: {host}, MAC {mac_text}", file=out)
    if problem:
        warn(problem)
    if not tv.enabled:
        print("The TV is switched off in the config (tv.enabled is false), so the agent leaves it alone: set tv.enabled to true.",
              file=out)
        return 1
    try:
        service = service or service_factory(config)
    except OSError as e:                    # (the agent's data directory cannot be made, or read, by this user)
        print(f"The TV service could not be set up: {e}", file=out)
        return 1
    try:
        if not service.status()["paired"]:
            print("The TV is not paired with this device: pair it under Options > TV on the room's table. This check found no "
                  f"TV key it can read at {service.key_path}; if the TV is paired, run the check as the user the agent runs as.",
                  file=out)
            return 1
        print(f"The TV is paired with this device (its key is in {service.key_path}).", file=out)
        key_before = KeyStore(service.key_path).read()
        try:
            await service.probe(CHECK_TIMEOUT_S)
        except TvError as e:
            print(e, file=out)
            if str(e) != REFUSED:
                print("Check that the TV is on and that tv.host is its address: a TV that is fully off does not answer.",
                      file=out)
            if isinstance(e, TvTimeout):        # (it may be showing its pairing prompt: the library cannot tell the two apart)
                print("If the TV is showing a prompt to allow this device, it no longer knows this device: Forget TV, then Pair, "
                      "under Options > TV on the room's table.", file=out)
            return 1
        if KeyStore(service.key_path).read() != key_before:       # (a look that fails has not got as far as a new key)
            print("The TV issued a new key (its pairing prompt was accepted during the check), which is kept.", file=out)
        shown = service.status()
        state = ("power state unavailable" if shown.get("power_known") is False
                 else "on" if shown["on"] else "in standby (its screen is off)")
        print(f"The TV answers: {shown['name'] or 'it gives no name'}, {state}.", file=out)
        inputs = shown["inputs"]
        if inputs:
            print("Inputs: " + ", ".join(_input_words(inputs, item["id"]) for item in inputs) + ".", file=out)
        else:
            print("The TV lists no inputs.", file=out)
        _check_input(tv, shown, out, warn)
        print("The TV answers, with the warnings above." if warnings else "Ready: the TV answers.", file=out)
        return 0
    finally:
        await _let_go(service)


def _mac(mac: Any) -> Tuple[str, Optional[str]]:
    """The MAC address as the first line shows it, and what is wrong with it, in words, when it is no use for turning the TV on.
    A value that is not text is one YAML made something of: an address of digits only, 10:20:30:40:50:59, is a number to it."""
    if mac is not None and not isinstance(mac, str):
        return str(mac), (f"tv.mac is not text ({mac!r}): YAML reads an address of digits only, such as 10:20:30:40:50:59, as a "
                          'number, so write it in quotes: tv.mac: "10:20:30:40:50:59".')
    return (mac or "").strip() or "not set", mac_problem(mac)


def _input_words(inputs: List[Dict[str, str]], input_id: str) -> str:
    """An input as a line has it: its label and its id, or its id alone."""
    label = next((item["label"] for item in inputs if item["id"] == input_id), "")
    return f"{label} ({input_id})" if label and label != input_id else input_id


def _check_input(tv: Any, shown: Dict[str, Any], out: TextIO, warn: Callable[[str], None]) -> None:
    """What the TV shows against the input the room Pi is on (tv.pi_input): what it shows when it is on, and a warning when the
    room Pi's input is not one the TV has, or the TV shows something else (a meeting join switches it, unless the TV is not to be
    kept on)."""
    if shown.get("capabilities", {}).get("direct_inputs") is False:
        print("Use Input on the table to choose the room Pi's HDMI source on the TV; this TV does not report its input.", file=out)
        return
    pi = str(tv.pi_input or "").strip()
    ids = [item["id"] for item in shown["inputs"]]
    on_pi = shows_the_pi(pi, shown["input"], shown["app"]) if shown["on"] else None
    if shown["on"] and (shown["input"] is not None or shown["app"]):
        what = _input_words(shown["inputs"], shown["input"]) if shown["input"] is not None else f"the app {shown['app']}"
        print(f"The TV shows {what}" + (", the room Pi's input (tv.pi_input)." if on_pi else "."), file=out)
    if not pi:
        warn("tv.pi_input is not set, so a meeting join cannot switch the TV to the room Pi.")
    elif ids and pi not in ids:
        warn(f"tv.pi_input is {pi}, which is not one of the TV's inputs ({', '.join(ids)}): a meeting join cannot switch the TV "
             "to the room Pi.")
    elif on_pi is False:
        warn(f"The TV is not showing the room Pi's input {pi} (tv.pi_input): "
             + ("a meeting join switches it." if tv.keep_on else "tv.keep_on is false, so a meeting join leaves it."))


async def _find_tvs(out: TextIO, discover: Discover) -> None:
    """What Find TVs finds, for a room with no TV set up: the TVs that answer, by name and address."""
    print("Looking for LG and Samsung TVs on the network (this takes a few seconds)...", file=out)
    try:
        found = await discover()
    except TvError as e:
        print(e, file=out)
        return
    if not found:
        print("No TV answered. Check that the TV is on, on the same network as this device, and allows network control.", file=out)
        return
    print(f"Found {len(found)} TV{'' if len(found) == 1 else 's'}:", file=out)
    for tv in found:
        print(f"  {tv['name'] or '(no name)'}  {tv['host']}", file=out)
    print("On the room's table open Options > TV > Find TVs, select yours, then accept the request on the TV. "
          "The room saves its connection settings automatically.", file=out)


async def _let_go(service: TvService) -> None:
    """Hang up on the TV, in the time that is given for it: the check is over, and a hang-up that goes wrong is no news."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(service.stop(), LET_GO_TIMEOUT_S)
