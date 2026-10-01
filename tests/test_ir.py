import os
import subprocess
import time

import numpy as np
import pytest

from presenced import ir

LISTING = """ioctl: VIDIOC_ENUM_FMT
\t[0]: 'GREY' (8-bit Greyscale)
\t\tSize: Discrete 640x360
"""


def fake_listing(listings):
    def run(args, **kwargs):
        device = args[args.index("-d") + 1]
        return subprocess.CompletedProcess(args, 0, stdout=listings.get(device, ""))
    return run


def test_a_configured_camera_is_used_with_its_own_size(monkeypatch):
    big = LISTING.replace("640x360", "1280x720")
    monkeypatch.setattr(ir.subprocess, "run", fake_listing({"/dev/video0": LISTING, "/dev/video4": big}))
    assert ir.find_camera("/dev/video4") == ("/dev/video4", 1280, 720)


def test_a_configured_camera_without_ir_is_an_error(monkeypatch):
    monkeypatch.setattr(ir.subprocess, "run", fake_listing({"/dev/video0": LISTING}))
    with pytest.raises(LookupError, match="video9"):
        ir.find_camera("/dev/video9")


def test_no_ir_camera_falls_back_to_sonar_alone(monkeypatch):
    from presenced.config import Camera
    from presenced.service import make_camera

    monkeypatch.setattr(ir.glob, "glob", lambda pattern: [])
    assert isinstance(make_camera(Camera()), ir.NoCamera)


def test_a_stalled_camera_does_not_block_forever():
    read_end, write_end = os.pipe()
    try:
        with os.fdopen(read_end, "rb", buffering=0) as stream:
            started = time.monotonic()
            assert ir.read_exactly(stream, 1000, time.monotonic() + 0.2) == b""
            assert time.monotonic() - started < 1.0
    finally:
        os.close(write_end)


class FakeDetector:
    def __init__(self, faces):
        self.faces = faces

    def detect(self, image):
        return 1, self.faces


def camera(frames, faces):
    cam = object.__new__(ir.IRCamera)
    cam.width, cam.height = 640, 360
    cam.score_threshold = 0.5
    cam.min_face_px = 0.08 * 640
    cam.roi = (0, 0, 10, 10)
    cam.default_roi = lambda: (192, 60, 256, 216)
    cam.detector = FakeDetector(faces)
    cam.grab = lambda: frames
    return cam


def strobed(count=12, lit=90, unlit=10):
    return np.stack([np.full((360, 640), lit if i % 2 else unlit, np.uint8) for i in range(count)])


def test_a_close_face_counts_and_the_exposure_follows_it():
    face = np.array([[300, 100, 120, 140] + [0] * 10 + [0.9]], np.float32)
    cam = camera(strobed(), face)
    seen = cam.look()
    assert (seen.faces, round(seen.score, 2), seen.blind) == (1, 0.9, False)
    assert cam.roi != (192, 60, 256, 216)


def test_a_face_across_the_room_does_not_count():
    face = np.array([[500, 100, 30, 36] + [0] * 10 + [0.9]], np.float32)
    cam = camera(strobed(), face)
    seen = cam.look()
    assert seen.faces == 0
    assert cam.roi == (192, 60, 256, 216)  # back to metering where a face would be


def test_no_strobe_means_blind():
    cam = camera(strobed(lit=10, unlit=10), None)
    assert cam.look().blind


def test_a_cut_short_look_gives_up_the_camera():
    cam = camera(np.zeros((0, 360, 640), np.uint8), None)
    assert cam.look().blind


def test_a_held_camera_is_not_even_opened(monkeypatch):
    import threading

    cam = object.__new__(ir.IRCamera)
    cam.width, cam.height = 640, 360
    cam.abort, cam.idle, cam.gate = threading.Event(), threading.Event(), threading.Lock()
    cam.idle.set()
    cam.abort.set()

    def no_popen(*args, **kwargs):
        raise AssertionError("the camera was opened")

    monkeypatch.setattr(ir.subprocess, "Popen", no_popen)
    assert len(ir.IRCamera.grab(cam)) == 0
    assert cam.idle.is_set()


def test_a_corrupt_model_falls_back_to_sonar_alone(monkeypatch):
    import cv2

    from presenced.config import Camera
    from presenced import service

    def corrupt(*args, **kwargs):
        raise cv2.error("failed to parse the model")

    monkeypatch.setattr(service, "IRCamera", corrupt)
    assert isinstance(service.make_camera(Camera()), ir.NoCamera)
