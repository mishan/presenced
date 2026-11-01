"""The whole daemon on a real desktop, with only the sensors scripted.

rig/scenario.py DESKTOP stay|leave|sensors|idle. The desktop's own idle lock is set
short, so that "it did not lock" means something, and whether it locked is read
the way presenced reads it. With sensors, the sonar and camera are the real code,
down to the commands they run, which rig/fakes stands in for.
"""
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/repo")

from dbus_fast import BusType  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402

from presenced.config import Config  # noqa: E402
from presenced.desktop import call  # noqa: E402
from presenced.ir import fetch_model  # noqa: E402
from presenced.keep import POWER, SCREENSAVER  # noqa: E402
from presenced.monitor import Monitor, State  # noqa: E402
from presenced.sensor import Reading, Verdict  # noqa: E402
from presenced.service import Daemon, make_sensors  # noqa: E402

LOCK_AFTER_S = float(os.environ["LOCK_AFTER_S"])
EVERY_S = 10.0
RETURNED = "/tmp/returned"
SCENE = "/tmp/rig-scene"
IDLE_ACTION_S = float(os.environ["IDLE_ACTION_S"] or 0)
VIDEO = {"gnome": (SCREENSAVER, "/org/freedesktop/ScreenSaver", SCREENSAVER), "kde": (SCREENSAVER, "/org/freedesktop/ScreenSaver", SCREENSAVER),
         "xfce": POWER}
REMOTE = "org.gnome.Mutter.RemoteDesktop"


class Scripted:
    def __init__(self, name, role, verdict):
        self.name, self.role, self.verdict, self.reads = name, role, verdict, 0

    def read(self):
        self.reads += 1
        return Reading(self.verdict)

    def hold(self):
        pass

    def release(self):
        pass

    def describe(self):
        return "scripted"


