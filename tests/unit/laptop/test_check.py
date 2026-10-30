"""`croom --check-laptop`: the pin, where the MeetUp is, and with --switch one move the other way."""
import io

from croom.core.config import Config
from croom.laptop.check import check_laptop
from tests.unit.laptop.fakes import FakeUsbSwitch


class CheckedSwitch(FakeUsbSwitch):
    chip = "/dev/gpiochip4"


async def test_the_check_says_where_the_meetup_is_and_presses_nothing():
    out, switch = io.StringIO(), CheckedSwitch()
    assert await check_laptop(Config(), out, switch=switch) == 0
    text = out.getvalue()
    assert "GPIO17 (header pin 11)" in text and "/dev/gpiochip4" in text and "on this Pi" in text
    assert "laptop.enabled is false" in text and switch.moves == [] and switch.closed


async def test_the_check_with_switch_moves_it_the_other_way():
    out, switch = io.StringIO(), CheckedSwitch()
    assert await check_laptop(Config(), out, switch=switch, move=True) == 0
    assert switch.moves == ["laptop"] and "now on the laptop side" in out.getvalue()


async def test_a_switch_that_does_not_move_fails_the_check():
    out, switch = io.StringIO(), CheckedSwitch()
    switch.answers = False
    assert await check_laptop(Config(), out, switch=switch, move=True) == 1
    assert "did not answer" in out.getvalue()


async def test_a_pin_that_cannot_be_had_fails_the_check_and_says_why():
    out = io.StringIO()
    assert await check_laptop(Config(), out, switch=CheckedSwitch(available=False)) == 1
    assert "busy" in out.getvalue() and "systemctl stop croom" in out.getvalue()


async def test_a_held_switch_is_neither_opened_nor_closed():
    # Settings' Test the switch: the running agent's switch, which must stay the agent's
    out, switch = io.StringIO(), CheckedSwitch()
    assert await check_laptop(Config(), out, switch=switch, move=True, held=True) == 0
    assert switch.moves == ["laptop"] and not switch.opened and not switch.closed


async def test_a_held_switch_that_is_not_available_says_why_without_the_command_line_hint():
    out, switch = io.StringIO(), CheckedSwitch(available=False)
    assert await check_laptop(Config(), out, switch=switch, held=True) == 1
    assert "busy" in out.getvalue() and "systemctl" not in out.getvalue() and not switch.closed
