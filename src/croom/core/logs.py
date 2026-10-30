"""
What the agent writes to its log, and at what level.

The agent logs warnings and errors and nothing more unless it is started with -v (everything at INFO) or --debug, and the installed unit
starts it with neither, so a stock install's journal holds what went wrong and little else. The dialogs Zoom draws over the TV are the one
thing the journal must always hold (spec 2026-10-13, section 5, "log and learn"): a new dialog is judged, and its phrase added to the
acknowledge list, from the journal, a handful of lines in a meeting and read weeks after it. Their lines come from one logger of their own,
DIALOGS_LOGGER, which is at INFO whatever the agent is started with.

Under systemd the agent's standard error is the journal, which gives every line the priority info unless the line begins with one of its
own, "<4>" (SyslogLevelPrefix, on by default). Each line the agent writes there begins with its level's priority, so that Settings › Logs
(the root helper's `journalctl -p warning`) finds the warnings and errors, a traceback's every line with them.
"""

import logging
import os
import sys
from typing import Mapping, Optional, TextIO

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# The logger of the three lines each dialog gets (what Zoom drew, what was done with it): see the Zoom provider.
DIALOGS_LOGGER = "croom.meeting.dialogs"


def priority_of(levelno: int) -> int:
    """A log level's syslog priority, as the journal keeps it: 7 debug, 6 info, 4 warning, 3 error, 2 critical."""
    if levelno <= logging.DEBUG:
        return 7
    if levelno <= logging.INFO:
        return 6
    if levelno <= logging.WARNING:
        return 4
    if levelno <= logging.ERROR:
        return 3
    return 2


class JournalFormatter(logging.Formatter):
    """LOG_FORMAT with the record's priority, "<N>", at the start of each of its lines: the journal takes each line as an entry of its own."""

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{priority_of(record.levelno)}>"
        return "\n".join(prefix + line for line in super().format(record).split("\n"))


def writes_to_journal(stream: Optional[TextIO] = None, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether `stream` (standard error) is the journal's: systemd says which of its streams it connected, as JOURNAL_STREAM=<device>:<inode>."""
    stream = sys.stderr if stream is None else stream
    environ = os.environ if environ is None else environ
    try:
        device, inode = (int(part) for part in environ.get("JOURNAL_STREAM", "").split(":"))
        status = os.fstat(stream.fileno())
    except (ValueError, OSError, AttributeError):
        return False
    return (status.st_dev, status.st_ino) == (device, inode)


def level_for(verbose: bool = False, debug: bool = False) -> int:
    """The level the agent logs at: WARNING, INFO with -v, DEBUG with --debug."""
    return logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)


def setup_logging(verbose: bool = False, debug: bool = False) -> None:
    """Set the agent's logging up as its command line asks, and make the dialogs' lines reach the journal whatever it asks."""
    level = level_for(verbose, debug)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    if writes_to_journal():
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stderr:
                handler.setFormatter(JournalFormatter(LOG_FORMAT))
    logging.getLogger(DIALOGS_LOGGER).setLevel(min(level, logging.INFO))
