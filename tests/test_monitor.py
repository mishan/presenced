import pytest

from presenced.monitor import Monitor, State
from presenced.sensor import Reading, Verdict

INTERVAL = 3.0
MOVING = Reading(Verdict.PRESENT)
QUIET = Reading(Verdict.QUIET)
MUTED = Reading(Verdict.UNKNOWN)
GONE = Reading(Verdict.ABSENT)
FACE = Reading(Verdict.PRESENT)
EMPTY = Reading(Verdict.ABSENT)
BLIND = Reading(Verdict.UNKNOWN)


class Sensor:
    def __init__(self, name, role, read):
        self.name, self.role, self.read = name, role, read


class Fake:
    """Stands in for the sonar, the camera and the clock."""

    def __init__(self):
        self.now = 0.0
        self.reading = MOVING
        self.seen = FACE
        self.looks = 0
        self.sonar = Sensor("sonar", "poll", lambda: self.reading)
        self.camera = Sensor("camera", "look", self.look)

    def look(self):
        self.looks += 1
        return self.seen

    def clock(self):
        return self.now


def monitor():
    fake = Fake()
    return fake, Monitor([fake.sonar, fake.camera], 30.0, 10.0, clock=fake.clock)


def run_for(fake, mon, seconds):
    """Step until the given time has passed; the state it changed at, if it did."""
    start_state = mon.state
    changed_at = None
    end = fake.now + seconds
    while fake.now < end:
        mon.step()
        if changed_at is None and mon.state is not start_state:
            changed_at = fake.now
        fake.now += INTERVAL
    return changed_at


def test_holding_still_for_a_moment_does_not_look():
    fake, mon = monitor()
    fake.reading = QUIET
    run_for(fake, mon, 20)
    fake.reading = MOVING
    run_for(fake, mon, 6)
    fake.reading = QUIET
    run_for(fake, mon, 20)
    assert fake.looks == 0
    assert mon.state is State.PRESENT


def test_still_person_with_a_face_stays_and_is_not_watched_constantly():
    fake, mon = monitor()
    fake.reading = QUIET
    run_for(fake, mon, 300)
    assert mon.state is State.PRESENT
    assert fake.looks <= 300 / 30


def test_leaving_goes_away_after_quiet_period_and_two_empty_looks():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    changed_at = run_for(fake, mon, 120)
    assert mon.state is State.AWAY
    assert 40 <= changed_at <= 45
    assert fake.looks == 2


def test_motion_between_the_two_looks_starts_over():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    run_for(fake, mon, 33)
    assert fake.looks == 1
    fake.reading = MOVING
    run_for(fake, mon, 3)
    fake.reading = QUIET
    run_for(fake, mon, 27)
    assert mon.state is State.PRESENT
    assert fake.looks == 1


def test_coming_back_is_seen_at_once():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    run_for(fake, mon, 60)
    assert mon.state is State.AWAY
    fake.reading = MOVING
    fake.seen = FACE
    run_for(fake, mon, 3)
    assert mon.state is State.PRESENT


def test_passers_by_do_not_blink_the_camera_every_probe():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    run_for(fake, mon, 60)
    looks = fake.looks
    fake.reading = MOVING
    run_for(fake, mon, 60)
    assert mon.state is State.AWAY
    assert fake.looks - looks <= 60 / 10 + 1


def test_blind_camera_waits_three_quiet_periods():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = BLIND
    changed_at = run_for(fake, mon, 150)
    assert mon.state is State.AWAY
    assert 90 <= changed_at <= 95


def test_muted_speaker_never_goes_away_on_its_own():
    fake, mon = monitor()
    fake.reading = MUTED
    fake.seen = BLIND
    run_for(fake, mon, 300)
    assert mon.state is State.PRESENT


def test_input_brings_the_user_back_and_restarts_the_count():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    run_for(fake, mon, 60)
    assert mon.state is State.AWAY
    mon.active()
    assert mon.state is State.PRESENT
    run_for(fake, mon, 27)
    assert mon.state is State.PRESENT


