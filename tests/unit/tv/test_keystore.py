import os
import stat

from croom.tv.keystore import KeyStore


def test_a_missing_or_blank_key_file_reads_as_no_key(tmp_path):
    store = KeyStore(tmp_path / "tv-client-key")
    assert store.read() is None
    (tmp_path / "tv-client-key").write_text("  \n")
    assert store.read() is None


def test_the_key_file_is_private_even_over_one_that_was_not(tmp_path):
    path = tmp_path / "state" / "tv-client-key"           # the folder does not exist yet
    store = KeyStore(path)
    store.write("key-1")
    assert path.read_text() == "key-1" and stat.S_IMODE(path.stat().st_mode) == 0o600
    os.chmod(path, 0o644)
    store.write("key-2")
    assert path.read_text() == "key-2" and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_key_file_is_created_private_not_made_private_afterwards(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    previous = os.umask(0)                                # the most permissive creation mode there is
    try:
        KeyStore(tmp_path / "tv-client-key").write("key-1")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "tv-client-key").stat().st_mode) == 0o600


def test_clear_removes_the_key_and_is_quiet_when_there_is_none(tmp_path):
    store = KeyStore(tmp_path / "tv-client-key")
    store.write("key-1")
    store.clear()
    store.clear()
    assert store.read() is None
