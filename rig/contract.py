"""What a real desktop does with presenced's idle inhibitor.

Runs presenced's own Keeper and Desktop against the session bus of a running
desktop: rig/contract.py gnome|kde|xfce|sway. It prints what it measured as JSON
and exits non-zero if anything differs from what this desktop is required to do,
so that a run which tested nothing cannot pass.
"""
import asyncio
import importlib.metadata
import json
import logging
import subprocess
import sys
import time

sys.path.insert(0, "/repo")

from dbus_fast import BusType  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402

from presenced.desktop import GNOME_SESSION, MUTTER_IDLE, Desktop, call  # noqa: E402
from presenced.keep import APP, SCREENSAVER, Keeper  # noqa: E402

POWER = ("org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit",
         "org.freedesktop.PowerManagement.Inhibit")
LATE_S = 12.0  # how long to give a desktop to show a change
UP_S = 90.0  # and to bring its services up

# what each desktop must do; a fact missing here is only reported
REQUIRED = {
    "gnome": {
        "up": [SCREENSAVER, MUTTER_IDLE, GNOME_SESSION[0]], "version": ["gnome-shell", "--version"],
        "inhibitor_taken": True, "idle_source": MUTTER_IDLE, "input_watch": True, "get_active_refused": True,
        "power_service": False, "lists_ours": True, "release_resets_idle": True, "drop_clears_hold": True,
    },
    "kde": {
        "up": [SCREENSAVER, POWER[0]], "version": ["kwin_wayland", "--version"],
        "inhibitor_taken": True, "idle_source": None, "get_active_refused": False,
        "power_service": True, "shows_ours": True, "drop_clears_hold": True,
    },
    "xfce": {
        "up": [POWER[0], "org.xfce.ScreenSaver"], "version": ["dpkg-query", "-W", "xfce4-session"],
        "inhibitor_taken": True, "drop_clears_hold": True,
    },
    "sway": {
        "up": [], "version": ["sway", "--version"],
        "inhibitor_taken": False, "idle_source": None, "power_service": False,
    },
}


async def owned(bus, name):
    body = await call(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                      "NameHasOwner", "s", (name,))
    return bool(body and body[0])


async def has_inhibit(bus):
    body = await call(bus, *POWER, "HasInhibit")
    return None if body is None else bool(body[0])


async def our_hold_shows(bus):
    """Whether the desktop itself says ours is held, past presenced's own filtering; None if it cannot say."""
    listed = await call(bus, *GNOME_SESSION, GNOME_SESSION[0], "GetInhibitors")
    if listed is None:
        return await has_inhibit(bus)
    for path in listed[0]:
        app = await call(bus, GNOME_SESSION[0], path, f"{GNOME_SESSION[0]}.Inhibitor", "GetAppId")
        if app and app[0] == APP:
            return True
    return False


async def until(bus, wanted):
    """Seconds until our hold shows as wanted, or None if it never does."""
    started = time.monotonic()
    while time.monotonic() - started < LATE_S:
        if await our_hold_shows(bus) is wanted:
            return round(time.monotonic() - started, 1)
        await asyncio.sleep(0.25)
    return None


async def measure(required):
    facts = {}
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    deadline = time.monotonic() + UP_S
    while not all([await owned(bus, name) for name in required["up"]]) and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    facts["up"] = {name: await owned(bus, name) for name in required["up"]}
    facts["power_service"] = await owned(bus, POWER[0])

    desktop = Desktop(bus, None)
    bus.add_message_handler(desktop.on_message)
    await desktop.idle_seconds()
    facts["idle_source"] = desktop.idle_source[0] if desktop.idle_source else None
    await desktop.watch_input()
    facts["input_watch"] = desktop.armed_at is not None
    await desktop.saver_active()
    facts["get_active_refused"] = desktop.saver_refused
    facts["inhibited_before"] = await desktop.idle_inhibited()

    keeper = Keeper(bus, 300.0)
    await keeper.want(True)
    facts["inhibitor_taken"] = keeper.held
    if not keeper.held:
        return facts
    desktop.holding(True)

    # the whole time as the daemon's ticks would, for a hold that shows late
    facts["own_hold_read_as_video"], facts["shows_ours_after_s"] = False, None
    started = time.monotonic()
    while time.monotonic() - started < LATE_S:
        if await desktop.idle_inhibited():
            facts["own_hold_read_as_video"] = True
        if facts["shows_ours_after_s"] is None and await our_hold_shows(bus):
            facts["shows_ours_after_s"] = round(time.monotonic() - started, 1)
        await asyncio.sleep(0.5)
    facts["shows_ours"] = facts["shows_ours_after_s"] is not None
    facts["lists_ours"] = facts["shows_ours"] and await owned(bus, GNOME_SESSION[0])

    before = await desktop.idle_seconds()
    await keeper.want(False)
    desktop.holding(False)
    facts["release_cleared_after_s"] = await until(bus, False)
    after = await desktop.idle_seconds()
    facts["idle_before_release_s"], facts["idle_after_release_s"] = before, after
    facts["release_resets_idle"] = None if before is None or after is None else after < before
    facts["release_fired_input_watch"] = desktop.armed_at == float("inf")

    other = await MessageBus(bus_type=BusType.SESSION).connect()
    dropped = Keeper(other, 300.0)
    await dropped.want(True)
    facts["second_hold_taken"] = dropped.held
    shown = await until(bus, True) if facts["shows_ours"] else None
    other.disconnect()
    facts["drop_clears_hold"] = shown is not None and await until(bus, False) is not None
    return facts


def verdict(facts, required):
    failures = [f"{key} is {facts.get(key)!r}, not {value!r}" for key, value in required.items()
                if key not in ("up", "version") and facts.get(key) != value]
    failures += [f"{name} never came up" for name, there in facts["up"].items() if not there]
    if facts.get("own_hold_read_as_video"):
        failures.append("our own inhibitor reads as another program's")
    if facts.get("inhibitor_taken") and facts.get("release_cleared_after_s") is None and facts.get("shows_ours"):
        failures.append("the inhibitor survived its release")
    if facts.get("release_fired_input_watch"):
        failures.append("a release fired the input watch")
    return failures


logging.basicConfig(level=logging.WARNING)
wanted = REQUIRED[sys.argv[1]]
measured = asyncio.run(asyncio.wait_for(measure(wanted), UP_S + 90))
version = subprocess.run(wanted["version"], capture_output=True, text=True)
measured["version"] = (version.stdout or version.stderr).strip().splitlines()[0]
measured["dbus_fast"] = importlib.metadata.version("dbus-fast")
broken = verdict(measured, wanted)
print(json.dumps({"facts": measured, "failures": broken}, indent=1))
sys.exit(1 if broken else 0)
