"""A name that a device gave itself, as one printable line: a TV (and whatever answers on the network) chooses what it is called, and
the name is shown on the table and printed on a terminal."""
import pytest
from croom.tv.names import MAX_NAME_LENGTH, one_line


@pytest.mark.parametrize("text, line", [
    ("Boardroom TV", "Boardroom TV"),
    ("  Board\n   room\tTV  ", "Board room TV"),             # one line, one space between words
    ("Salle de réunion", "Salle de réunion"),                # what a terminal can show stays: it is the name
    ("Board\x1b[2Jroom", "Board[2Jroom"),                     # an escape is dropped, so what follows it is only text
    ("Boardroom\x7f\x9b TV", "Boardroom TV"),                # DEL and a C1 CSI
    ("Board​room", "Boardroom"),                         # a zero-width space
    ("a b", "a b"),                                      # a line separator is a break
    ("\x1b\x07", ""),                                         # nothing printable is no name
    (None, ""), ("", ""), (12, "12"),
])
def test_a_name_is_one_printable_line(text, line):
    assert one_line(text) == line


def test_a_name_is_no_longer_than_a_line_that_a_table_has_room_for():
    assert MAX_NAME_LENGTH == 100
    assert one_line("x" * 300) == "x" * 100
    assert one_line("x" * 300, 5) == "xxxxx"