def test_changes_are_reported_once_each():
    fake, mon = monitor()
    seen = []
    mon.on_change = seen.append
    fake.reading = QUIET
    fake.seen = EMPTY
    run_for(fake, mon, 60)
    mon.active()
    mon.active()
    assert seen == [State.AWAY, State.PRESENT]


def test_camera_alone_decides_when_sonar_is_off():
    fake, _ = monitor()
    fake.seen = EMPTY
    mon = Monitor([fake.camera], 30.0, 10.0, clock=fake.clock)
    changed_at = run_for(fake, mon, 120)
    assert mon.state is State.AWAY
    assert 40 <= changed_at <= 45


def test_sonar_alone_decides_when_camera_is_off():
    fake, _ = monitor()
    fake.reading = QUIET
    mon = Monitor([fake.sonar], 30.0, 10.0, clock=fake.clock)
    changed_at = run_for(fake, mon, 150)
    assert mon.state is State.AWAY
    assert 90 <= changed_at <= 95


def test_blind_camera_is_not_asked_every_probe():
    fake, mon = monitor()
    fake.reading = MUTED
    fake.seen = BLIND
    run_for(fake, mon, 300)
    assert fake.looks <= 300 / 30 + 1


def test_a_held_monitor_neither_steps_nor_switches():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    mon.hold.set()
    run_for(fake, mon, 120)
    assert mon.state is State.PRESENT
    assert fake.looks == 0
    mon.switch(State.AWAY)  # even a step already under way cannot switch it
    assert mon.state is State.PRESENT


def test_restart_forgets_the_quiet_run_but_keeps_the_state():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    run_for(fake, mon, 33)
    assert fake.looks == 1
    mon.restart()
    assert mon.state is State.PRESENT
    run_for(fake, mon, 27)
    assert fake.looks == 1  # a full new quiet period is needed before the next look


def test_a_hold_set_during_the_probe_keeps_the_camera_shut():
    fake, mon = monitor()
    fake.reading = QUIET
    fake.seen = EMPTY
    run_for(fake, mon, 30)
    looks = fake.looks

    def probe_then_pause():
        mon.freeze()
        return fake.reading

    fake.sonar.read = probe_then_pause
    run_for(fake, mon, 30)
    assert fake.looks == looks
    assert mon.state is State.PRESENT


def gone_sensor():
    return Sensor("proximity", "poll", lambda: GONE)


def test_a_poll_sensor_saying_nobody_is_here_skips_the_quiet_wait():
    fake, _ = monitor()
    fake.seen = EMPTY
    mon = Monitor([gone_sensor(), fake.camera], 30.0, 10.0, clock=fake.clock)
    changed_at = run_for(fake, mon, 60)
    assert mon.state is State.AWAY
    assert changed_at <= 12
    assert fake.looks == 2


def test_with_nothing_to_look_a_lasting_absence_decides():
    fake, _ = monitor()
    mon = Monitor([gone_sensor()], 30.0, 10.0, clock=fake.clock)
    assert 10 <= run_for(fake, mon, 30) <= 12


def test_with_nothing_to_look_one_absent_reading_is_not_enough():
    # a presence sensor that times out reads 0, which looks like nobody there
    fake, _ = monitor()
    readings = iter([GONE])
    mon = Monitor([Sensor("proximity", "poll", lambda: next(readings, QUIET))], 30.0, 10.0, clock=fake.clock)
    run_for(fake, mon, 60)
    assert mon.state is State.PRESENT


def test_motion_beats_a_poll_sensor_saying_nobody_is_here():
    fake, _ = monitor()
    fake.seen = EMPTY
    mon = Monitor([fake.sonar, gone_sensor(), fake.camera], 30.0, 10.0, clock=fake.clock)
    run_for(fake, mon, 120)
    assert mon.state is State.PRESENT
    assert fake.looks == 0


