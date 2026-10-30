"""The Settings PIN (spec 2026-10-29, section 2): kept hashed, 4 to 8 digits, unlocked for 5 minutes after the last request that used the
token, Lock ends one, and 5 wrong tries lock unlocking for 10 minutes. The clock is a stand-in the tests move by hand."""

import json
import os
import stat

import pytest

from croom.settings.pin import LOCKOUT_SECONDS, MAX_TRIES, UNLOCK_SECONDS, PinError, PinLocked, PinStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return PinStore(tmp_path / "settings-pin.json", clock=clock)


def test_a_new_store_has_no_pin_and_writes_nothing(store, tmp_path):
    assert store.is_set() is False
    assert not (tmp_path / "settings-pin.json").exists()


def test_the_file_holds_a_salted_hash_and_no_digits_of_the_pin(store, tmp_path):
    store.set_pin("73914628")
    path = tmp_path / "settings-pin.json"
    text = path.read_text(encoding="utf-8")
    assert "73914628" not in text
    saved = json.loads(text)
    assert saved["salt"] and saved["hash"] and saved["kdf"] in ("scrypt", "pbkdf2_sha256")
    assert "73914628" not in json.dumps(list(saved.values()))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.is_set() is True


def test_two_stores_of_the_same_pin_hash_it_differently(tmp_path, clock):
    first, second = PinStore(tmp_path / "a.json", clock=clock), PinStore(tmp_path / "b.json", clock=clock)
    first.set_pin("2468")
    second.set_pin("2468")
    a, b = (json.loads((tmp_path / name).read_text()) for name in ("a.json", "b.json"))
    assert a["salt"] != b["salt"] and a["hash"] != b["hash"]


@pytest.mark.parametrize("pin", ["123", "123456789", "12a4", "", " 1234", "１２３４", 1234, None, "12.4"])
def test_a_pin_is_four_to_eight_plain_digits(store, pin):
    with pytest.raises(PinError) as refused:
        store.set_pin(pin)
    assert str(refused.value) == "The PIN is 4 to 8 digits"
    assert store.is_set() is False


@pytest.mark.parametrize("pin", ["1234", "00000000"])
def test_four_and_eight_digits_are_taken(store, pin):
    store.set_pin(pin)
    assert store.check(store.unlock(pin))


def test_a_pin_is_chosen_once_only(store):
    store.set_pin("1234")
    with pytest.raises(PinError) as refused:
        store.set_pin("5678")
    assert "already" in str(refused.value) and refused.value.status == 409
    assert store.check(store.unlock("1234"))


def test_unlocking_before_a_pin_is_chosen_says_to_choose_one(store):
    with pytest.raises(PinError) as refused:
        store.unlock("1234")
    assert str(refused.value) == "Choose a PIN first" and refused.value.status == 409


def test_the_right_pin_gives_a_token_that_checks(store):
    store.set_pin("1234")
    token = store.unlock("1234")
    assert isinstance(token, str) and len(token) >= 32
    assert store.check(token) is True
    assert store.check("not-a-token") is False
    assert store.check("") is False and store.check(None) is False


def test_each_unlock_gives_its_own_token(store):
    store.set_pin("1234")
    first, second = store.unlock("1234"), store.unlock("1234")
    assert first != second and store.check(first) and store.check(second)


def test_a_wrong_pin_says_so_with_the_tries_left(store):
    store.set_pin("1234")
    with pytest.raises(PinError) as refused:
        store.unlock("4321")
    assert not isinstance(refused.value, PinLocked)
    assert str(refused.value) == "Wrong PIN: 4 tries left" and refused.value.status == 403
    with pytest.raises(PinError) as refused:
        store.unlock("4321")
    assert str(refused.value) == "Wrong PIN: 3 tries left"


def test_five_wrong_tries_lock_unlocking_for_ten_minutes(store, clock):
    store.set_pin("1234")
    assert MAX_TRIES == 5 and LOCKOUT_SECONDS == 600
    for _ in range(4):
        with pytest.raises(PinError):
            store.unlock("0000")
    with pytest.raises(PinLocked) as locked:
        store.unlock("0000")
    assert locked.value.minutes == 10 and locked.value.status == 423
    assert str(locked.value) == "Too many wrong PINs: try again in 10 minutes"
    assert store.locked_minutes() == 10
    with pytest.raises(PinLocked):                       # the right PIN too, while it lasts
        store.unlock("1234")
    clock.advance(9 * 60 + 1)
    with pytest.raises(PinLocked) as locked:
        store.unlock("1234")
    assert locked.value.minutes == 1 and str(locked.value) == "Too many wrong PINs: try again in 1 minute"
    clock.advance(60)
    assert store.locked_minutes() == 0
    assert store.check(store.unlock("1234"))


def test_after_a_lock_out_the_tries_start_again_at_five(store, clock):
    store.set_pin("1234")
    for _ in range(5):
        with pytest.raises(PinError):
            store.unlock("0000")
    clock.advance(LOCKOUT_SECONDS)
    with pytest.raises(PinError) as refused:
        store.unlock("0000")
    assert str(refused.value) == "Wrong PIN: 4 tries left"


def test_the_right_pin_starts_the_tries_again(store):
    store.set_pin("1234")
    for _ in range(4):
        with pytest.raises(PinError):
            store.unlock("0000")
    store.unlock("1234")
    with pytest.raises(PinError) as refused:
        store.unlock("0000")
    assert str(refused.value) == "Wrong PIN: 4 tries left"


