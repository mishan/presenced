from presenced.ir import IRReading
from presenced.monitor import Monitor, State
from presenced.sonar import SonarReading

INTERVAL = 3.0
MOVING = SonarReading(-24.0, -15.0)
QUIET = SonarReading(-24.0, -33.0)
MUTED = SonarReading(-120.0, 0.0)
FACE = IRReading(1, 0.9, 20.0, False)
EMPTY = IRReading(0, 0.0, 20.0, False)
BLIND = IRReading(0, 0.0, 0.0, True)


class Fake:
    """Stands in for the sonar, the camera and the clock."""

    def __init__(self):
        self.now = 0.0
        self.reading = MOVING
        self.seen = FACE
        self.looks = 0

    def probe(self):
        return self.reading

    def look(self):
        self.looks += 1
        return self.seen

    def clock(self):
        return self.now


def monitor():
    fake = Fake()
    return fake, Monitor(fake, fake, -28.0, 30.0, 10.0, clock=fake.clock)


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
    from presenced.sonar import NoSonar

    fake, _ = monitor()
    fake.seen = EMPTY
    mon = Monitor(NoSonar(), fake, -28.0, 30.0, 10.0, clock=fake.clock)
    changed_at = run_for(fake, mon, 120)
    assert mon.state is State.AWAY
    assert 40 <= changed_at <= 45


def test_sonar_alone_decides_when_camera_is_off():
    from presenced.ir import NoCamera

    fake, _ = monitor()
    fake.reading = QUIET
    mon = Monitor(fake, NoCamera(), -28.0, 30.0, 10.0, clock=fake.clock)
    changed_at = run_for(fake, mon, 150)
    assert mon.state is State.AWAY
    assert 90 <= changed_at <= 95


def test_blind_camera_is_not_asked_every_probe():
    fake, mon = monitor()
    fake.reading = MUTED
    fake.seen = BLIND
    run_for(fake, mon, 300)
    assert fake.looks <= 300 / 30 + 1
