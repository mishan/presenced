"""The presenced daemon: senses presence and publishes it on the session bus."""
import argparse
import asyncio
import inspect
import logging
import os
import signal
import subprocess
import sys
import time

import cv2

from dbus_fast import BusType, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import ErrorType, NameFlag, PropertyAccess, RequestNameReply
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, dbus_property, method
from dbus_fast.service import signal as dbus_signal

from . import __version__
from .config import ConfigError, load
from .controls import Controls
from .desktop import Desktop, call
from .ir import IRCamera
from .keep import Keeper
from .monitor import Monitor, State
from .proximity import ProximitySensor
from .sensor import Verdict
from .sonar import Sonar
from .wake import Waker

log = logging.getLogger("presenced")

BUS_NAME = "io.github.mishan.Presenced"
OBJECT_PATH = "/io/github/mishan/Presenced"
INTERFACE = "io.github.mishan.Presenced1"

SUSPEND_GAP = 5.0  # seconds of boot time the monotonic clock missed: we were asleep
STEP_WAIT_S = 6.0  # how long a pause or inhibit waits for a check under way to finish
FAILING_FOR_S = 60.0  # how long ticks may keep failing before presenced exits for systemd to restart it
EXIT_CONFIG = 2  # a bad configuration: restarting will not help
EXIT_TAKEN = 3  # another presenced owns the bus name: restarting will not help either
ACTION_TIMEOUT_S = 60.0  # an action still running after this long is killed
IDLE_RESET_SLACK_S = 1.0  # input this soon after the desktop restarted its idle count is put down to the restart


class PresenceInterface(ServiceInterface):
    """io.github.mishan.Presenced1, as seen by presencectl, tray icons and scripts."""

    def __init__(self, daemon):
        super().__init__(INTERFACE)
        self.daemon = daemon

    @dbus_property(access=PropertyAccess.READ)
    def State(self) -> "s":
        return self.daemon.monitor.state.value

    @dbus_property(access=PropertyAccess.READ)
    def StateSince(self) -> "d":
        return self.daemon.state_since

    @dbus_property(access=PropertyAccess.READ)
    def Sensing(self) -> "s":
        return self.daemon.mode

    @dbus_property(access=PropertyAccess.READ)
    def Paused(self) -> "b":
        return self.daemon.controls.paused

    @dbus_property(access=PropertyAccess.READ)
    def PausedUntil(self) -> "d":
        until = self.daemon.controls.paused_until
        return 0.0 if until is None or until == float("inf") else until

    @dbus_property(access=PropertyAccess.READ)
    def Inhibitors(self) -> "a(uss)":
        return [[cookie, held.who, held.why] for cookie, held in self.daemon.controls.inhibitors.items()]

    # Pause and Inhibit reply only once a check under way has finished, so the
    # speaker and camera are free when the caller hears back. They are marked
    # as coroutine functions rather than written as ones: the part before the
    # await runs as the call is dispatched, while the caller's name is known.

    @method()
    @inspect.markcoroutinefunction
    def Pause(self, seconds: "d"):
        try:
            self.daemon.pause(seconds)
        except ValueError as error:
            raise DBusError(ErrorType.INVALID_ARGS, str(error)) from None
        return self.daemon.step_finished()

    @method()
    def Resume(self):
        self.daemon.resume()

    @method()
    @inspect.markcoroutinefunction
    def Inhibit(self, who: "s", why: "s") -> "u":
        try:
            cookie = self.daemon.inhibit(who, why)
        except ValueError as error:
            raise DBusError(ErrorType.LIMITS_EXCEEDED, str(error)) from None
        return self.daemon.step_finished(cookie)

    @method()
    def Uninhibit(self, cookie: "u") -> "b":
        return self.daemon.uninhibit(cookie)

    @method()
    def GetStatus(self) -> "a{sv}":
        return self.daemon.status()

    @dbus_signal()
    def StateChanged(self, state) -> "s":
        return state


