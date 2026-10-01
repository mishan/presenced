"""Decide between present and away from the sonar and the camera.

Sonar is the cheap, frequent check. It only sees motion, and a person can hold
still for a while, so a single quiet reading means nothing: the sonar has to
stay quiet for a sustained period before the IR camera is asked whether a face
is still there, and the camera has to miss twice, some time apart, before the
user counts as away.
"""
import enum
import logging
import threading
import time

log = logging.getLogger("presenced")

BLIND_PATIENCE = 3  # with the camera blind, wait this many times longer on sonar alone


class State(enum.Enum):
    PRESENT = "present"
    AWAY = "away"


class Monitor:
    def __init__(
        self,
        sonar,
        camera,
        threshold_db,
        quiet_for,
        recheck_after,
        on_change=None,
        clock=time.monotonic,
    ):
        self.sonar = sonar
        self.camera = camera
        self.threshold_db = threshold_db
        self.quiet_for = quiet_for
        self.recheck_after = recheck_after
        self.on_change = on_change
        self.clock = clock
        self.state = State.PRESENT
        self.quiet_since = None  # start of the current unbroken run of quiet probes
        self.missed_at = None  # when the camera last came up empty in this run
        self.looked_at = None  # when the camera was last asked, while away
        self.blind_at = None  # when the camera last saw nothing at all
        self.last_sonar = None
        self.last_look = None
        # while set, the state is frozen: a step already under way when sensing
        # was paused or inhibited must not switch it, or run an action. The
        # gate makes setting the hold and switching state exclusive, so a
        # switch either finished before the hold or does not happen at all
        self.hold = threading.Event()
        self.gate = threading.Lock()

    @property
    def quiet_seconds(self):
        return 0.0 if self.quiet_since is None else self.clock() - self.quiet_since

    def freeze(self):
        with self.gate:
            self.hold.set()

    def switch(self, state):
        with self.gate:
            if state is self.state or self.hold.is_set():
                return
            self.state = state
            self.quiet_since = None if state is State.PRESENT else self.clock()
            self.missed_at = None
            self.looked_at = None
            self.blind_at = None
            log.info("now %s", state.value)
            if self.on_change:
                self.on_change(state)

    def active(self):
        """Something other than the sensors showed the user is here: input, an unlock."""
        self.restart()
        self.switch(State.PRESENT)

    def restart(self):
        """Forget the current quiet run, as after a pause or a suspend: start counting afresh."""
        self.quiet_since = None if self.state is State.PRESENT else self.clock()
        self.missed_at = None
        self.looked_at = None
        self.blind_at = None

    def look(self):
        seen = self.camera.look()  # returns at once, blind, if held meanwhile
        self.last_look = seen
        log.debug(
            "camera: faces=%d score=%.2f near=%.0f%% blind=%s",
            seen.faces, seen.score, seen.near_pct, seen.blind,
        )
        return seen

    def step(self):
        if self.hold.is_set():
            return
        now = self.clock()
        reading = self.sonar.probe()
        if self.hold.is_set():
            return  # paused or inhibited during the probe: leave the camera alone
        self.last_sonar = reading
        moving = reading.usable and reading.motion_db > self.threshold_db
        if moving:
            self.quiet_since = None
            self.missed_at = None
        elif self.quiet_since is None:
            self.quiet_since = now
        quiet_s = 0.0 if self.quiet_since is None else now - self.quiet_since
        log.debug(
            "sonar: level=%.1f dB motion=%.1f dB %s",
            reading.level_db, reading.motion_db,
            "moving" if moving else f"quiet {quiet_s:.0f} s" if reading.usable else "unusable",
        )
        if self.state is State.PRESENT:
            self.step_present(now, quiet_s, reading.usable)
        else:
            self.step_away(now, quiet_s, moving, reading.usable)

    def step_present(self, now, quiet_s, sonar_usable):
        if quiet_s < self.quiet_for:
            return
        sonar_says_away = sonar_usable and quiet_s >= BLIND_PATIENCE * self.quiet_for
        if self.blind_at is not None and now - self.blind_at < self.quiet_for:
            # the camera was blind a moment ago (shutter closed?): rely on the
            # sonar for now rather than blink the camera every probe
            if sonar_says_away:
                self.switch(State.AWAY)
            return
        if self.missed_at is not None and now - self.missed_at < self.recheck_after:
            return
        seen = self.look()
        if seen.blind:
            self.blind_at = now
            if sonar_says_away:
                self.switch(State.AWAY)
            return
        self.blind_at = None
        if seen.faces:
            # sitting still: wait out another full quiet period before looking again
            self.quiet_since = now
            self.missed_at = None
        elif self.missed_at is None:
            self.missed_at = now
        else:
            self.switch(State.AWAY)

    def step_away(self, now, quiet_s, moving, sonar_usable):
        if moving:
            # people walking past should not blink the camera every probe
            if self.looked_at is not None and now - self.looked_at < self.recheck_after:
                return
        elif sonar_usable or quiet_s < self.quiet_for:
            return
        self.looked_at = now
        if not moving:
            self.quiet_since = now  # without sonar, look again after another quiet period
        seen = self.look()
        if seen.faces or (seen.blind and moving):
            self.switch(State.PRESENT)