def test_a_face_overruling_a_poll_sensor_is_not_watched_constantly():
    fake, _ = monitor()
    mon = Monitor([gone_sensor(), fake.camera], 30.0, 10.0, clock=fake.clock)
    run_for(fake, mon, 300)
    assert mon.state is State.PRESENT
    assert fake.looks <= 300 / 30 + 1


@pytest.mark.parametrize(
    "seen, verdict",
    [
        ((FACE, EMPTY), Verdict.PRESENT),
        ((EMPTY, FACE), Verdict.PRESENT),
        ((BLIND, EMPTY), Verdict.ABSENT),
        ((BLIND, BLIND), Verdict.UNKNOWN),
        ((), Verdict.UNKNOWN),
    ],
)
def test_any_face_wins_and_any_clear_look_beats_a_blind_one(seen, verdict):
    looks = [Sensor(f"look{i}", "look", lambda r=r: r) for i, r in enumerate(seen)]
    mon = Monitor(looks, 30.0, 10.0)
    assert mon.look().verdict is verdict


def test_a_hold_between_look_sensors_stops_the_rest():
    fake, _ = monitor()

    def look_then_pause():
        mon.freeze()
        return EMPTY

    mon = Monitor([Sensor("first", "look", look_then_pause), fake.camera], 30.0, 10.0, clock=fake.clock)
    mon.look()
    assert fake.looks == 0


def test_a_blind_camera_and_a_poll_sensor_saying_nobody_is_here_go_away():
    # the shutter is closed, then the presence sensor sees the user get up
    fake, _ = monitor()
    fake.reading = QUIET
    fake.seen = BLIND
    proximity = Sensor("proximity", "poll", lambda: QUIET)
    mon = Monitor([fake.sonar, proximity, fake.camera], 30.0, 10.0, clock=fake.clock)
    run_for(fake, mon, 36)
    assert mon.state is State.PRESENT
    proximity.read = lambda: GONE
    assert run_for(fake, mon, 15) is not None
    assert mon.state is State.AWAY


def test_a_poll_sensor_saying_nobody_is_here_does_not_look_while_away():
    fake, _ = monitor()
    fake.seen = EMPTY
    mon = Monitor([gone_sensor(), fake.camera], 30.0, 10.0, clock=fake.clock)
    run_for(fake, mon, 30)
    assert mon.state is State.AWAY
    looks = fake.looks
    run_for(fake, mon, 120)
    assert fake.looks == looks


def test_a_poll_sensor_that_keeps_seeing_someone_the_camera_does_not_backs_off():
    fake, _ = monitor()
    fake.seen = EMPTY
    seeing = Sensor("proximity", "poll", lambda: GONE)
    mon = Monitor([seeing, fake.camera], 30.0, 10.0, clock=fake.clock)
    run_for(fake, mon, 30)
    assert mon.state is State.AWAY
    looks = fake.looks
    seeing.read = lambda: MOVING  # someone beside the desk, say
    run_for(fake, mon, 300)
    assert fake.looks - looks <= 300 / 30 + 2


def test_motion_while_away_never_looks_more_often_than_recheck_after():
    fake, _ = monitor()
    fake.seen = EMPTY
    fake.reading = QUIET
    mon = Monitor([fake.sonar, fake.camera], 5.0, 60.0, clock=fake.clock)
    run_for(fake, mon, 120)
    assert mon.state is State.AWAY
    looks = fake.looks
    fake.reading = MOVING
    run_for(fake, mon, 120)
    assert fake.looks - looks <= 120 / 60 + 1


@pytest.mark.parametrize("seen", [FACE, EMPTY])
def test_a_flickering_poll_sensor_does_not_blink_the_camera(seen):
    # someone at the edge of the presence sensor's range: 1, 0, 1, 0...
    fake, _ = monitor()
    fake.seen = seen
    flicker = iter([MOVING, GONE] * 100)
    mon = Monitor([Sensor("proximity", "poll", lambda: next(flicker)), fake.camera], 30.0, 10.0, clock=fake.clock)
    run_for(fake, mon, 120)
    assert mon.state is State.PRESENT
    assert fake.looks <= 120 / 10 + 1
