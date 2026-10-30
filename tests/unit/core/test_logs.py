"""
The agent's logging: what a stock install's journal holds. The installed unit starts the agent with no -v, which logs warnings and errors only,
and the dialogs Zoom draws over the TV (spec 2026-10-13, section 5: "log and learn") must be in the journal all the same.
"""

import logging
import sys
from unittest.mock import AsyncMock

import pytest

from croom.core import agent
from croom.core.logs import DIALOGS_LOGGER, level_for

PROVIDER = "croom.meeting.providers.zoom_sdk"


def written(reached):
    """What reached the journal, as it would read: "<logger> <LEVEL> <words>"."""
    return [f"{r.name} {r.levelname} {r.getMessage()}" for r in reached]


def test_with_no_v_a_dialogs_info_line_reaches_the_journal_and_the_providers_other_info_lines_do_not(agent_logging):
    reached = agent_logging()                                                      # the installed unit: no flag
    logging.getLogger(DIALOGS_LOGGER).info("Zoom dialog: 'Recording consent'")
    logging.getLogger(PROVIDER).info("Joining Zoom meeting 99612060433 through the Meeting SDK")
    logging.getLogger(PROVIDER).debug("a debug line")
    logging.getLogger(PROVIDER).warning("The microphone was not muted at join")
    assert written(reached) == [f"{DIALOGS_LOGGER} INFO Zoom dialog: 'Recording consent'", f"{PROVIDER} WARNING The microphone was not muted at join"]


@pytest.mark.parametrize("verbose, debug, providers", [
    (True, False, ["INFO", "WARNING"]),                                            # -v: everything from INFO up, the dialogs' lines among it once
    (False, True, ["DEBUG", "INFO", "WARNING"]),                                   # --debug: everything, the dialogs' own included
])
def test_v_and_debug_show_more_and_the_dialogs_lines_are_still_there_once(agent_logging, verbose, debug, providers):
    reached = agent_logging(verbose=verbose, debug=debug)
    for level in (logging.DEBUG, logging.INFO, logging.WARNING):
        logging.getLogger(PROVIDER).log(level, "provider")
        logging.getLogger(DIALOGS_LOGGER).log(level, "dialogs")
    assert [r.levelname for r in reached if r.name == PROVIDER] == providers
    assert [r.levelname for r in reached if r.name == DIALOGS_LOGGER] == providers


def test_the_dialogs_have_the_logger_the_guides_and_the_agents_setup_name():
    assert DIALOGS_LOGGER == "croom.meeting.dialogs"


def test_the_level_the_command_line_asks_for():
    assert level_for() == logging.WARNING and level_for(verbose=True) == logging.INFO
    assert level_for(debug=True) == logging.DEBUG and level_for(verbose=True, debug=True) == logging.DEBUG


@pytest.mark.parametrize("argv, flags", [([], (False, False)), (["-v"], (True, False)), (["--verbose"], (True, False)), (["--debug"], (False, True))])
def test_the_agents_main_sets_logging_up_from_its_flags_and_runs_the_agent(monkeypatch, argv, flags):
    """The unit's command is `python -m croom.core.agent -c config.yaml`: main must set the logging up with exactly what the command line gave."""
    calls, ran = [], []
    monkeypatch.setattr(agent, "setup_logging", lambda verbose, debug: calls.append((verbose, debug)))

    async def run_agent(config_path):
        ran.append(config_path)

    monkeypatch.setattr(agent, "run_agent", run_agent)
    monkeypatch.setattr(sys, "argv", ["croom", "-c", "/etc/croom/config.yaml", *argv])
    agent.main()
    assert calls == [flags] and ran == ["/etc/croom/config.yaml"]


# Under systemd: each line begins with its priority, which the journal takes (Settings > Logs' "Warnings and errors" is journalctl -p warning)

def test_each_line_of_a_record_begins_with_its_priority_for_the_journal():
    from croom.core.logs import LOG_FORMAT, JournalFormatter, priority_of
    assert [priority_of(level) for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)] == [7, 6, 4, 3, 2]
    formatter = JournalFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord("croom.x", logging.ERROR, __file__, 1, "Traceback:\n  File x\nValueError", None, None)
    assert formatter.format(record).split("\n") == ["<3>ERROR Traceback:", "<3>  File x", "<3>ValueError"]
    assert LOG_FORMAT.startswith("%(asctime)s - ")             # the helper's journal job takes the time off the front of the line


def test_standard_error_is_the_journals_only_when_systemd_says_so(tmp_path):
    import os
    from croom.core.logs import writes_to_journal
    with open(tmp_path / "stream", "w") as stream:
        status = os.fstat(stream.fileno())
        assert writes_to_journal(stream, {"JOURNAL_STREAM": f"{status.st_dev}:{status.st_ino}"})
        assert not writes_to_journal(stream, {"JOURNAL_STREAM": f"{status.st_dev}:{status.st_ino + 1}"})   # its other stream
        assert not writes_to_journal(stream, {})
        assert not writes_to_journal(stream, {"JOURNAL_STREAM": "nonsense"})
