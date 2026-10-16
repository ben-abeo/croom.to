"""Fixtures the unit tests of more than one package share."""

import logging
import time

import pytest


@pytest.fixture
def device_zone(monkeypatch):
    """A function that sets the device's clock to a time zone (a POSIX string, which needs no tz database) for the rest of the test;
    the zone is put back afterwards. "The device's local day" is a test only where local time is not UTC."""
    def set_zone(zone):
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield set_zone
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def central_time(device_zone):
    """The device's clock set to US Central time (daylight saving from the second Sunday of March to the first of November), whatever
    zone the machine running the tests keeps."""
    device_zone("CST6CDT,M3.2.0,M11.1.0")


@pytest.fixture
def agent_logging():
    """A function that sets logging up as the agent's command line does (`agent_logging()` is an agent started with no flag, as the installed unit
    starts it; `agent_logging(verbose=True)` is -v) and gives back the list of log records that reach the root logger's handlers from then on.
    The root logger and the dialogs' logger are put back as they were; the handlers pytest keeps on the root are left where they are."""
    from croom.core.logs import DIALOGS_LOGGER, setup_logging

    root, dialogs = logging.getLogger(), logging.getLogger(DIALOGS_LOGGER)
    level, dialogs_level, others = root.level, dialogs.level, root.handlers[:]
    mine = []

    class Collect(logging.Handler):
        def __init__(self, records):
            super().__init__()
            self.records = records

        def emit(self, record):
            self.records.append(record)

    def start(verbose=False, debug=False):
        reached = []
        root.handlers[:] = []             # basicConfig does nothing for a root that has a handler, and pytest has put its own on this one
        dialogs.setLevel(logging.NOTSET)
        setup_logging(verbose, debug)
        mine.extend(root.handlers)        # the stream handler basicConfig added
        collect = Collect(reached)
        mine.append(collect)
        root.handlers[:] = others + mine
        return reached

    try:
        yield start
    finally:
        for handler in mine:
            root.removeHandler(handler)
        root.setLevel(level)
        dialogs.setLevel(dialogs_level)
