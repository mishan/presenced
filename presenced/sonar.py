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
import threading
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
PLAY_SLACK_S = 5.0  # pw-play gets this long beyond the tone itself before it is killed
STOP_TIMEOUT_S = 2.0  # and pw-record this long to stop when asked
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


def pipewire_objects():
    """Everything PipeWire knows about, or an empty list if it cannot be asked."""
    try:
        dump = subprocess.run(["pw-dump"], capture_output=True, check=True, timeout=10).stdout
        return json.loads(dump)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        log.debug("pw-dump failed: %s", error)
        return []


def active_routes(objects):
    """{(device id, direction, profile device index): name} of every route in use."""
    routes = {}
    for obj in objects:
        info = obj.get("info") or {}
        for route in (info.get("params") or {}).get("Route", []):
            # an active route names the one profile device it is playing on
            indexes = [route["device"]] if "device" in route else route.get("devices", [])
            for index in indexes:
                routes[(obj.get("id"), route.get("direction"), index)] = (
                    f"{route.get('name', '')} {route.get('description', '')}"
                )
    return routes


INTERNAL_BUSES = ("pci", "platform")  # built-in sound; not usb or bluetooth
NOT_A_SPEAKER = ("headphone", "headset", "hdmi", "displayport", "line", "usb", "bluetooth")


def output_route(props, routes):
    """The route a sink is playing through now, or None if PipeWire does not say."""
    return routes.get((props.get("device.id"), "Output", props.get("card.profile.device")))


def is_laptop_speaker(props, routes):
    """Whether a sink is a built-in speaker, and not headphones, HDMI, USB or Bluetooth.

    The route a sink is playing through decides: one output can serve both the
    speaker and the headphone jack ("Speaker + Headphones"), and only the
    route says which is in use. Only where PipeWire reports no route does the
    sink's own name have to do.
    """
    if props.get("device.api") != "alsa" or props.get("device.bus") not in INTERNAL_BUSES:
        return False
    route = output_route(props, routes)
    text = (route or f"{props['node.name']} {props.get('node.description', '')}").lower()
    return "speaker" in text and not any(word in text for word in NOT_A_SPEAKER)


def find_node(media_class, hint, objects=None):
    """Name of the PipeWire node of a class that is, or matches, hint; None if absent.

    Speakers must also be the laptop's own: the tone is never sent anywhere
    else, whatever the hint says. The hint is matched against the route a
    sink plays through as well as its name, since on many laptops only the
    route mentions the speaker ("Built-in Audio Analog Stereo", playing
    through "Speakers").
    """
    objects = pipewire_objects() if objects is None else objects
    candidates = [
        props
        for props in ((obj.get("info") or {}).get("props") or {} for obj in objects)
        if props.get("media.class") == media_class and "node.name" in props
    ]
    routes = {}
    if media_class == "Audio/Sink":
        routes = active_routes(objects)
        candidates = [props for props in candidates if is_laptop_speaker(props, routes)]

    def described(props):
        return f"{props['node.name']} {props.get('node.description', '')} {output_route(props, routes) or ''}"

    exact = [props for props in candidates if props["node.name"] == hint]
    matching = exact or [props for props in candidates if hint.lower() in described(props).lower()]
    return matching[0]["node.name"] if matching else None


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
    if len(on) == 0:  # pure silence: a muted microphone delivers exact zeros
        return UNUSABLE
    trim = int(round(FADE_S / BLOCK_S)) + 1
    steady = base[on[0] + trim : on[-1] + 1 - trim]
    if len(steady) < MIN_BLOCKS:
        return UNUSABLE

    level = np.abs(steady).mean() + 1e-12
    motion = np.sqrt((np.abs(steady - steady.mean(axis=0)) ** 2).mean()) + 1e-12
    return SonarReading(float(20 * np.log10(2 * level)), float(20 * np.log10(motion / level)))


class Sonar:
    def __init__(self, tone_hz=20000.0, burst_s=1.4, speaker="speaker", microphone="digital microphone"):
        self.abort = threading.Event()  # set to cut a tone short when sensing is held off
        self.tone_hz = tone_hz
        self.speaker = speaker
        self.microphone = microphone
        self.tone = make_tone(tone_hz, burst_s)
        self.found = (None, None)  # the node names the hints last matched

    def nodes(self):
        """Current speaker and microphone node names; either is None if missing."""
        objects = pipewire_objects()
        self.found = (
            find_node("Audio/Sink", self.speaker, objects),
            find_node("Audio/Source", self.microphone, objects),
        )
        return self.found

    def play(self, pcm, sink=None):
        """Play samples on the speaker only; False if it is not there, or if cut short."""
        sink = sink or self.nodes()[0]
        if sink is None or self.abort.is_set():
            return False
        deadline = time.monotonic() + len(pcm) / (RATE * CHANNELS * 2) + PLAY_SLACK_S
        try:
            player = subprocess.Popen(
                player_command(sink), stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as error:
            log.debug("pw-play failed: %s", error)
            return False
        pending = pcm
        try:
            while True:
                try:
                    player.communicate(pending, timeout=0.1)
                    return player.returncode == 0
                except subprocess.TimeoutExpired:
                    pending = None  # handed over already; communicate keeps feeding it
                if self.abort.is_set() or time.monotonic() > deadline:
                    return False
        finally:
            if player.poll() is None:
                player.kill()
                player.wait()

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
                try:
                    recorder.wait(timeout=STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                    recorder.wait()
            if not played:
                return UNUSABLE
            recording.seek(0)
            raw = recording.read()
        pcm = np.frombuffer(raw[: len(raw) // (2 * CHANNELS) * 2 * CHANNELS], dtype="<i2")
        return analyze(pcm.reshape(-1, CHANNELS) / 32768.0, self.tone_hz)


def player_command(sink):
    return ["pw-play", *STREAM, "--target", sink, "-P", PLAYBACK_PROPERTIES, "-"]


class NoSonar:
    """Stands in when the sonar is turned off."""

    speaker = microphone = "disabled"

    def __init__(self):
        self.abort = threading.Event()
    found = ("disabled", "disabled")

    def nodes(self):
        return self.found

    def probe(self):
        return UNUSABLE
