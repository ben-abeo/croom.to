"""Fixtures the unit tests of more than one package share."""

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
