"""
What the agent writes to its log, and at what level.

The agent logs warnings and errors and nothing more unless it is started with -v (everything at INFO) or --debug, and the installed unit
starts it with neither, so a stock install's journal holds what went wrong and little else. The dialogs Zoom draws over the TV are the one
thing the journal must always hold (spec 2026-10-13, section 5, "log and learn"): a new dialog is judged, and its phrase added to the
acknowledge list, from the journal, a handful of lines in a meeting and read weeks after it. Their lines come from one logger of their own,
DIALOGS_LOGGER, which is at INFO whatever the agent is started with.
"""

import logging

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# The logger of the three lines each dialog gets (what Zoom drew, what was done with it): see the Zoom provider.
DIALOGS_LOGGER = "croom.meeting.dialogs"


def level_for(verbose: bool = False, debug: bool = False) -> int:
    """The level the agent logs at: WARNING, INFO with -v, DEBUG with --debug."""
    return logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)


def setup_logging(verbose: bool = False, debug: bool = False) -> None:
    """Set the agent's logging up as its command line asks, and make the dialogs' lines reach the journal whatever it asks."""
    level = level_for(verbose, debug)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger(DIALOGS_LOGGER).setLevel(min(level, logging.INFO))
