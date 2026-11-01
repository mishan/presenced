"""Face check through the IR camera.

The camera's emitter lights alternate frames. Subtracting an unlit frame from
a lit one removes everything the emitter does not reach, which leaves what is
close to the screen. That alone does not say a person is there (a pillow or a
wall will do), so a face detector has the final word.

Left to itself the camera meters on the whole frame, which is mostly dark
background, so a face at arm's length comes out clipped to white and the
detector misses it. The camera's region-of-interest control moves the
metering: it takes effect when written while the stream runs, and the
exposure it settles on carries over to the next stream.
"""
import glob
import hashlib
import os
import re
import select
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass

import cv2
import numpy as np

from .config import data_home
from .sensor import Reading, Verdict

# YuNet, from OpenCV's model zoo (MIT license), pinned to a commit and checked
MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/f12e12798e8314f7c074a6656816c048dcc95b7a"
    f"/models/face_detection_yunet/{MODEL_NAME}"
)
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

GRAB_FRAMES = 12
MAX_FRAMES = 40  # two seconds at 20 fps, for the exposure to catch up with a face that just appeared
DETECT_FROM = 4  # frames before this are still adjusting to the metering region
DETECT_LAST = 8  # look for faces in this many of the last frames
CLIPPED = 0.25  # share of the metering region at full white that means "still too bright"
ROI_GROW = 1.5  # meter on a little more than the face box
MIN_STROBE = 3.0  # lit minus unlit brightness below this means the camera is blind
NEAR = 40
DETECT_FLOOR = 0.3  # report scores down to here, so thresholds can be tuned from logs
STREAM_TIMEOUT_S = 6.0  # a camera that stalls mid-stream must not stall the daemon


@dataclass
class IRReading:
    faces: int
    score: float  # confidence of the best face, 0 when there is none
    near_pct: float  # share of the frame the emitter lights up
    blind: bool  # no strobe seen: shutter closed, or the emitter is off


def greyscale_size(device):
    """Frame size of a device's greyscale (IR) format, or None if it has none."""
    listing = subprocess.run(
        ["v4l2-ctl", "-d", device, "--list-formats-ext"], capture_output=True, text=True, timeout=10
    ).stdout
    start = listing.find("'GREY'")
    if start < 0:
        return None
    size = re.search(r"Size: Discrete (\d+)x(\d+)", listing[start:])
    return (int(size[1]), int(size[2])) if size else None


def find_camera(device=""):
    """Device path and frame size of the given camera, or of the first IR one."""
    if device:
        size = greyscale_size(device)
        if size is None:
            raise LookupError(f"{device} has no greyscale (IR) format")
        return device, *size
    for candidate in sorted(glob.glob("/dev/video*")):
        size = greyscale_size(candidate)
        if size:
            return candidate, *size
    raise LookupError("no greyscale (IR) camera found")


def read_exactly(stream, size, deadline):
    """size bytes from a pipe, or fewer if it ends or the deadline passes."""
    fd = stream.fileno()
    chunks, have = [], 0
    while have < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            break
        chunk = os.read(fd, size - have)
        if not chunk:
            break
        chunks.append(chunk)
        have += len(chunk)
    return b"".join(chunks)


def model_path():
    return data_home() / MODEL_NAME


def fetch_model():
    """Download the face model if it is not already in place; its path."""
    path = model_path()
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256:
        return path
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != MODEL_SHA256:
        raise ValueError(f"face model checksum mismatch: got {digest}, expected {MODEL_SHA256}")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)
    return path


def roi_control(x, y, w, h):
    return f"region_of_interest_rectangle=({x},{y})/{w}x{h}"


