import os
import stat

import pytest
from croom.tv.keystore import KeyStore


def test_a_missing_or_blank_key_file_reads_as_no_key(tmp_path):
    store = KeyStore(tmp_path / "tv-client-key")
    assert store.read() is None
    (tmp_path / "tv-client-key").write_text("  \n")
    assert store.read() is None


def test_the_key_is_read_without_the_newline_an_editor_adds(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9\n")
    assert KeyStore(tmp_path / "tv-client-key").read() == "key-9"


@pytest.mark.parametrize("make", ["a folder", "bytes that are not text", "a file nobody may read"])
def test_a_key_file_that_cannot_be_read_reads_as_no_key(tmp_path, make):
    path = tmp_path / "tv-client-key"
    if make == "a folder":
        path.mkdir()
    elif make == "bytes that are not text":
        path.write_bytes(b"\xff\xfe\x00key")
    else:
        if os.geteuid() == 0:
            pytest.skip("root can read anything")
        path.write_text("key-9")
        path.chmod(0)
    assert KeyStore(path).read() is None


def test_the_key_file_is_private_even_over_one_that_was_not(tmp_path):
    path = tmp_path / "state" / "tv-client-key"           # the folder does not exist yet
    store = KeyStore(path)
    store.write("key-1")
    assert path.read_text() == "key-1" and stat.S_IMODE(path.stat().st_mode) == 0o600
    os.chmod(path, 0o644)
    store.write("key-2")
    assert path.read_text() == "key-2" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert os.listdir(path.parent) == ["tv-client-key"]   # no temporary file is left behind


def test_the_key_file_is_created_private_not_made_private_afterwards(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "fchmod", lambda *args, **kwargs: None)
    previous = os.umask(0)                                # the most permissive creation mode there is
    try:
        KeyStore(tmp_path / "tv-client-key").write("key-1")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "tv-client-key").stat().st_mode) == 0o600


def test_a_temporary_file_a_crash_left_behind_does_not_get_a_looser_mode_than_the_key(tmp_path):
    leftover = tmp_path / "tv-client-key.tmp"
    leftover.write_text("half a key")
    leftover.chmod(0o666)
    KeyStore(tmp_path / "tv-client-key").write("key-1")
    assert stat.S_IMODE((tmp_path / "tv-client-key").stat().st_mode) == 0o600
    assert (tmp_path / "tv-client-key").read_text() == "key-1" and os.listdir(tmp_path) == ["tv-client-key"]


@pytest.mark.parametrize("breaks", ["fsync", "replace"])
def test_a_write_that_fails_leaves_the_old_key_whole_and_no_litter(tmp_path, monkeypatch, breaks):
    """The key is written whole to a file of its own and renamed into place: when the write fails, or the power goes
    before the rename, the key that was there is still there, and never half of a new one."""
    path = tmp_path / "tv-client-key"
    store = KeyStore(path)
    store.write("old-key")

    def fail(*args, **kwargs):
        raise OSError("the disk is full")
    monkeypatch.setattr(os, breaks, fail)
    with pytest.raises(OSError):
        store.write("new-key")
    monkeypatch.undo()
    assert path.read_text() == "old-key" and os.listdir(tmp_path) == ["tv-client-key"]


def test_clear_removes_the_key_and_is_quiet_when_there_is_none(tmp_path):
    store = KeyStore(tmp_path / "tv-client-key")
    store.write("key-1")
    store.clear()
    store.clear()
    assert store.read() is None


def test_clear_also_removes_a_temporary_file_a_crash_left_behind(tmp_path):
    """That file holds a key too: forgetting the TV must not leave one in the data dir."""
    (tmp_path / "tv-client-key").write_text("key-1")
    (tmp_path / "tv-client-key.tmp").write_text("key-2")
    KeyStore(tmp_path / "tv-client-key").clear()
    assert os.listdir(tmp_path) == []
