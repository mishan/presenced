import asyncio
import math
import threading

import pytest
from dbus_fast import MessageType
from dbus_fast.errors import DBusError

from presenced.config import Config
from presenced.ir import IRReading
from presenced.monitor import State
from presenced.service import OBJECT_PATH, Daemon, PresenceInterface

FACE = IRReading(1, 0.9, 20.0, False)


class FakeCamera:
    device = "fake"

    def __init__(self):
        self.abort = threading.Event()
        self.idle = threading.Event()
        self.idle.set()


class FakeMonitor:
    """Goes where it is told on its next step."""

    def __init__(self):
        self.state = State.AWAY
        self.next_state = State.AWAY
        self.next_look = None
        self.last_look = None
        self.last_sonar = None
        self.sonar = type("Sonar", (), {"found": ("sink", "source"), "speaker": "speaker", "microphone": "mic"})()
        self.camera = FakeCamera()
        self.hold = threading.Event()
        self.on_change = None
        self.steps = 0
        self.restarts = 0

    def step(self):
        self.steps += 1
        self.last_look = self.next_look
        self.state = self.next_state

    def active(self):
        self.state = State.PRESENT

    def restart(self):
        self.restarts += 1


class FakeDesktop:
    def __init__(self, locked=True, idle=600.0, inhibited=False):
        self.is_locked = locked
        self.idle = idle
        self.inhibited = inhibited

    async def idle_inhibited(self):
        return self.inhibited

    async def locked(self):
        return self.is_locked

    async def idle_seconds(self):
        return self.idle


class FakeWaker:
    def __init__(self):
        self.wakes = 0

    async def wake(self):
        self.wakes += 1
        return True


class FakeInterface:
    PausedUntil = 0.0
    Inhibitors = []

    def emit_properties_changed(self, changes):
        pass

    def StateChanged(self, state):
        pass


def daemon(wake=True, locked=True, idle=600.0):
    config = Config()
    config.wake.enabled = wake
    monitor, waker = FakeMonitor(), FakeWaker()
    d = Daemon(config, monitor=monitor, waker=waker)
    d.desktop = FakeDesktop(locked, idle)
    d.interface = FakeInterface()
    return d, monitor, waker


def tick(d):
    async def run():
        d.loop = asyncio.get_running_loop()
        await d.tick()

    asyncio.run(run())


def test_a_face_returning_to_a_locked_screen_wakes_it():
    d, monitor, waker = daemon()
    monitor.next_state, monitor.next_look = State.PRESENT, FACE
    tick(d)
    assert waker.wakes == 1
    assert d.mode == "watching"


def test_motion_alone_does_not_wake_the_screen():
    d, monitor, waker = daemon()
    monitor.next_state = State.PRESENT  # a blind camera and motion
    tick(d)
    assert waker.wakes == 0


def test_already_present_does_not_wake_again():
    d, monitor, waker = daemon()
    monitor.state = monitor.next_state = State.PRESENT
    monitor.next_look = FACE
    tick(d)
    assert waker.wakes == 0


def test_without_wake_nothing_is_sensed_while_locked():
    d, monitor, waker = daemon(wake=False)
    monitor.next_state, monitor.next_look = State.PRESENT, FACE
    tick(d)
    assert monitor.steps == 0
    assert waker.wakes == 0
    assert d.mode == "locked"


def test_an_unlocked_return_does_not_nudge():
    d, monitor, waker = daemon(locked=False)
    monitor.next_state, monitor.next_look = State.PRESENT, FACE
    tick(d)
    assert waker.wakes == 0
    assert d.mode == "sensing"


def test_input_at_the_lock_screen_counts_as_present():
    d, monitor, waker = daemon(idle=1.0)
    tick(d)
    assert monitor.state is State.PRESENT
    assert monitor.steps == 0
    assert d.mode == "input"


def test_unlocking_counts_as_present():
    d, monitor, waker = daemon(wake=False)
    tick(d)
    d.desktop.is_locked = False
    monitor.next_state = State.AWAY
    tick(d)
    assert monitor.state is State.AWAY  # stepped after the unlock made it present
    assert monitor.steps == 1


