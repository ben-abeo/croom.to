"""Laptop mode and the TV (spec 2026-10-27): the TV switched to the laptop's input and back, or words that say which to choose."""
from tests.unit.tv.helpers import running, service_for
from tests.unit.tv.test_keep_on import on_input


def switched(client):
    return [call for call in client.calls if call[0] == "set_input"]


async def test_the_tv_goes_to_the_laptops_input_and_back_to_the_pis(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_1"), key="key-9", laptop_input="HDMI_2")
    async with running(service) as client:
        assert service.status()["laptop_input"] == "HDMI_2"
        assert await service.show_input("laptop") is None
        assert service.status()["input"] == "HDMI_2"
        assert await service.show_input("pi") is None
        assert switched(client) == [("set_input", "HDMI_2"), ("set_input", "HDMI_1")]


async def test_a_tv_already_on_the_input_is_left_alone(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_2"), key="key-9", laptop_input="HDMI_2")
    async with running(service) as client:
        assert await service.show_input("laptop") is None
        assert switched(client) == []


async def test_a_room_with_no_laptop_input_is_told_to_choose_one(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_1"), key="key-9")
    async with running(service) as client:
        words = await service.show_input("laptop")
        assert "Options" in words and switched(client) == []


async def test_a_tv_that_is_not_paired_gets_the_input_in_words(tmp_path):
    service = service_for(tmp_path, laptop_input="HDMI_2")          # no key: not paired
    async with running(service):
        assert await service.show_input("laptop") == "Choose HDMI 2 on the TV with its Input button"


async def test_a_tv_that_refuses_the_switch_gets_the_input_in_words(tmp_path):
    def refusing(host, key):
        client = on_input("HDMI_1")(host, key)

        async def set_input(*args):
            raise ConnectionError("no")
        client.set_input = set_input
        return client
    service = service_for(tmp_path, refusing, key="key-9", laptop_input="HDMI_2")
    async with running(service):
        assert await service.show_input("laptop") == "Choose HDMI 2 on the TV with its Input button"
