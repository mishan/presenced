"""Ultrasonic motion sensing through the laptop's own speaker and microphones.

A short, inaudible tone is played while the microphones record. The tone that
comes back is the sum of the direct path and every echo in the room, so its
amplitude and phase hold still in an empty room and wander when something in
front of the laptop moves.
"""
import json
import logging
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("presenced")

RATE = 48000
CHANNELS = 2
BLOCK_S = 0.05  # baseband resolution: 20 Hz either side of the tone
FADE_S = 0.1  # ramp the tone in and out so the speaker does not click
LEAD_S = 0.3  # recorder head start, and tail after the tone ends
TONE_GAIN = 0.5
MIN_LEVEL_DB = -50.0  # below this the speaker is muted or turned right down
MIN_BLOCKS = 10
STREAM = ["--raw", "--rate", str(RATE), "--channels", str(CHANNELS), "--format", "s16"]

# If the speaker goes away, PipeWire would otherwise send the tone to whatever
# is the default output, which may be a headset.
PLAYBACK_PROPERTIES = "{ node.dont-fallback = true, node.dont-reconnect = true }"


@dataclass
class SonarReading:
    level_db: float  # strength of the received tone
    motion_db: float  # fluctuation of the tone relative to its strength

    @property
    def usable(self):
        return self.level_db > MIN_LEVEL_DB


UNUSABLE = SonarReading(-120.0, 0.0)


def find_node(media_class, hint):
    """Name of the PipeWire node of a class that is, or matches, hint; None if absent."""
    dump = subprocess.run(["pw-dump"], capture_output=True, check=True).stdout
    candidates = []
    for obj in json.loads(dump):
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") == media_class and "node.name" in props:
            candidates.append(props)
    for props in candidates:
        if props["node.name"] == hint:
            return hint
    for props in candidates:
        text = f"{props['node.name']} {props.get('node.description', '')}"
        if hint.lower() in text.lower():
            return props["node.name"]
    return None


def to_pcm(mono):
    """Interleaved 16-bit samples, the same on every channel."""
    pcm = (mono * 32767).astype("<i2")
    return np.repeat(pcm[:, None], CHANNELS, axis=1).tobytes()


def make_tone(tone_hz, seconds):
    """A faded sine."""
    n = int(RATE * seconds)
    tone = TONE_GAIN * np.sin(2 * np.pi * tone_hz * np.arange(n) / RATE)
    ramp = int(RATE * FADE_S)
    tone[:ramp] *= np.linspace(0, 1, ramp)
    tone[-ramp:] *= np.linspace(1, 0, ramp)
    return to_pcm(tone)


def make_beeps(count, long=False):
    """Audible 1 kHz beeps, used to cue a person during calibration."""
    n = int((0.8 if long else 0.15) * RATE)
    beep = 0.2 * np.sin(2 * np.pi * 1000 * np.arange(n) / RATE) * np.hanning(n)
    gap = np.zeros(int(0.15 * RATE))
    return to_pcm(np.concatenate([part for _ in range(count) for part in (beep, gap)]))


def analyze(samples, tone_hz):
    """Reduce a recording of one burst, shaped (frames, channels), to a reading."""
    # the microphones sit a wavelength or so apart, so their tones can arrive
    # in antiphase: demodulate each channel alone rather than summing them
    block = int(RATE * BLOCK_S)
    blocks = len(samples) // block
    if blocks == 0:
        return UNUSABLE
    t = np.arange(blocks * block) / RATE
    mixed = samples[: blocks * block] * np.exp(-2j * np.pi * tone_hz * t)[:, None]
    base = mixed.reshape(blocks, block, -1).mean(axis=1)

    # keep the steady part of the burst: drop the silence around it and the ramps;
    # the burst fills most of the recording, so its level is the upper quartile,
    # where the maximum would instead follow a key click or a knock on the case
    magnitude = np.abs(base).mean(axis=1)
    on = np.flatnonzero(magnitude > 0.5 * np.percentile(magnitude, 75))
    trim = int(round(FADE_S / BLOCK_S)) + 1
    steady = base[on[0] + trim : on[-1] + 1 - trim]
    if len(steady) < MIN_BLOCKS:
        return UNUSABLE

    level = np.abs(steady).mean() + 1e-12
    motion = np.sqrt((np.abs(steady - steady.mean(axis=0)) ** 2).mean()) + 1e-12
    return SonarReading(float(20 * np.log10(2 * level)), float(20 * np.log10(motion / level)))


class Sonar:
    def __init__(self, tone_hz=20000.0, burst_s=1.4, speaker="speaker", microphone="digital microphone"):
        self.tone_hz = tone_hz
        self.speaker = speaker
        self.microphone = microphone
        self.tone = make_tone(tone_hz, burst_s)
        self.found = (None, None)  # the node names the hints last matched

    def nodes(self):
        """Current speaker and microphone node names; either is None if missing."""
        self.found = find_node("Audio/Sink", self.speaker), find_node("Audio/Source", self.microphone)
        return self.found

    def play(self, pcm, sink=None):
        """Play samples on the speaker only; False if it is not there."""
        sink = sink or self.nodes()[0]
        if sink is None:
            return False
        result = subprocess.run(
            ["pw-play", *STREAM, "--target", sink, "-P", PLAYBACK_PROPERTIES, "-"],
            input=pcm,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def probe(self):
        """Play one burst and return what came back."""
        # look the nodes up every time: speakers come and go with docks and jacks
        sink, source = self.nodes()
        if sink is None or source is None:
            log.debug("sonar: speaker or microphone missing")
            return UNUSABLE
        with tempfile.TemporaryFile() as recording:
            recorder = subprocess.Popen(
                ["pw-record", *STREAM, "--target", source, "-"],
                stdout=recording,
                stderr=subprocess.DEVNULL,
            )
            try:
                time.sleep(LEAD_S)
                played = self.play(self.tone, sink)
                time.sleep(LEAD_S)
            finally:
                recorder.send_signal(signal.SIGINT)
                recorder.wait()
            if not played:
                return UNUSABLE
            recording.seek(0)
            raw = recording.read()
        pcm = np.frombuffer(raw[: len(raw) // (2 * CHANNELS) * 2 * CHANNELS], dtype="<i2")
        return analyze(pcm.reshape(-1, CHANNELS) / 32768.0, self.tone_hz)


class NoSonar:
    """Stands in when the sonar is turned off."""

    speaker = microphone = "disabled"
    found = ("disabled", "disabled")

    def nodes(self):
        return self.found

    def probe(self):
        return UNUSABLE
