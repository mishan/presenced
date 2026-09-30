import asyncio

from presenced.config import Config
from presenced.ir import IRReading
from presenced.monitor import State
from presenced.service import Daemon

FACE = IRReading(1, 0.9, 20.0, False)


class FakeMonitor:
    """Goes where it is told on its next step."""

    def __init__(self):
        self.state = State.AWAY
        self.next_state = State.AWAY
        self.next_look = None
        self.last_look = None
        self.last_sonar = None
        self.sonar = type("Sonar", (), {"found": ("sink", "source"), "speaker": "speaker", "microphone": "mic"})()
        self.on_change = None
        self.steps = 0

    def step(self):
        self.steps += 1
        self.last_look = self.next_look
        self.state = self.next_state

    def active(self):
        self.state = State.PRESENT


class FakeDesktop:
    def __init__(self, locked=True, idle=600.0):
        self.is_locked = locked
        self.idle = idle

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