class Daemon:
    def __init__(self, config, monitor=None, waker=None):
        self.config = config
        self.controls = Controls()
        self.mode = "starting"
        self.state_since = time.time()
        self.idle_seconds = None
        self.was_locked = False
        self.sender = None  # unique name of the caller of the method being handled
        self.poke = asyncio.Event()
        self.stop = asyncio.Event()
        self.actions = asyncio.Queue()
        self.stepping = asyncio.Event()  # set while no check (tick) is under way
        self.stepping.set()
        self.last_paused = False
        self.last_error = None
        self.failing_since = None
        self.exit_code = 0
        self.clock = time.monotonic
        self.was_inhibited = False
        self.idle_reset_at = None  # when the desktop last restarted its idle count by itself
        self.keeper = None
        self.was_holding = False
        self.monitor = monitor or Monitor(
            make_sensors(config), config.sensing.quiet_for, config.sensing.recheck_after,
            look_every=config.sensing.keep_awake_every if config.sensing.keep_awake else None,
        )
        self.monitor.on_change = self.state_changed
        self.waker = waker or Waker(config.wake.command)
        self.sleep_offset = self.boot_offset()

    @staticmethod
    def boot_offset():
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()

    # called from the sensing thread as well as the event loop
    def state_changed(self, state):
        self.loop.call_soon_threadsafe(self.announce, state)

    def announce(self, state):
        self.state_since = time.time()
        self.interface.StateChanged(state.value)
        self.interface.emit_properties_changed({"State": state.value, "StateSince": self.state_since})
        command = self.config.actions.on_away if state is State.AWAY else self.config.actions.on_present
        if command:
            self.actions.put_nowait((command, state))

    async def run_actions(self):
        """Run actions one at a time, in order, so a quick away and back cannot cross over."""
        while True:
            command, state = await self.actions.get()
            try:
                await self.run_action(command, state)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s action failed: %s", state.value, command)

    async def run_action(self, command, state):
        # presenced itself runs outside the desktop session (under systemd, or
        # from a terminal elsewhere), so a bare `loginctl lock-session` would
        # lock the wrong one: tell the command which session is the desktop
        session_id, _ = await self.desktop.session()
        env = dict(os.environ, PRESENCED_STATE=state.value, PRESENCED_SESSION=session_id or "")
        # its own process group, so a timeout or shutdown takes its children too
        process = await asyncio.create_subprocess_shell(command, env=env, start_new_session=True)
        try:
            code = await asyncio.wait_for(process.wait(), ACTION_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError) as stopped:
            kill_group(process)
            await process.wait()
            if isinstance(stopped, asyncio.CancelledError):
                raise
            log.warning("%s action still running after %g s, killed: %s", state.value, ACTION_TIMEOUT_S, command)
            return
        if code:
            log.warning("%s action exited with status %d: %s", state.value, code, command)

    def set_mode(self, mode):
        if mode != self.mode:
            self.mode = mode
            log.info("sensing: %s", mode)
            self.interface.emit_properties_changed({"Sensing": mode})

    def controls_changed(self):
        self.last_paused = self.controls.paused
        self.interface.emit_properties_changed(
            {
                "Paused": self.controls.paused,
                "PausedUntil": self.interface.PausedUntil,
                "Inhibitors": self.interface.Inhibitors,
            }
        )
        self.poke.set()

    def hold_now(self):
        """Freeze the state at once, and stop a check under way from using the speaker or camera."""
        self.monitor.freeze()
        for sensor in self.monitor.sensors:
            sensor.hold()

    async def step_finished(self, result=None):
        """result, once no check is under way any more."""
        try:
            await asyncio.wait_for(self.stepping.wait(), STEP_WAIT_S)
        except TimeoutError:
            log.warning("a check was still running after %g s", STEP_WAIT_S)
        return result

    def hold(self, mode):
        self.set_mode(mode)
        if not self.monitor.hold.is_set():
            self.hold_now()

    @property
    def controlled(self):
        return self.controls.paused or self.controls.inhibited

    def release(self):
        # a pause or inhibit may have arrived while this tick awaited the desktop:
        # it, not the reasons this tick had, decides
        if self.controlled:
            return False
        if self.monitor.hold.is_set():
            self.monitor.hold.clear()
            for sensor in self.monitor.sensors:
                sensor.release()
            self.monitor.restart()  # whatever was counted before the hold is stale now
            self.monitor.look_soon()
        return True

    def pause(self, seconds):
        self.controls.pause(seconds)
        log.info("paused %s", f"for {seconds:g} s" if seconds > 0 else "until resumed")
        self.hold_now()
        self.controls_changed()

    def resume(self):
        self.controls.resume()
        log.info("resumed")
        self.controls_changed()

    def inhibit(self, who, why):
        cookie = self.controls.inhibit(self.sender, who, why)
        log.info("inhibited by %s (%s): %s", who, self.sender, why)
        self.hold_now()
        self.controls_changed()
        return cookie

    def uninhibit(self, cookie):
        released = self.controls.uninhibit(cookie, self.sender)
        if released:
            log.info("inhibitor %d released", cookie)
            self.controls_changed()
        return released

    def status(self):
        monitor = self.monitor
        status = {
            "state": Variant("s", monitor.state.value),
            "sensing": Variant("s", self.mode),
            "quiet_seconds": Variant("d", monitor.quiet_seconds),
            "sensors": Variant("a{sv}", {
                sensor.name: Variant("a{sv}", sensor_status(sensor, monitor.readings.get(sensor.name)))
                for sensor in monitor.sensors
            }),
            "keeping_awake": Variant("b", bool(self.keeper and self.keeper.held)),
            "version": Variant("s", __version__),
        }
        if self.idle_seconds is not None:
            status["idle_seconds"] = Variant("d", self.idle_seconds)
        if self.last_error:
            status["last_error"] = Variant("s", self.last_error)
        return status

    def on_message(self, message):
        """See every incoming message before it is dispatched."""
        if message.message_type == MessageType.METHOD_CALL and message.path == OBJECT_PATH:
            # the service methods cannot see who called them; they read it from here.
            # Match on the path alone: a call may leave out the interface name
            self.sender = message.sender
        elif (
            message.message_type == MessageType.SIGNAL
            and message.member == "NameOwnerChanged"
            and message.sender == "org.freedesktop.DBus"
        ):
            name, _, new_owner = message.body
            self.desktop.owner_changed(name)
            if self.keeper:
                self.keeper.lost(name)
            if not new_owner and self.controls.release_owner(name):
                log.info("inhibitors of %s released: it left the bus", name)
                self.controls_changed()
        return None

    async def tick(self):
        """One round of sensing; pause and inhibit replies wait until it is over."""
        self.stepping.clear()
        try:
            await self.sense()
        finally:
            await self.keep_awake()
            self.stepping.set()

    async def keep_awake(self):
        if not self.keeper or self.mode == "held":
            # held: another program's inhibitor. Ours stays, as giving it back could lock someone
            # seen a moment ago, but goes unrenewed: if that "program" is really our own
            # inhibitor misread, the deadline ends it
            return
        await self.keeper.want(self.monitor.attended and self.mode == "sensing")
        if self.keeper.held != self.was_holding:
            self.was_holding = self.keeper.held
            self.desktop.holding(self.was_holding)

    async def sense(self):
        offset = self.boot_offset()
        if offset - self.sleep_offset > SUSPEND_GAP:
            log.info("resumed from suspend")
            self.monitor.restart()
        self.sleep_offset = offset
        silent_since = await self.desktop.watch_input()  # before any hold: it must be in place when input comes

        if self.controls.paused != self.last_paused:
            self.controls_changed()  # a timed pause ran out
        if self.controlled:
            self.hold("paused" if self.controls.paused else "inhibited")
            return

        locked = await self.desktop.locked()
        unlocked = self.was_locked and not locked
        self.was_locked = locked
        if locked and not self.config.wake.enabled:
            self.hold("locked")
            return
        if unlocked and self.release():
            # only a person can unlock the screen; count it before anything
            # below can hold the state, or it would be lost
            self.monitor.active()

        must_know = self.config.sensing.respect_idle_inhibitors and not locked
        try:
            inhibited = await self.desktop.idle_inhibited()
        except TimeoutError:
            if must_know:
                raise
            inhibited = self.was_inhibited
        if self.was_inhibited and not inhibited:
            self.idle_reset_at = self.clock()
        self.was_inhibited = inhibited
        if inhibited and must_know:
            # a video is playing, say: the desktop will not blank or lock, and neither should we
            self.hold("held")
            return

        if not self.release():
            self.hold("paused" if self.controls.paused else "inhibited")
            return

        self.idle_seconds = await self.desktop.idle_seconds()
        if self.recent_input(silent_since):
            self.set_mode("input")
            self.monitor.active()
            return

        self.set_mode("watching" if locked else "sensing")
        before = self.monitor.state
        self.monitor.force_looks = not locked
        await asyncio.to_thread(self.monitor.step)
        seen = self.monitor.last_look
        if (
            locked and before is State.AWAY and self.monitor.state is State.PRESENT
            and seen and seen.verdict is Verdict.PRESENT
        ):
            log.info("someone came back: waking the screen")
            await self.waker.wake()

    def recent_input(self, silent_since):
        """Whether the idle time means someone touched the laptop, not that an idle inhibitor ended.

        Mutter restarts its idle count when the last inhibitor goes, without any
        input. An event no watch fired for was that; with no watch, the timing has to tell.
        """
        idle = self.idle_seconds
        if idle is None or idle >= self.config.sensing.start_after_idle:
            return False
        last_event = self.clock() - idle
        if silent_since is not None:
            return last_event <= silent_since
        resets = [at for at in (self.idle_reset_at, self.keeper and self.keeper.released_at) if at is not None]
        return not resets or last_event > max(resets) + IDLE_RESET_SLACK_S

    async def safe_tick(self):
        """A tick that logs what went wrong instead of ending the daemon, unless it keeps failing."""
        try:
            await self.tick()
        except Exception as error:
            now = self.clock()
            if self.failing_since is None:
                self.failing_since = now
            summary = f"{type(error).__name__}: {error}"
            if summary != self.last_error:
                log.exception("sensing failed; carrying on")
                self.last_error = summary
            else:
                log.debug("sensing failed again: %s", summary)
            if now - self.failing_since >= FAILING_FOR_S:
                # not getting better by itself: exit, and let systemd start afresh
                log.error("sensing has failed for %g s; exiting", now - self.failing_since)
                self.exit_code = 1
                self.stop.set()
        else:
            self.failing_since = None
            self.last_error = None

    async def run(self):
        self.loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(signum, self.stop.set)

        session = await MessageBus(bus_type=BusType.SESSION).connect()
        system = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.desktop = Desktop(session, system)
        if self.config.sensing.keep_awake:
            self.keeper = Keeper(session, self.monitor.keep_for)
        self.interface = PresenceInterface(self)
        session.add_message_handler(self.on_message)
        session.add_message_handler(self.desktop.on_message)
        session.export(OBJECT_PATH, self.interface)
        await call(
            session, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "AddMatch", "s", ("type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged'",),
        )
        reply = await session.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
        if reply != RequestNameReply.PRIMARY_OWNER:
            log.error("%s is already taken: is presenced running?", BUS_NAME)
            return EXIT_TAKEN

        log.info("presenced %s", __version__)
        for sensor in self.monitor.sensors:
            log.info("%s: %s", sensor.name, sensor.describe())
        if not self.monitor.sensors:
            log.warning("no sensors: only input and unlocking count")
        actions = asyncio.ensure_future(self.run_actions())
        for bus, name in ((session, "session"), (system, "system")):
            asyncio.ensure_future(bus.wait_for_disconnect()).add_done_callback(
                lambda done, name=name: self.lost_bus(name, done)
            )
        interval = self.config.sensing.interval
        while not self.stop.is_set():
            started = self.loop.time()
            self.poke.clear()  # before the tick, so a poke during it is not lost
            await self.safe_tick()
            remaining = max(0.0, interval - (self.loop.time() - started))
            waits = [asyncio.ensure_future(self.poke.wait()), asyncio.ensure_future(self.stop.wait())]
            await asyncio.wait(waits, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            for waiting in waits:
                waiting.cancel()
        log.info("stopping")
        self.stopping = True
        actions.cancel()
        await asyncio.gather(actions, return_exceptions=True)  # kills a running action
        await asyncio.sleep(0.1)  # let pause and inhibit replies still waiting go out
        session.disconnect()
        system.disconnect()
        return self.exit_code

    def lost_bus(self, name, done=None):
        if done is not None and not done.cancelled():
            done.exception()  # retrieved, so asyncio does not complain it never was
        if getattr(self, "stopping", False):
            return
        # every call would fail from now on: exit, and let systemd reconnect us
        log.error("lost the %s bus; exiting", name)
        self.exit_code = 1
        self.stop.set()


def make_sensors(config):
    sensors = (make_sonar(config.sonar), make_proximity(config.proximity), make_camera(config.camera))
    return [sensor for sensor in sensors if sensor]


def make_sonar(config):
    if not config.enabled:
        return None
    return Sonar(config.tone_hz, config.burst, config.speaker, config.microphone, config.threshold_db)


def make_proximity(config):
    """The laptop's presence sensor, or None if it is turned off or there is none."""
    if not config.enabled:
        return None
    try:
        return ProximitySensor(config.device)
    except LookupError as error:
        # most laptops have none, so only a configured one missing is worth a warning
        (log.warning if config.device else log.info)("no proximity sensor: %s", error)
        return None


def make_camera(config):
    """The IR camera, or None if it is turned off or cannot be used."""
    if not config.enabled:
        return None
    try:
        return IRCamera(config.device, config.score_threshold, config.min_face_width)
    except (LookupError, FileNotFoundError, OSError, subprocess.SubprocessError, cv2.error) as error:
        log.warning("no camera: %s", error)
        return None


def sensor_status(sensor, reading):
    """A sensor's latest verdict and numbers, as D-Bus variants."""
    values = {"device": sensor.describe()}
    if reading is not None:
        values = {**values, "verdict": reading.verdict.value, **reading.detail}
    def signature(value):  # bool before int, since a bool is an int
        return next(sig for kind, sig in ((bool, "b"), (int, "i"), (float, "d"), (str, "s")) if isinstance(value, kind))

    return {key: Variant(signature(value), value) for key, value in values.items()}


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main():
    parser = argparse.ArgumentParser(prog="presenced", description=__doc__)
    parser.add_argument("-c", "--config", help="configuration file (default ~/.config/presenced/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every reading")
    parser.add_argument("--version", action="version", version=f"presenced {__version__}")
    args = parser.parse_args()
    under_journal = "JOURNAL_STREAM" in os.environ
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s" if under_journal else "%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    try:
        config = load(args.config)
        daemon = Daemon(config)
    except (ConfigError, LookupError, FileNotFoundError) as error:
        log.error("%s", error)
        return EXIT_CONFIG
    return asyncio.run(daemon.run())


if __name__ == "__main__":
    sys.exit(main())