def test_a_playing_video_freezes_the_state():
    d, monitor, waker = daemon(locked=False)
    d.desktop.inhibited = True
    tick(d)
    assert monitor.steps == 0
    assert monitor.state is State.AWAY  # frozen as it was, not forced to present
    assert monitor.hold.is_set()
    assert d.mode == "held"


def test_a_video_behind_the_lock_screen_does_not_stop_watching():
    d, monitor, waker = daemon(locked=True)
    d.desktop.inhibited = True
    monitor.next_state, monitor.next_look = State.PRESENT, FACE
    tick(d)
    assert monitor.steps == 1
    assert waker.wakes == 1


def test_idle_inhibitors_can_be_ignored():
    d, monitor, waker = daemon(locked=False)
    d.config.sensing.respect_idle_inhibitors = False
    d.desktop.inhibited = True
    tick(d)
    assert monitor.steps == 1


def test_pausing_freezes_the_state_and_takes_the_camera_back():
    d, monitor, waker = daemon(locked=False)
    d.pause(60)
    assert monitor.hold.is_set()
    assert monitor.camera.abort.is_set()
    monitor.next_state = State.PRESENT
    tick(d)
    assert monitor.steps == 0
    assert monitor.state is State.AWAY
    assert d.mode == "paused"


def test_resuming_releases_the_hold_and_starts_counting_afresh():
    d, monitor, waker = daemon(locked=False)
    d.pause(0)
    tick(d)
    d.resume()
    tick(d)
    assert not monitor.hold.is_set()
    assert not monitor.camera.abort.is_set()
    assert monitor.restarts == 1
    assert monitor.steps == 1


def test_a_timed_pause_runs_out_by_itself():
    d, monitor, waker = daemon(locked=False)
    clock = [1000.0]
    d.controls.clock = lambda: clock[0]
    d.pause(30)
    tick(d)
    assert monitor.steps == 0
    clock[0] += 31
    tick(d)
    assert monitor.steps == 1


def test_bad_pause_lengths_are_refused_over_dbus():
    d, monitor, waker = daemon()
    interface = PresenceInterface(d)
    for seconds in (math.nan, math.inf, -5.0):
        with pytest.raises(DBusError):
            interface.Pause(seconds)
    assert not d.controls.paused


def test_suspend_restarts_the_count_without_changing_the_state():
    d, monitor, waker = daemon(locked=False)
    d.sleep_offset -= 3600  # as if an hour of boot time passed without us
    tick(d)
    assert monitor.restarts == 1
    assert monitor.state is State.AWAY


def test_a_failing_tick_is_logged_and_the_daemon_carries_on(caplog):
    d, monitor, waker = daemon(locked=False)

    async def broken():
        raise RuntimeError("bus went away")

    d.desktop.locked = broken

    async def run():
        d.loop = asyncio.get_running_loop()
        await d.safe_tick()
        await d.safe_tick()

    asyncio.run(run())
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1  # in full once, then quietly


class Message:
    def __init__(self, sender, interface, path=OBJECT_PATH):
        self.message_type = MessageType.METHOD_CALL
        self.sender = sender
        self.interface = interface
        self.path = path
        self.member = "Inhibit"


def test_the_caller_is_known_even_without_an_interface_name():
    d, monitor, waker = daemon()
    d.on_message(Message(":1.5", "io.github.mishan.Presenced1"))
    d.on_message(Message(":1.9", None))
    assert d.sender == ":1.9"


def test_actions_run_one_at_a_time_in_order():
    d, monitor, waker = daemon()
    ran = []

    async def run_action(command, state):
        ran.append(f"start {command}")
        await asyncio.sleep(0.01)
        ran.append(f"end {command}")

    d.run_action = run_action

    async def run():
        worker = asyncio.ensure_future(d.run_actions())
        d.actions.put_nowait(("lock", State.AWAY))
        d.actions.put_nowait(("hello", State.PRESENT))
        await asyncio.sleep(0.1)
        worker.cancel()

    asyncio.run(run())
    assert ran == ["start lock", "end lock", "start hello", "end hello"]
