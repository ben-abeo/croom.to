"""A name that a device gave itself, as one printable line. A TV (and whatever answers a search of the network) chooses what it is
called, and the name is shown on the table and printed on a terminal: it is text and never markup, never an escape."""

from typing import Any

MAX_NAME_LENGTH = 100


def one_line(text: Any, limit: int = MAX_NAME_LENGTH) -> str:
    """`text` on one line of at most `limit` characters: runs of whitespace become one space, and the characters that are not
    printable (escapes and other controls, separators, zero-width marks) are dropped. What a terminal can show stays."""
    line = " ".join(str(text or "").split())
    return "".join(char for char in line if char.isprintable())[:limit]
