"""Settings, read from a TOML file with defaults for everything."""
import json
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "presenced"


def data_home():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "presenced"


DEFAULT_PATH = config_home() / "config.toml"


@dataclass
class Sensing:
    start_after_idle: float = 5.0  # seconds without keyboard or mouse input before sensing starts
    quiet_for: float = 30.0  # seconds the sonar must stay quiet before the camera is asked
    recheck_after: float = 10.0  # seconds between the two empty looks that mean away
    interval: float = 3.0  # seconds between sonar probes
    respect_idle_inhibitors: bool = True  # while a video plays, sense nothing and change nothing


@dataclass
class Sonar:
    enabled: bool = True
    tone_hz: float = 20000.0
    burst: float = 1.4  # seconds of tone per probe
    threshold_db: float = -28.0  # motion above this counts as movement
    speaker: str = "speaker"  # PipeWire sink: node name, or part of its name or description
    microphone: str = "digital microphone"  # PipeWire source, matched the same way


@dataclass
class Camera:
    enabled: bool = True
    device: str = ""  # empty: the first camera that offers greyscale
    score_threshold: float = 0.5
    min_face_width: float = 0.08  # share of the frame width; smaller faces are across the room


@dataclass
class Proximity:
    enabled: bool = True
    device: str = ""  # IIO device, as iio:deviceN or a path; empty: the first one named prox


@dataclass
class Wake:
    enabled: bool = False  # keep sensing while locked, and wake the screen when someone comes
    command: str = ""  # shell command that wakes the screen; empty: a nudge of a virtual mouse


@dataclass
class Actions:
    on_away: str = ""  # shell command run when the user leaves
    on_present: str = ""  # shell command run when the user comes back


@dataclass
class Config:
    sensing: Sensing = field(default_factory=Sensing)
    sonar: Sonar = field(default_factory=Sonar)
    camera: Camera = field(default_factory=Camera)
    proximity: Proximity = field(default_factory=Proximity)
    wake: Wake = field(default_factory=Wake)
    actions: Actions = field(default_factory=Actions)


class ConfigError(ValueError):
    pass


# (lowest, highest) allowed for each number; None leaves that side open
LIMITS = {
    "sensing.start_after_idle": (0.0, None),
    "sensing.quiet_for": (1.0, None),
    "sensing.recheck_after": (0.0, None),
    "sensing.interval": (0.5, 60.0),
    "sonar.tone_hz": (15000.0, 23500.0),  # below is audible to most people, above aliases at 48 kHz
    "sonar.burst": (1.0, 10.0),  # shorter leaves too little steady tone to measure
    "sonar.threshold_db": (-80.0, 0.0),
    "camera.score_threshold": (0.0, 1.0),
    "camera.min_face_width": (0.0, 1.0),
}


def parse(text, source="config"):
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{source}: {error}") from None
    config = Config()
    sections = {f.name: getattr(config, f.name) for f in fields(config)}
    for name, values in raw.items():
        if name not in sections:
            raise ConfigError(f"{source}: unknown section [{name}]")
        if not isinstance(values, dict):
            raise ConfigError(f"{source}: {name} must be a section")
        section = sections[name]
        known = {f.name: f for f in fields(section)}
        for key, value in values.items():
            if key not in known:
                raise ConfigError(f"{source}: unknown setting {name}.{key}")
            expected = type(getattr(section, key))
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, expected):
                raise ConfigError(f"{source}: {name}.{key} must be {expected.__name__}")
            low, high = LIMITS.get(f"{name}.{key}", (None, None))
            if (low is not None and value < low) or (high is not None and value > high):
                span = f"between {low:g} and {high:g}" if low is not None and high is not None else (
                    f"at least {low:g}" if low is not None else f"at most {high:g}"
                )
                raise ConfigError(f"{source}: {name}.{key} must be {span}, not {value:g}")
            setattr(section, key, value)
    return config


def load(path=None):
    """The configuration at path, or the defaults if there is no file."""
    path = Path(path) if path else DEFAULT_PATH
    if not path.exists():
        return Config()
    return parse(path.read_text(), str(path))


TEMPLATE = """\
# presenced configuration. Every setting is optional; these are the defaults.

[sensing]
# Seconds without keyboard or mouse input before sensing starts.
start_after_idle = {sensing.start_after_idle:g}
# Seconds the sonar must stay quiet before the camera is asked.
quiet_for = {sensing.quiet_for:g}
# Seconds between the two empty camera looks that mean away.
recheck_after = {sensing.recheck_after:g}
# Seconds between sonar probes.
interval = {sensing.interval:g}
# While a program has asked the desktop not to go idle, as browsers and video
# players do during playback, sense nothing and leave the state as it is. Your
# desktop itself would not blank or lock the screen then either.
respect_idle_inhibitors = {idle_inhibitors}

[sonar]
enabled = {sonar_enabled}
tone_hz = {sonar.tone_hz:g}
burst = {sonar.burst:g}
threshold_db = {sonar.threshold_db:g}
# PipeWire nodes: a node name, or part of a name or description.
speaker = {speaker}
microphone = {microphone}

[camera]
enabled = {camera_enabled}
# Empty: the first camera that offers greyscale (IR) frames.
device = {device}
score_threshold = {camera.score_threshold:g}
# Faces narrower than this share of the frame are too far away to count.
min_face_width = {camera.min_face_width:g}

[proximity]
# The laptop's own human presence sensor, if it has one that Linux exposes
# (hid-sensor-prox). It sees someone sitting still, so the camera rarely needs to.
enabled = {proximity_enabled}
# Empty: the first IIO device named prox. Or iio:deviceN, or its path in /sys.
device = {proximity_device}

[wake]
# Keep sensing while the screen is locked, and wake the screen when someone
# comes to the laptop. The camera LED lights whenever it looks, so this is off
# by default.
enabled = {wake_enabled}
# Shell command that wakes the screen. Empty: nudge a virtual mouse, which
# needs write access to /dev/uinput.
command = {wake_command}

[actions]
# Shell commands. PRESENCED_STATE is set to "away" or "present", and
# PRESENCED_SESSION to the id of your desktop's login session. To lock the
# screen when you leave: on_away = "loginctl lock-session $PRESENCED_SESSION"
on_away = {on_away}
on_present = {on_present}
"""


def render(config):
    return TEMPLATE.format(
        sensing=config.sensing,
        sonar=config.sonar,
        camera=config.camera,
        actions=config.actions,
        idle_inhibitors=str(config.sensing.respect_idle_inhibitors).lower(),
        sonar_enabled=str(config.sonar.enabled).lower(),
        camera_enabled=str(config.camera.enabled).lower(),
        proximity_enabled=str(config.proximity.enabled).lower(),
        proximity_device=json.dumps(config.proximity.device),
        wake_enabled=str(config.wake.enabled).lower(),
        wake_command=json.dumps(config.wake.command),
        # JSON strings are valid TOML basic strings, escapes included
        speaker=json.dumps(config.sonar.speaker),
        microphone=json.dumps(config.sonar.microphone),
        device=json.dumps(config.camera.device),
        on_away=json.dumps(config.actions.on_away),
        on_present=json.dumps(config.actions.on_present),
    )
