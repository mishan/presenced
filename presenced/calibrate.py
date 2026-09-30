"""Fit the sonar to this laptop and this room.

Two measurements: which tone frequency the speaker and microphones pass well,
and how much the tone wanders in the empty room. The motion threshold sits a
little above the empty room, so anything that moves stands out.
"""
import re
import statistics
import time

from .config import Config, render
from .sonar import MIN_LEVEL_DB, Sonar, make_beeps

SWEEP_HZ = [19000, 19500, 20000, 20500, 21000, 21500, 22000, 22500]
SWEEP_PROBES = 3
USABLE_SPAN_DB = 6.0  # a tone within this much of the strongest one is good enough
MIN_USABLE_DB = MIN_LEVEL_DB + 10
LEAVE_S = 10.0  # time to get out of the room after the cue
ROOM_PROBES = 10
THRESHOLD_MARGIN_DB = 3.0
THRESHOLD_RANGE_DB = (-40.0, -20.0)


def pick_tone(levels):
    """Highest frequency, hence least audible, that comes through nearly as well as the best."""
    if not levels:
        return None
    best = max(levels.values())
    usable = [hz for hz, level in levels.items() if level >= max(best - USABLE_SPAN_DB, MIN_USABLE_DB)]
    return max(usable) if usable else None


def pick_threshold(empty_room_motion):
    """Motion threshold a margin above the most the empty room wandered."""
    low, high = THRESHOLD_RANGE_DB
    return round(min(high, max(low, max(empty_room_motion) + THRESHOLD_MARGIN_DB)), 1)


def update_config(text, tone_hz, threshold_db):
    """Config text with the [sonar] tone and threshold set, everything else kept."""
    values = {"tone_hz": f"{tone_hz:g}", "threshold_db": f"{threshold_db:g}"}
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "[sonar]"), None)
    if start is None:
        lines += ["", "[sonar]"] + [f"{key} = {value}" for key, value in values.items()]
        return "\n".join(lines) + "\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines)
    )
    for key, value in values.items():
        pattern = re.compile(rf"\s*#?\s*{key}\s*=")
        found = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
        if found is None:
            lines.insert(start + 1, f"{key} = {value}")
            end += 1
        else:
            lines[found] = f"{key} = {value}"
    return "\n".join(lines) + "\n"


def config_text(existing, tone_hz, threshold_db):
    if existing is None:
        config = Config()
        config.sonar.tone_hz = tone_hz
        config.sonar.threshold_db = threshold_db
        return render(config)
    return update_config(existing, tone_hz, threshold_db)


def sweep(speaker, microphone, say=print):
    say("Measuring which tones the speaker and microphones pass. Stay where you are.")
    levels = {}
    for hz in SWEEP_HZ:
        sonar = Sonar(hz, 1.0, speaker, microphone)
        readings = [sonar.probe().level_db for _ in range(SWEEP_PROBES)]
        levels[hz] = statistics.median(readings)
        say(f"  {hz / 1000:4.1f} kHz  {levels[hz]:6.1f} dB")
    return levels


def measure_room(sonar, say=print):
    say("")
    say("Next, the empty room. When you hear two beeps, leave the room (or at least")
    say(f"get well out of view) within {LEAVE_S:g} seconds. Three beeps mean come back.")
    for count in (3, 2, 1):
        say(f"  {count}...")
        time.sleep(1)
    sonar.play(make_beeps(2))
    time.sleep(LEAVE_S)
    motion = []
    for _ in range(ROOM_PROBES):
        reading = sonar.probe()
        if reading.usable:
            motion.append(reading.motion_db)
    sonar.play(make_beeps(3))
    return motion
