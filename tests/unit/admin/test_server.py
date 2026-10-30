"""The helper's socket (spec 2026-10-29, section 5): one JSON request per connection, one JSON answer, on a socket in a temporary folder,
with the client the control service will use."""

import asyncio
import grp
import json
import logging
import os
import stat

import pytest

from croom.admin.client import AdminClient, AdminError, AdminUnavailable
from croom.admin.jobs import JobError
from croom.admin.server import MAX_REQUEST, AdminServer


def echo(args):
    return {"echo": args}


def refuse(args):
    raise JobError("Leave the meeting first")


def crash(args):
    raise KeyError("secret detail")


JOBS = {"echo": echo, "refuse": refuse, "crash": crash}


@pytest.fixture
async def socket_path(tmp_path):
    path = tmp_path / "admin.sock"
    server = AdminServer(path, JOBS)
    await server.start()
    yield path
    await server.stop()


async def raw(path, data: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(data)
    writer.write_eof()
    answer = await reader.read()
    writer.close()
    return json.loads(answer)


async def test_round_trip(socket_path):
    answer = await raw(socket_path, json.dumps({"job": "echo", "a": 1}).encode())
    assert answer == {"ok": True, "echo": {"a": 1}}


async def test_the_socket_is_for_its_owner_and_group_only(socket_path):
    assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o660


async def test_the_socket_takes_the_group_it_is_given(tmp_path):
    path = tmp_path / "grouped.sock"
    group = grp.getgrgid(os.getgid()).gr_name
    server = AdminServer(path, JOBS, group=group)
    await server.start()
    try:
        assert os.stat(path).st_gid == os.getgid()
    finally:
        await server.stop()
    assert not path.exists()


async def test_a_stale_socket_from_a_crash_is_replaced(tmp_path):
    path = tmp_path / "stale.sock"
    path.write_text("left behind")
    server = AdminServer(path, JOBS)
    await server.start()
    try:
        assert (await raw(path, b'{"job": "echo"}'))["ok"] is True
    finally:
        await server.stop()


async def test_an_unknown_job(socket_path):
    assert await raw(socket_path, b'{"job": "rm", "path": "/"}') == {"ok": False, "error": "Unknown job"}


async def test_a_jobs_own_words(socket_path):
    assert await raw(socket_path, b'{"job": "refuse"}') == {"ok": False, "error": "Leave the meeting first"}


async def test_anything_else_is_the_helper_failing_and_logged(socket_path, caplog):
    with caplog.at_level(logging.ERROR, logger="croom.admin.server"):
        answer = await raw(socket_path, b'{"job": "crash"}')
    assert answer == {"ok": False, "error": "The helper failed: KeyError"}
    assert "secret detail" in caplog.text


@pytest.mark.parametrize("data", [b"not json", b"[1, 2]", b'{"no": "job"}', b'{"job": 7}', b"\xff\xfe"])
async def test_a_request_it_cannot_read(socket_path, data):
    answer = await raw(socket_path, data)
    assert answer["ok"] is False and "could not read" in answer["error"]


async def test_a_request_too_large_is_refused(socket_path):
    answer = await raw(socket_path, b'{"job": "echo", "x": "' + b"a" * MAX_REQUEST + b'"}')
    assert answer == {"ok": False, "error": "The request is too large"}


# --- the client


async def test_the_client_calls_a_job(socket_path):
    client = AdminClient(str(socket_path), timeout=5)
    assert client.available()
    assert await client.call("echo", yaml="room: {}") == {"ok": True, "echo": {"yaml": "room: {}"}}


async def test_the_client_raises_the_jobs_words(socket_path):
    client = AdminClient(str(socket_path), timeout=5)
    with pytest.raises(AdminError, match="Leave the meeting first"):
        await client.call("refuse")


async def test_no_helper_is_unavailable(tmp_path):
    client = AdminClient(str(tmp_path / "nothing.sock"), timeout=5)
    assert not client.available()
    with pytest.raises(AdminUnavailable, match="Not available on this device"):
        await client.call("version")


async def test_a_helper_that_does_not_answer_is_unavailable(tmp_path):
    path = tmp_path / "silent.sock"

    async def silent(reader, writer):
        await asyncio.sleep(10)

    server = await asyncio.start_unix_server(silent, path=str(path))
    try:
        with pytest.raises(AdminUnavailable):
            await AdminClient(str(path), timeout=0.2).call("version")
    finally:
        server.close()


async def test_jobs_run_off_the_event_loop(tmp_path):
    """A slow job (a write, a systemctl) does not stop the helper answering another request meanwhile."""
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    release = asyncio.Event()

    def slow(args):
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(5)
        return {"slow": True}

    path = tmp_path / "slow.sock"
    server = AdminServer(path, {"slow": slow, "echo": echo})
    await server.start()
    try:
        client = AdminClient(str(path), timeout=5)
        pending = asyncio.ensure_future(client.call("slow"))
        await asyncio.wait_for(started.wait(), 5)
        assert (await client.call("echo"))["ok"] is True
        release.set()
        assert await pending == {"ok": True, "slow": True}
    finally:
        await server.stop()
