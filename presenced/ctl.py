"""presencectl: query and control presenced, and set it up."""
import argparse
import asyncio
import contextlib
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

from . import __version__
from .config import DEFAULT_PATH, Config, ConfigError, load, render

BUS_NAME = "io.github.mishan.Presenced"
OBJECT_PATH = "/io/github/mishan/Presenced"
INTERFACE = "io.github.mishan.Presenced1"

UNIT_NAME = "presenced.service"
UNIT = """\
[Unit]
Description=Presence detection from the speaker, microphones and IR camera
Documentation=https://github.com/mishan/presenced
PartOf=graphical-session.target
After=graphical-session.target pipewire.service wireplumber.service

[Service]
Type=dbus
BusName={bus_name}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
# a bad configuration (2) or another presenced already running (3) will not
# get better by restarting
RestartPreventExitStatus=2 3

[Install]
WantedBy=graphical-session.target
"""


class NotRunning(Exception):
    pass


def parse_duration(text):
    """Seconds in "90", "90s", "15m", "1h" or "1h30m"."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", text)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != text.replace(" ", ""):
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (try 90s, 15m or 1h)")
    return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)


class Client:
    def __init__(self, bus):
        self.bus = bus

    @classmethod
    async def connect(cls):
        return cls(await MessageBus(bus_type=BusType.SESSION).connect())

    async def call(self, member, signature="", body=(), interface=INTERFACE):
        reply = await self.bus.call(
            Message(
                destination=BUS_NAME, path=OBJECT_PATH, interface=interface,
                member=member, signature=signature, body=list(body),
            )
        )
        if reply.message_type == MessageType.ERROR:
            if reply.error_name in (
                "org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.NameHasNoOwner",
            ):
                raise NotRunning()
            raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply.body

    async def properties(self):
        (props,) = await self.call("GetAll", "s", (INTERFACE,), "org.freedesktop.DBus.Properties")
        return {name: variant.value for name, variant in props.items()}

    async def status(self):
        (status,) = await self.call("GetStatus")
        return {name: variant.value for name, variant in status.items()}


@contextlib.asynccontextmanager
async def inhibited(why):
    """Hold presenced off the speaker and camera for a while, if it is running."""
    client = await Client.connect()
    try:
        (cookie,) = await client.call("Inhibit", "ss", ("presencectl", why))
    except NotRunning:
        cookie = None
    try:
        yield
    finally:
        if cookie is not None:
            await client.call("Uninhibit", "u", (cookie,))
        client.bus.disconnect()


def ago(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def clock_time(unix):
    return datetime.datetime.fromtimestamp(unix).strftime("%H:%M")


async def cmd_status(args):
    client = await Client.connect()
    props = await client.properties()
    status = await client.status()
    print(f"State:    {props['State']} for {ago(time.time() - props['StateSince'])}")
    sensing = props["Sensing"]
    if props["Paused"]:
        until = props["PausedUntil"]
        sensing += f" until {clock_time(until)}" if until else " until resumed"
    print(f"Sensing:  {sensing}")
    for cookie, who, why in props["Inhibitors"]:
        print(f"Inhibited by {who}: {why}")
    if "idle_seconds" in status:
        print(f"Input:    idle {ago(status['idle_seconds'])}")
    if status.get("keeping_awake"):
        print("Desktop:  kept awake")
    print(f"Quiet:    {ago(status['quiet_seconds'])}")
    for name, values in status.get("sensors", {}).items():  # an older daemon has none
        values = {key: variant.value for key, variant in values.value.items()}
        device = values.pop("device", "")
        verdict = values.pop("verdict", "not read yet")
        numbers = ", ".join(
            f"{key} {value:.2f}" if isinstance(value, float) else f"{key} {value}" for key, value in values.items()
        )
        print(f"{name.capitalize() + ':':<9} {verdict}{f' ({numbers})' if numbers else ''}, {device}")
    client.bus.disconnect()


async def cmd_watch(args):
    client = await Client.connect()
    await client.properties()  # fail early if presenced is not running
    await client.bus.call(
        Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=[f"type='signal',sender='{BUS_NAME}',path='{OBJECT_PATH}'"],
        )
    )

    def on_message(message):
        if message.message_type != MessageType.SIGNAL or message.path != OBJECT_PATH:
            return
        stamp = time.strftime("%H:%M:%S")
        if message.member == "StateChanged":
            print(f"{stamp} {message.body[0]}", flush=True)
        elif message.member == "PropertiesChanged" and args.all:
            for name, variant in message.body[1].items():
                if name not in ("State", "StateSince"):
                    print(f"{stamp} {name} = {variant.value}", flush=True)

    client.bus.add_message_handler(on_message)
    props = await client.properties()
    print(f"{time.strftime('%H:%M:%S')} {props['State']} (sensing: {props['Sensing']})", flush=True)
    await client.bus.wait_for_disconnect()


async def cmd_pause(args):
    client = await Client.connect()
    await client.call("Pause", "d", (args.duration or 0.0,))
    until = f"until {clock_time(time.time() + args.duration)}" if args.duration else "until resumed"
    print(f"paused {until}")


async def cmd_resume(args):
    client = await Client.connect()
    await client.call("Resume")
    print("resumed")


async def cmd_inhibit(args):
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        sys.exit("presencectl inhibit: give a command to run, for example: presencectl inhibit -- zoom")
    client = await Client.connect()
    (cookie,) = await client.call("Inhibit", "ss", (args.who, args.why))
    process = await asyncio.create_subprocess_exec(*argv)
    code = await process.wait()
    await client.call("Uninhibit", "u", (cookie,))
    sys.exit(code)


async def cmd_sonar(args):
    from .sonar import Sonar

    config = load(args.config).sonar
    sonar = Sonar(config.tone_hz, config.burst, config.speaker, config.microphone)
    speaker, microphone = sonar.nodes()
    print(f"speaker {speaker}\nmicrophone {microphone}")
    async with inhibited("sonar readings"):
        print("level_db motion_db")
        for _ in range(args.count):
            reading = await asyncio.to_thread(sonar.probe)
            note = "" if reading.usable else "  unusable"
            print(f"{reading.level_db:8.1f} {reading.motion_db:9.1f}{note}", flush=True)


async def cmd_camera(args):
    from .ir import IRCamera

    config = load(args.config).camera
    camera = IRCamera(config.device, config.score_threshold, config.min_face_width)
    print(f"camera {camera.device}")
    async with inhibited("camera readings"):
        print("faces score near_pct blind")
        for _ in range(args.count):
            seen = await asyncio.to_thread(camera.look)
            print(f"{seen.faces:5d} {seen.score:5.2f} {seen.near_pct:8.1f} {seen.blind}", flush=True)
            await asyncio.sleep(1)


async def cmd_calibrate(args):
    from . import calibrate
    from .sonar import Sonar

    path = Path(args.config) if args.config else DEFAULT_PATH
    config = load(path).sonar
    async with inhibited("calibration"):
        levels = await asyncio.to_thread(calibrate.sweep, config.speaker, config.microphone)
        tone_hz = calibrate.pick_tone(levels)
        if tone_hz is None:
            sys.exit("No tone came back. Is the speaker muted, or are headphones plugged in?")
        print(f"Best tone: {tone_hz / 1000:.1f} kHz")
        sonar = Sonar(tone_hz, config.burst, config.speaker, config.microphone)
        motion = await asyncio.to_thread(calibrate.measure_room, sonar)
    if len(motion) < calibrate.ROOM_PROBES // 2:
        sys.exit("Too few usable readings from the empty room; try again.")
    threshold = calibrate.pick_threshold(motion)
    print(f"Empty room motion: {min(motion):.1f} to {max(motion):.1f} dB")
    print(f"Motion threshold: {threshold:g} dB")
    existing = path.read_text() if path.exists() else None
    text = calibrate.config_text(existing, tone_hz, threshold)
    if not args.write:
        print(f"\nRun again with --write to save these to {path}, or set them yourself:")
        print(f"  [sonar]\n  tone_hz = {tone_hz:g}\n  threshold_db = {threshold:g}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"Saved to {path}. Restart presenced to use them: systemctl --user restart presenced")


LOCK_ACTION = "loginctl lock-session $PRESENCED_SESSION"


async def cmd_init_config(args):
    path = Path(args.config) if args.config else DEFAULT_PATH
    if path.exists() and not args.force:
        print(f"{path} already exists; leaving it as it is")
        return
    config = Config()
    if args.lock:
        config.actions.on_away = LOCK_ACTION
    config.wake.enabled = args.wake
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(config))
    print(f"wrote {path}")


async def cmd_fetch_model(args):
    from .ir import fetch_model

    print(await asyncio.to_thread(fetch_model))


def find_daemon():
    here = Path(sys.argv[0]).resolve().parent / "presenced"
    if here.exists():
        return str(here)
    found = shutil.which("presenced")
    if found:
        return str(Path(found).resolve())
    sys.exit("cannot find the presenced executable")


async def cmd_install_service(args):
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / UNIT_NAME
    unit.write_text(UNIT.format(bus_name=BUS_NAME, exec_start=find_daemon()))
    print(f"wrote {unit}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.enable:
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=True)
        print("presenced is running and will start with your desktop session")
    else:
        print(f"start it with: systemctl --user enable --now {UNIT_NAME}")


def main():
    parser = argparse.ArgumentParser(prog="presencectl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"presencectl {__version__}")
    commands = parser.add_subparsers(dest="subcommand", required=True)

    commands.add_parser("status", help="show what presenced sees").set_defaults(func=cmd_status)

    watch = commands.add_parser("watch", help="print state changes as they happen")
    watch.add_argument("-a", "--all", action="store_true", help="also print sensing and pause changes")
    watch.set_defaults(func=cmd_watch)

    pause = commands.add_parser("pause", help="stop sensing, for a while or until resumed")
    pause.add_argument("duration", nargs="?", type=parse_duration, help="for example 30m or 1h")
    pause.set_defaults(func=cmd_pause)

    commands.add_parser("resume", help="end a pause").set_defaults(func=cmd_resume)

    inhibit = commands.add_parser("inhibit", help="run a command with sensing held off")
    inhibit.add_argument("--who", default="presencectl")
    inhibit.add_argument("--why", default="command running")
    inhibit.add_argument("argv", nargs=argparse.REMAINDER, metavar="command")
    inhibit.set_defaults(func=cmd_inhibit)

    for name, func, default_count, text in (
        ("sonar", cmd_sonar, 10, "print sonar readings"),
        ("camera", cmd_camera, 5, "print IR camera readings"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("-n", "--count", type=int, default=default_count)
        sub.add_argument("-c", "--config", help="configuration file")
        sub.set_defaults(func=func)

    calibrate = commands.add_parser("calibrate", help="fit the sonar to this laptop and room")
    calibrate.add_argument("-c", "--config", help="configuration file")
    calibrate.add_argument("--write", action="store_true", help="save the results to the configuration file")
    calibrate.set_defaults(func=cmd_calibrate)

    init = commands.add_parser("init-config", help="write a configuration file with every setting")
    init.add_argument("-c", "--config", help="where to write it")
    init.add_argument("--lock", action="store_true", help="lock the screen when you leave")
    init.add_argument("--wake", action="store_true", help="wake the screen when you come back")
    init.add_argument("--force", action="store_true", help="replace an existing file")
    init.set_defaults(func=cmd_init_config)

    commands.add_parser("fetch-model", help="download the face detection model").set_defaults(func=cmd_fetch_model)

    install = commands.add_parser("install-service", help="install the systemd user service")
    install.add_argument("--enable", action="store_true", help="also enable and start it")
    install.set_defaults(func=cmd_install_service)

    args = parser.parse_args()
    try:
        asyncio.run(args.func(args))
    except NotRunning:
        sys.exit("presenced is not running (start it with: systemctl --user start presenced)")
    except (ConfigError, LookupError, FileNotFoundError) as error:
        sys.exit(f"presencectl: {error}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
