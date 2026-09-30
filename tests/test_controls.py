import math

from presenced.controls import Controls


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_timed_pause_runs_out():
    clock = Clock()
    controls = Controls(clock)
    controls.pause(60)
    assert controls.paused
    clock.now += 59
    assert controls.paused
    clock.now += 2
    assert not controls.paused
    assert controls.paused_until is None


def test_pause_without_a_time_lasts_until_resumed():
    clock = Clock()
    controls = Controls(clock)
    controls.pause(0)
    assert controls.paused_until == math.inf
    clock.now += 1e9
    assert controls.paused
    controls.resume()
    assert not controls.paused


def test_only_the_holder_releases_an_inhibitor():
    controls = Controls(Clock())
    cookie = controls.inhibit(":1.5", "zoom", "call")
    assert controls.inhibited
    assert not controls.uninhibit(cookie, ":1.9")
    assert controls.uninhibit(cookie, ":1.5")
    assert not controls.inhibited


def test_inhibitors_go_when_their_program_leaves_the_bus():
    controls = Controls(Clock())
    controls.inhibit(":1.5", "zoom", "call")
    controls.inhibit(":1.5", "zoom", "screen share")
    kept = controls.inhibit(":1.7", "obs", "recording")
    assert controls.release_owner(":1.5") == 2
    assert list(controls.inhibitors) == [kept]