class Scenario:
    def __init__(self, desktop, real_sensors):
        self.desktop = desktop
        self.sonar = Scripted("sonar", "poll", Verdict.PRESENT)
        self.camera = Scripted("camera", "look", Verdict.PRESENT)
        config = Config()
        config.sensing.interval, config.sensing.quiet_for = 1.0, 4.0
        config.sensing.recheck_after, config.sensing.start_after_idle = 2.0, 2.0
        config.sensing.keep_awake_every = EVERY_S
        config.wake.enabled = True
        config.actions.on_away = "loginctl lock-session $PRESENCED_SESSION"
        config.actions.on_present = f"touch {RETURNED}"
        sensors = [self.sonar, self.camera]
        if real_sensors:
            self.scene("face moving")
            os.environ["PATH"] = f"/repo/rig/fakes/bin:{os.environ['PATH']}"
            fetch_model()
            config.camera.device = "/dev/video0"
            sensors = make_sensors(config)
        self.monitor = Monitor(sensors, 4.0, 2.0, look_every=EVERY_S)
        self.daemon = Daemon(config, monitor=self.monitor)
        self.results = []
        self.modes = []
        set_mode = self.daemon.set_mode
        self.daemon.set_mode = lambda mode: (self.modes.append(mode), set_mode(mode))[1]

    def check(self, name, passed, **detail):
        self.results.append({"check": name, "passed": bool(passed), **detail})
        print(f"{'ok  ' if passed else 'FAIL'} {name} {detail}", flush=True)

    def scene(self, text):
        with open(SCENE, "w") as scene:
            scene.write(text)

    def verdict(self, sensor):
        return self.monitor.readings[sensor].verdict

    async def locked(self):
        return await self.daemon.desktop.locked()

    async def within(self, seconds, condition):
        """Seconds until the condition held, or None."""
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if await condition():
                return round(time.monotonic() - started, 1)
            await asyncio.sleep(0.5)
        return None

    async def take(self):
        taken = await self.within(30, self.held)
        self.check("a face takes the hold", taken is not None, after_s=taken)

    async def sit(self):
        await asyncio.sleep(LOCK_AFTER_S + 15)
        self.check("sitting still past the idle lock does not lock", not await self.locked())

    async def held(self):
        return self.daemon.keeper is not None and self.daemon.keeper.held

    async def video(self):
        other = await MessageBus(bus_type=BusType.SESSION).connect()
        target = VIDEO[self.desktop]
        cookie = (await call(other, *target, "Inhibit", "ss", ("firefox", "video")))[0]
        seen = await self.within(15, self.playing)
        self.check("a video is noticed", seen is not None)
        self.check("a video leaves the hold in place", await self.held())
        self.modes.clear()
        await call(other, *target, "UnInhibit", "u", (cookie,))
        over = await self.within(15, self.not_playing)
        await asyncio.sleep(3)
        self.check("a video ending is noticed", over is not None)
        self.check("a video ending is not read as input", "input" not in self.modes, modes=self.modes[:6])
        self.check("the hold survives a video ending", await self.held() and not await self.locked())
        other.disconnect()

    async def playing(self):
        return self.daemon.mode == "held"

    async def not_playing(self):
        return self.daemon.mode == "sensing"

    async def touch(self):
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        session = (await call(bus, REMOTE, "/org/gnome/Mutter/RemoteDesktop", REMOTE, "CreateSession"))[0]
        await call(bus, REMOTE, session, f"{REMOTE}.Session", "Start")
        await call(bus, REMOTE, session, f"{REMOTE}.Session", "NotifyPointerMotionRelative", "dd", (5.0, 5.0))
        typed = await self.within(5, self.typing)
        self.check("real input is seen as input", typed is not None)
        await asyncio.sleep(3)
        bus.disconnect()

    async def typing(self):
        return self.daemon.mode == "input"

    async def lapse(self):
        self.camera.verdict = Verdict.ABSENT
        gone = await self.within(self.monitor.keep_for + 15, self.let_go)
        self.check("motion alone does not keep the hold", gone is not None, after_s=gone)
        locked = await self.within(LOCK_AFTER_S + 30, self.locked)
        self.check("the desktop's own lock returns", locked is not None, after_s=locked)
        await asyncio.sleep(3)
        looks, probes = self.camera.reads, self.sonar.reads
        await asyncio.sleep(2.5 * EVERY_S)
        self.check("no forced looks while locked", self.camera.reads == looks and self.sonar.reads > probes)

    async def let_go(self):
        return not await self.held()

    async def leave(self):
        self.sonar.verdict, self.camera.verdict = Verdict.QUIET, Verdict.ABSENT
        away = await self.within(30, self.away)
        self.check("leaving reaches away", away is not None, after_s=away)
        self.check("away gives the hold back", await self.within(3, self.let_go) is not None)
        locked = await self.within(15, self.locked)
        self.check("away locks through on_away", locked is not None, after_s=locked)
        await asyncio.sleep(8)
        self.check("the idle reset of a release is not a return", not os.path.exists(RETURNED))

    async def away(self):
        return self.monitor.state is State.AWAY

    async def present(self):
        return self.monitor.state is State.PRESENT

    async def sensed(self):
        await self.take()
        camera = self.monitor.readings["camera"]
        self.check("the detector finds the face", camera.verdict is Verdict.PRESENT, **camera.detail)
        self.check("the sonar hears motion", self.verdict("sonar") is Verdict.PRESENT)
        self.scene("empty still")
        away = await self.within(40, self.away)
        self.check("an empty room reaches away", away is not None, after_s=away)
        self.check("the detector finds nobody", self.verdict("camera") is Verdict.ABSENT)
        self.check("the sonar hears stillness", self.verdict("sonar") is Verdict.QUIET)
        self.check("that locks through on_away", await self.within(15, self.locked) is not None)
        self.scene("face moving")
        back = await self.within(30, self.present)
        self.check("a face coming back is seen", back is not None and self.verdict("camera") is Verdict.PRESENT)

    async def session_there(self):
        return await call(self.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "NameHasOwner", "s", ("org.gnome.SessionManager",))

    async def session_gone(self):
        return await self.session_there() == [False]

    async def idle_action(self):
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        await self.take()
        await asyncio.sleep(IDLE_ACTION_S + 25)
        self.check("a hold keeps gsd-power's idle action off", await self.session_there() == [True])
        self.camera.verdict = Verdict.ABSENT
        gone = await self.within(self.monitor.keep_for + IDLE_ACTION_S + 45, self.session_gone)
        # sooner than the idle action could run is a session that died by itself
        self.check("the idle action runs once the hold lapses", gone is not None and gone >= IDLE_ACTION_S, after_s=gone)

    async def run(self, name):
        task = asyncio.ensure_future(self.daemon.run())
        await asyncio.sleep(1)
        if name == "sensors":
            await self.sensed()
        elif name == "idle":
            await self.idle_action()
        elif name == "stay":
            await self.take()
            await self.sit()
            await self.video()
            if self.desktop == "gnome":
                await self.touch()
                await self.take()
            await self.lapse()
        else:
            await self.take()
            await self.leave()
        self.daemon.stop.set()
        await task


logging.basicConfig(level=logging.INFO, format="log %(message)s")
scenario = Scenario(sys.argv[1], sys.argv[2] == "sensors")
asyncio.run(asyncio.wait_for(scenario.run(sys.argv[2]), 300))
print(json.dumps(scenario.results))
sys.exit(0 if scenario.results and all(r["passed"] for r in scenario.results) else 1)
