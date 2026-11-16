"""The people in a picture (devices/people.py): the AI HAT's bodies and faces as a head each, and YuNet when there is no HAT."""
import pytest

from croom.devices import framing, people

SIZE = (1280, 720)


def picture():
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    ok, jpeg = cv2.imencode(".jpg", numpy.full((720, 1280, 3), 128, numpy.uint8))
    assert ok
    return jpeg.tobytes()


def test_a_face_is_its_bodys_head_and_a_body_without_one_gets_a_head():
    facing = (100, 100, 200, 300)
    away = (600, 200, 200, 300)
    face = (180, 120, 40, 50)
    heads = people.heads([facing, away], [face])
    assert heads[0] == face and len(heads) == 2
    x, y, w, h = heads[1]
    assert x + w / 2 == 700 and w == 40 and h == pytest.approx(52) and 200 < y < 220     # across the body's middle, at its top
    assert people.heads([], [face]) == [face]                  # a face with no body found is a person still


def test_the_hats_boxes_come_back_in_the_pictures_own_pixels_and_weak_ones_are_dropped():
    seen = {}

    def infer(square):
        seen["shape"], seen["bar"], seen["picture"] = square.shape, tuple(square[0, 0]), tuple(square[320, 320])
        # the square is the 1280 x 720 picture at half size between two 140 px bars: rows are ymin, xmin, ymax, xmax, score
        return [[[0.21875, 0.0, 0.78125, 0.5, 0.9], [0.3, 0.3, 0.4, 0.4, 0.3]], [[0.5, 0.25, 0.625, 0.3125, 0.8]]]

    bodies, faces, size = people.find(picture(), infer)
    assert size == SIZE and seen["shape"] == (640, 640, 3) and seen["bar"] == (114, 114, 114) and seen["picture"] != seen["bar"]
    assert bodies == [pytest.approx((0, 0, 640, 720))] and faces == [pytest.approx((320, 360, 80, 160))]
    assert people.find(b"not a picture", infer) == ([], [], (0, 0))


def test_a_room_without_the_hat_looks_with_yunet(monkeypatch):
    monkeypatch.setattr(people, "available", lambda: False)
    monkeypatch.setattr(framing, "detect_faces", lambda jpeg: ([(1, 2, 3, 4)], SIZE))
    assert people.detect(b"jpeg") == ([(1, 2, 3, 4)], SIZE) and people.count(b"jpeg") == 1


def test_a_hat_that_fails_is_said_once_and_yunet_looks_instead(monkeypatch, caplog):
    def broken(jpeg):
        raise OSError("no device")

    monkeypatch.setattr(people, "available", lambda: True)
    monkeypatch.setattr(people, "find", broken)
    monkeypatch.setattr(people, "_said", None)
    monkeypatch.setattr(framing, "detect_faces", lambda jpeg: ([], SIZE))
    assert people.detect(b"jpeg") == ([], SIZE) and people.detect(b"jpeg") == ([], SIZE)
    assert [r.message for r in caplog.records].count(
        "The AI HAT did not look for people (OSError: no device): looking on the CPU instead") == 1


def test_the_hat_is_looked_with_when_it_is_there(monkeypatch):
    monkeypatch.setattr(people, "available", lambda: True)
    monkeypatch.setattr(people, "find", lambda jpeg: ([(600, 200, 200, 300)], [], SIZE))
    heads, size = people.detect(b"jpeg")
    assert len(heads) == 1 and size == SIZE