class IRCamera:
    name = "camera"
    role = "look"

    def __init__(self, device="", score_threshold=0.5, min_face_width=0.08):
        self.device, self.width, self.height = find_camera(device)
        self.abort = threading.Event()  # set to cut a look short and free the camera
        self.idle = threading.Event()  # set while the camera is not streaming
        self.idle.set()
        self.gate = threading.Lock()  # makes "not aborted, so open the camera" one step
        self.score_threshold = score_threshold
        self.min_face_px = min_face_width * self.width
        model = model_path()
        if not model.exists():
            raise FileNotFoundError(f"{model} is missing; run `presencectl fetch-model`")
        self.detector = cv2.FaceDetectorYN.create(
            str(model), "", (self.width, self.height), DETECT_FLOOR
        )
        self.roi = self.default_roi()
        self.metering = subprocess.run(
            ["v4l2-ctl", "-d", self.device, "--list-ctrls"], capture_output=True, text=True, timeout=10
        ).stdout.count("region_of_interest_rectangle") > 0

    def hold(self):
        with self.gate:
            self.abort.set()

    def release(self):
        self.abort.clear()

    def describe(self):
        return self.device

    def read(self):
        seen = self.look()
        if seen.blind:
            verdict = Verdict.UNKNOWN
        else:
            verdict = Verdict.PRESENT if seen.faces else Verdict.ABSENT
        return Reading(verdict, {"faces": seen.faces, "score": seen.score, "near_pct": seen.near_pct})

    def default_roi(self):
        """Where a face at a laptop usually is."""
        return (self.width * 3 // 10, self.height // 6, self.width * 2 // 5, self.height * 3 // 5)

    def clipped(self, frame):
        x, y, w, h = self.roi
        return float((frame[y : y + h, x : x + w] >= 250).mean())

    def grab(self):
        """Consecutive frames, shaped (count, height, width), metered on self.roi.

        While the room is empty the exposure drifts up, so a face that has just
        come back starts out clipped to white. The stream then runs on, up to
        MAX_FRAMES, until the exposure has caught up.
        """
        size = self.width * self.height
        frames = []
        with self.gate:
            if self.abort.is_set():  # held off before the camera was even opened
                return np.zeros((0, self.height, self.width), np.uint8)
            self.idle.clear()
        try:
            stream = subprocess.Popen(
                [
                    "v4l2-ctl", "-d", self.device,
                    f"--set-fmt-video=width={self.width},height={self.height},pixelformat=GREY",
                    "--stream-mmap", f"--stream-count={MAX_FRAMES}", "--stream-to=-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + STREAM_TIMEOUT_S
                for n in range(MAX_FRAMES):
                    if self.abort.is_set():
                        frames = []
                        break
                    buf = read_exactly(stream.stdout, size, deadline)
                    if len(buf) < size:
                        break
                    frames.append(np.frombuffer(buf, dtype=np.uint8).reshape(self.height, self.width))
                    if n == 0 and self.metering:
                        subprocess.run(
                            ["v4l2-ctl", "-d", self.device, "--set-ctrl", roi_control(*self.roi)],
                            capture_output=True,
                            timeout=5,
                        )
                    if n + 1 >= GRAB_FRAMES:
                        brighter = max(frames[-2:], key=lambda frame: frame.mean())
                        if self.clipped(brighter) < CLIPPED:
                            break
            finally:
                stream.kill()
                stream.wait()
        finally:
            self.idle.set()
        return np.stack(frames) if frames else np.zeros((0, self.height, self.width), np.uint8)

    def meter_on(self, box):
        """Aim the exposure at a face box, grown a little and kept inside the frame."""
        x, y, w, h = box
        cx, cy = x + w / 2, y + h / 2
        w, h = min(self.width, int(w * ROI_GROW)), min(self.height, int(h * ROI_GROW))
        x = int(min(max(0, cx - w / 2), self.width - w))
        y = int(min(max(0, cy - h / 2), self.height - h))
        self.roi = (x, y, w, h)

    def look(self):
        frames = self.grab()
        if len(frames) <= DETECT_FROM + 1:
            return IRReading(0, 0.0, 0.0, True)
        means = frames.mean(axis=(1, 2))
        if means.max() - means.min() < MIN_STROBE:
            return IRReading(0, 0.0, 0.0, True)
        lit = means > (means.max() + means.min()) / 2

        # one pose or one exposure can fool the detector, so try every settled lit
        # frame against its unlit neighbor and keep the best
        best_faces, best_score, near_pct, best_box = 0, 0.0, 0.0, None
        first = max(DETECT_FROM, len(frames) - DETECT_LAST)
        for i in np.flatnonzero(lit):
            if i < first:
                continue
            neighbors = [j for j in (i - 1, i + 1) if 0 <= j < len(frames) and not lit[j]]
            if not neighbors:
                continue
            near = np.clip(frames[i].astype(np.int16) - frames[neighbors[0]], 0, 255).astype(np.uint8)
            _, faces = self.detector.detect(cv2.cvtColor(near, cv2.COLOR_GRAY2BGR))
            score = 0.0 if faces is None else float(faces[:, -1].max())
            if score >= best_score:
                best_score = score
                best_faces = 0 if faces is None else int(
                    ((faces[:, -1] >= self.score_threshold) & (faces[:, 2] >= self.min_face_px)).sum()
                )
                near_pct = 100 * float((near > NEAR).mean())
                if faces is not None:
                    best_box = faces[faces[:, -1].argmax(), :4]
        if best_faces:
            self.meter_on(best_box)
        else:
            self.roi = self.default_roi()  # the face may come back somewhere else
        return IRReading(best_faces, best_score, near_pct, False)

