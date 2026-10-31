import asyncio
import math
import threading
from types import SimpleNamespace

import pytest
from dbus_fast import MessageType
from dbus_fast.errors import DBusError

from presenced.config import Config
from presenced.monitor import State
from presenced.sensor import Reading, Verdict
from presenced.service import OBJECT_PATH, Daemon, PresenceInterface

FACE = Reading(Verdict.PRESENT)


class FakeSensor:
    def __init__(self, name, role):
        self.name, self.role = name, role
        self.abort = threading.Event()

    def hold(self):
        self.abort.set()

    def release(self):
        self.abort.clear()

    def describe(self):
        return "fake"


class FakeMonitor:
    """Goes where it is told on its next step."""

    def __init__(self):
        self.state = State.AWAY
        self.next_state = State.AWAY
        self.next_look = None
        self.last_look = None
        self.quiet_seconds = 0.0
        self.readings = {}
        self.sonar = FakeSensor("sonar", "poll")
        self.camera = FakeSensor("camera", "look")
        self.sensors = [self.sonar, self.camera]
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

    def freeze(self):
        self.hold.set()


class FakeDesktop:
    def __init__(self, locked=True, idle=600.0, inhibited=False):
        self.is_locked = locked
        self.idle = idle
        self.inhibited = inhibited
        self.silent_since = None
        self.ticks_watched = 0

    async def idle_inhibited(self):
        return self.inhibited

    async def locked(self):
        return self.is_locked

    async def idle_seconds(self):
        return self.idle

    async def watch_input(self):
        self.ticks_watched += 1
        return self.silent_since

    def owner_changed(self, name):
        pass


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


def dispatched(name):
    """The function dbus-fast calls for a method, rather than its Python-side wrapper."""
    return PresenceInterface.__dict__[name].__dict__["__DBUS_METHOD"].fn


def test_pause_and_inhibit_are_coroutines_to_dbus():
    import inspect

    assert inspect.iscoroutinefunction(dispatched("Pause"))
    assert inspect.iscoroutinefunction(dispatched("Inhibit"))


def test_inhibit_replies_only_once_the_check_under_way_is_done():
    d, monitor, waker = daemon()
    interface = PresenceInterface(d)

    async def run():
        d.stepping.clear()  # a check is under way
        d.sender = ":1.5"
        reply = asyncio.ensure_future(dispatched("Inhibit")(interface, "howdy", "face unlock"))
        d.sender = ":1.9"  # the next call arrives before this one is answered
        await asyncio.sleep(0.05)
        assert not reply.done()
        assert monitor.hold.is_set() and monitor.camera.abort.is_set()
        d.stepping.set()
        return await reply

    cookie = asyncio.run(run())
    assert d.controls.inhibitors[cookie].owner == ":1.5"


def test_an_unlock_just_before_a_video_still_counts():
    d, monitor, waker = daemon(wake=False)
    tick(d)  # locked
    d.desktop.is_locked = False
    d.desktop.inhibited = True  # and a video resumes straight away
    tick(d)
    assert monitor.state is State.PRESENT
    assert d.mode == "held"


def test_a_minute_of_failures_ends_the_daemon_for_systemd_to_restart():
    from presenced.service import FAILING_FOR_S

    d, monitor, waker = daemon(locked=False)
    now = [100.0]
    d.clock = lambda: now[0]

    async def broken():
        raise TimeoutError("gnome-shell is not answering")

    d.desktop.locked = broken

    async def run():
        d.loop = asyncio.get_running_loop()
        await d.safe_tick()
        now[0] += FAILING_FOR_S - 1
        await d.safe_tick()
        assert not d.stop.is_set()
        now[0] += 1
        await d.safe_tick()

    asyncio.run(run())
    assert d.stop.is_set() and d.exit_code == 1


def test_one_good_tick_resets_the_failure_clock():
    d, monitor, waker = daemon(locked=False)
    d.failing_since = 5.0

    async def run():
        d.loop = asyncio.get_running_loop()
        await d.safe_tick()

    asyncio.run(run())
    assert d.failing_since is None


def test_a_pause_during_the_desktop_queries_is_not_undone():
    d, monitor, waker = daemon(locked=False)
    interface = PresenceInterface(d)

    async def run():
        d.loop = asyncio.get_running_loop()
        gate = asyncio.Event()

        async def slow_locked():
            await gate.wait()  # logind is slow to answer
            return False

        d.desktop.locked = slow_locked
        ticking = asyncio.ensure_future(d.tick())
        await asyncio.sleep(0.01)
        d.sender = ":1.5"
        reply = asyncio.ensure_future(dispatched("Pause")(interface, 30.0))
        await asyncio.sleep(0.01)
        assert not reply.done()  # no reply while the tick is still under way
        gate.set()
        await ticking
        await reply

    asyncio.run(run())
    assert monitor.steps == 0  # the check did not go ahead
    assert monitor.hold.is_set() and monitor.camera.abort.is_set() and monitor.sonar.abort.is_set()
    assert d.mode == "paused"


def test_a_hold_is_never_lifted_while_paused():
    d, monitor, waker = daemon(locked=False)
    d.pause(30)
    assert d.release() is False
    assert monitor.hold.is_set()


def test_losing_the_bus_ends_the_daemon():
    d, monitor, waker = daemon()
    d.lost_bus("system")
    assert d.stop.is_set() and d.exit_code == 1