def test_a_pin_that_is_not_digits_at_unlock_is_refused_without_counting_as_a_try(store):
    store.set_pin("1234")
    with pytest.raises(PinError) as refused:
        store.unlock("abcd")
    assert str(refused.value) == "The PIN is 4 to 8 digits" and refused.value.status == 400
    with pytest.raises(PinError) as refused:
        store.unlock("0000")
    assert str(refused.value) == "Wrong PIN: 4 tries left"


def test_a_token_lasts_five_minutes_after_the_last_check_that_used_it(store, clock):
    store.set_pin("1234")
    token = store.unlock("1234")
    assert UNLOCK_SECONDS == 300
    clock.advance(299)
    assert store.check(token)                            # a request that used it: five more minutes from here
    clock.advance(299)
    assert store.check(token)
    clock.advance(300)
    assert store.check(token) is False
    clock.advance(-200)                                  # once gone, gone: a clock that steps back does not bring it back
    assert store.check(token) is False


def test_lock_ends_one_token_and_not_the_others(store):
    store.set_pin("1234")
    mine, theirs = store.unlock("1234"), store.unlock("1234")
    store.lock(mine)
    assert store.check(mine) is False and store.check(theirs) is True
    store.lock(mine)                                     # twice, or a token that was never given: nothing to do
    store.lock("never-given")


def test_change_needs_the_old_pin_and_a_good_new_one(store, tmp_path):
    store.set_pin("1234")
    with pytest.raises(PinError) as refused:
        store.change("9999", "5678")
    assert str(refused.value) == "Wrong PIN: 4 tries left"
    with pytest.raises(PinError) as refused:
        store.change("1234", "56")
    assert str(refused.value) == "The PIN is 4 to 8 digits"
    store.change("1234", "567890")
    with pytest.raises(PinError):
        store.unlock("1234")
    assert store.check(store.unlock("567890"))
    assert "567890" not in (tmp_path / "settings-pin.json").read_text()


def test_change_counts_wrong_tries_and_is_locked_out_with_unlocking(store):
    store.set_pin("1234")
    for _ in range(4):
        with pytest.raises(PinError):
            store.change("0000", "5678")
    with pytest.raises(PinLocked):
        store.change("0000", "5678")
    with pytest.raises(PinLocked):
        store.change("1234", "5678")


def test_the_pin_file_is_read_again_so_deleting_it_resets_the_pin(store, tmp_path):
    """A forgotten PIN is reset at the TV Pi by deleting settings-pin.json (spec section 8): no restart is needed for that."""
    store.set_pin("1234")
    (tmp_path / "settings-pin.json").unlink()
    assert store.is_set() is False
    store.set_pin("8888")
    assert store.check(store.unlock("8888"))


def test_a_pin_file_that_cannot_be_read_is_a_pin_that_is_set_and_says_how_to_reset_it(store, tmp_path):
    """A broken file must not open Settings to whoever chooses a PIN next."""
    (tmp_path / "settings-pin.json").write_text("{not json", encoding="utf-8")
    assert store.is_set() is True
    with pytest.raises(PinError) as refused:
        store.set_pin("1234")
    assert refused.value.status == 409
    with pytest.raises(PinError) as refused:
        store.unlock("1234")
    assert "settings-pin.json" in str(refused.value)


def test_a_store_in_a_folder_that_does_not_exist_yet_makes_it(tmp_path, clock):
    store = PinStore(tmp_path / "data" / "settings-pin.json", clock=clock)
    store.set_pin("1234")
    assert (tmp_path / "data" / "settings-pin.json").is_file()


def test_a_change_ends_every_other_token_and_keeps_the_callers(store):
    """A PIN changed after a suspected leak must lock out whoever held a token for the old one: tokens slide, so one could otherwise be kept
    alive for good."""
    store.set_pin("1234")
    mine, theirs = store.unlock("1234"), store.unlock("1234")
    store.change("1234", "5678", keep=mine)
    assert store.check(theirs) is False
    assert store.check(mine) is True


def test_a_change_with_no_token_to_keep_ends_them_all(store):
    store.set_pin("1234")
    token = store.unlock("1234")
    store.change("1234", "5678")
    assert store.check(token) is False


def test_a_refused_change_ends_no_token(store):
    store.set_pin("1234")
    mine, theirs = store.unlock("1234"), store.unlock("1234")
    with pytest.raises(PinError):
        store.change("9999", "5678", keep=mine)
    assert store.check(theirs) and store.check(mine)


def test_a_pin_reset_at_the_tv_pi_ends_the_old_tokens_and_the_lock_out(store, tmp_path, clock):
    """The spec's reset is deleting settings-pin.json: what was unlocked with the old PIN, and a lock-out earned against it, go with it."""
    store.set_pin("1234")
    token = store.unlock("1234")
    for _ in range(5):
        with pytest.raises(PinError):
            store.unlock("0000")
    (tmp_path / "settings-pin.json").unlink()
    assert store.check(token) is False                   # no PIN, no Settings: not even before a new one is chosen
    store.set_pin("8888")
    assert store.locked_minutes() == 0
    assert store.check(store.unlock("8888"))
    assert store.check(token) is False


def test_choosing_a_pin_starts_the_wrong_tries_again(store, tmp_path):
    store.set_pin("1234")
    for _ in range(4):
        with pytest.raises(PinError):
            store.unlock("0000")
    (tmp_path / "settings-pin.json").unlink()
    store.set_pin("8888")
    with pytest.raises(PinError) as refused:
        store.unlock("0000")
    assert str(refused.value) == "Wrong PIN: 4 tries left"