def test_an_action_that_hangs_is_killed(monkeypatch, caplog):
    from presenced import service

    monkeypatch.setattr(service, "ACTION_TIMEOUT_S", 0.2)
    d, monitor, waker = daemon()

    async def session():
        return "2", "/org/freedesktop/login1/session/_32"

    d.desktop.session = session
    started = __import__("time").monotonic()
    asyncio.run(d.run_action("sleep 30", State.AWAY))
    assert __import__("time").monotonic() - started < 5
    assert "killed" in caplog.text


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


def test_the_service_unit_does_not_restart_on_permanent_errors():
    from presenced.ctl import UNIT
    from presenced.service import EXIT_CONFIG, EXIT_TAKEN

    assert f"RestartPreventExitStatus={EXIT_CONFIG} {EXIT_TAKEN}" in UNIT
    with open("contrib/presenced.service") as unit:
        assert f"RestartPreventExitStatus={EXIT_CONFIG} {EXIT_TAKEN}" in unit.read()



def test_status_shows_each_sensor(monkeypatch, capsys):
    import time

    import numpy as np
    from dbus_fast import Message, Variant

    from presenced import ctl

    d, monitor, waker = daemon()
    monitor.readings["sonar"] = Reading(Verdict.QUIET, {"level_db": np.float64(-24.0), "usable": True})
    Message(destination="a.b", path="/a", member="c", signature="a{sv}", body=[d.status()])._marshall(False)

    class FakeClient(ctl.Client):
        async def call(self, member, *args):
            if member == "GetStatus":
                return (d.status(),)
            return ({
                "State": Variant("s", "away"), "StateSince": Variant("d", time.time()),
                "Sensing": Variant("s", "sensing"), "Paused": Variant("b", False),
                "PausedUntil": Variant("d", 0.0), "Inhibitors": Variant("a(uss)", []),
            },)

    async def connect(cls):
        return FakeClient(type("Bus", (), {"disconnect": lambda self: None})())

    monkeypatch.setattr(ctl.Client, "connect", classmethod(connect))
    asyncio.run(ctl.cmd_status(None))
    out = capsys.readouterr().out
    assert "Sonar:    quiet (level_db -24.00, usable True), fake\n" in out
    assert "Camera:   not read yet, fake\n" in out


@pytest.mark.parametrize("respect", [True, False])
def test_an_inhibitor_ending_is_not_input(respect):
    # Mutter restarts its idle count when the last idle inhibitor goes
    d, monitor, _ = daemon(locked=False)
    d.config.sensing.respect_idle_inhibitors = respect
    d.desktop.inhibited = True
    tick(d)
    d.desktop.inhibited = False
    d.desktop.idle = 0.7
    tick(d)
    assert monitor.state is State.AWAY
    assert d.mode == "sensing"


def test_input_after_an_inhibitor_ended_still_counts():
    now = [100.0]
    d, monitor, _ = daemon(locked=False)
    d.clock = lambda: now[0]
    d.desktop.inhibited = True
    tick(d)
    d.desktop.inhibited = False
    tick(d)
    now[0] += 5.0
    d.desktop.idle = 0.5
    tick(d)
    assert monitor.state is State.PRESENT
    assert d.mode == "input"


def test_an_inhibitor_ending_behind_the_lock_screen_is_not_input():
    d, monitor, _ = daemon(locked=True)
    d.desktop.inhibited = True
    tick(d)
    d.desktop.inhibited = False
    d.desktop.idle = 0.7
    tick(d)
    assert monitor.state is State.AWAY


def test_an_idle_time_dating_from_the_reset_is_not_input():
    now = [100.0]
    d, monitor, _ = daemon(locked=False)
    d.clock = lambda: now[0]
    d.desktop.inhibited = True
    tick(d)
    d.desktop.inhibited = False
    tick(d)
    now[0] += 3.0
    d.desktop.idle = 3.7
    tick(d)
    assert monitor.state is State.AWAY


@pytest.mark.parametrize(
    "locked, respect", [(True, True), (True, False), (False, False)]
)
def test_a_hung_session_manager_only_matters_when_its_answer_is_needed(locked, respect):
    d, monitor, _ = daemon(locked=locked)
    d.config.sensing.respect_idle_inhibitors = respect

    async def hung():
        raise TimeoutError

    d.desktop.idle_inhibited = hung
    tick(d)
    assert monitor.steps == 1


@pytest.mark.parametrize(
    "silent_since, idle, state",
    [
        (97.0, 0.7, State.AWAY),  # the idle count restarted under a silent watch
        (math.inf, 0.7, State.PRESENT),  # the watch fired
        (99.0, 4.0, State.PRESENT),  # input just before the watch went in place
    ],
)
def test_where_input_is_watched_a_short_idle_time_needs_the_watch_to_agree(silent_since, idle, state):
    d, monitor, _ = daemon(locked=False, idle=idle)
    d.clock = lambda: 100.0
    d.desktop.silent_since = silent_since
    tick(d)
    assert monitor.state is state


@pytest.mark.parametrize("locked, wake, inhibited", [(False, True, True), (True, False, False)])
def test_input_is_watched_while_sensing_is_held(locked, wake, inhibited):
    d, _, _ = daemon(locked=locked, wake=wake)
    d.desktop.inhibited = inhibited
    tick(d)
    assert d.desktop.ticks_watched == 1


def test_an_idle_monitor_changing_hands_reaches_the_desktop():
    d, _, _ = daemon()
    seen = []
    d.desktop.owner_changed = seen.append
    d.on_message(SimpleNamespace(
        message_type=MessageType.SIGNAL, member="NameOwnerChanged", sender="org.freedesktop.DBus",
        path="/org/freedesktop/DBus", body=["some.name", ":1.1", ":1.2"],
    ))
    assert seen == ["some.name"]
