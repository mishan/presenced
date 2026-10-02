"""Decide between present and away from what the sensors say.

Poll sensors (the sonar) are the cheap, frequent check. The sonar only sees
motion, and a person can hold still for a while, so a single quiet reading
means nothing: the poll sensors have to stay quiet for a sustained period
before the look sensors (the IR camera) are asked whether a face is still
there, and they have to miss twice, some time apart, before the user counts as
away.
"""
import enum
import logging
import threading
import time

from .sensor import Reading, Verdict

log = logging.getLogger("presenced")

BLIND_PATIENCE = 3  # with the look sensors blind, wait this many times longer on the poll sensors alone
BLIND = Reading(Verdict.UNKNOWN)


class State(enum.Enum):
    PRESENT = "present"
    AWAY = "away"


class Monitor:
    def __init__(self, sensors, quiet_for, recheck_after, on_change=None, clock=time.monotonic):
        self.sensors = sensors
        self.polls = [sensor for sensor in sensors if sensor.role == "poll"]
        self.looks = [sensor for sensor in sensors if sensor.role == "look"]
        self.quiet_for = quiet_for
        self.recheck_after = recheck_after
        self.on_change = on_change
        self.clock = clock
        self.state = State.PRESENT
        self.quiet_since = None  # start of the current unbroken run of quiet probes
        self.missed_at = None  # when the camera last came up empty in this run
        self.looked_at = None  # when the camera was last asked, while away
        self.blind_at = None  # when the look sensors last saw nothing at all
        self.seen_at = None  # when a look last found someone in this run
        self.readings = {}  # the latest reading of each sensor, by name
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
            self.seen_at = None
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
        self.seen_at = None

    def read(self, sensor):
        reading = sensor.read()
        self.readings[sensor.name] = reading
        log.debug(
            "%s: %s %s", sensor.name, reading.verdict.value,
            " ".join(f"{key}={value:.1f}" if isinstance(value, float) else f"{key}={value}"
                     for key, value in reading.detail.items()),
        )
        return reading.verdict

    def look(self):
        """PRESENT if any look sensor sees someone, ABSENT if any sees nobody, else UNKNOWN."""
        verdicts = set()
        for sensor in self.looks:
            if self.hold.is_set():
                break
            verdicts.add(self.read(sensor))  # returns at once, UNKNOWN, if held meanwhile
        for verdict in (Verdict.PRESENT, Verdict.ABSENT):
            if verdict in verdicts:
                self.last_look = Reading(verdict)
                return self.last_look
        self.last_look = BLIND
        return BLIND

    def step(self):
        if self.hold.is_set():
            return
        now = self.clock()
        verdicts = set()
        for sensor in self.polls:
            verdicts.add(self.read(sensor))
            if self.hold.is_set():
                return  # paused or inhibited during the read: leave the look sensors alone
        moving = Verdict.PRESENT in verdicts
        # someone moving at the laptop is there, whatever else says they left
        absent = not moving and Verdict.ABSENT in verdicts
        usable = bool(verdicts - {Verdict.UNKNOWN})
        if moving:
            self.quiet_since = None
            self.missed_at = None
            self.seen_at = None
        elif self.quiet_since is None:
            self.quiet_since = now
        quiet_s = 0.0 if self.quiet_since is None else now - self.quiet_since
        if self.state is State.PRESENT:
            self.step_present(now, quiet_s, usable, absent)
        else:
            self.step_away(now, quiet_s, moving, usable)

    def step_present(self, now, quiet_s, usable, absent):
        # a poll sensor that says nobody is here skips the wait, unless a look
        # has since found someone: then it is someone sitting still, as with the sonar
        if quiet_s < self.quiet_for and (not absent or self.seen_at is not None):
            return
        polls_say_away = absent or (usable and quiet_s >= BLIND_PATIENCE * self.quiet_for)
        if self.blind_at is not None and now - self.blind_at < self.quiet_for:
            # the camera was blind a moment ago (shutter closed?): rely on the
            # poll sensors for now rather than blink the camera every probe
            if polls_say_away:
                self.switch(State.AWAY)
            return
        if self.missed_at is not None and now - self.missed_at < self.recheck_after:
            return
        seen = self.look().verdict
        if seen is Verdict.UNKNOWN:
            self.blind_at = now
            if polls_say_away:
                self.switch(State.AWAY)
            return
        self.blind_at = None
        if seen is Verdict.PRESENT:
            # sitting still: wait out another full quiet period before looking again
            self.quiet_since = now
            self.missed_at = None
            self.seen_at = now
        elif self.missed_at is None:
            self.missed_at = now
        else:
            self.switch(State.AWAY)

    def step_away(self, now, quiet_s, moving, usable):
        if moving:
            # people walking past should not blink the camera every probe
            if self.looked_at is not None and now - self.looked_at < self.recheck_after:
                return
        elif usable or quiet_s < self.quiet_for:
            return
        self.looked_at = now
        if not moving:
            self.quiet_since = now  # without poll sensors, look again after another quiet period
        seen = self.look().verdict
        if seen is Verdict.PRESENT or (seen is Verdict.UNKNOWN and moving):
            self.switch(State.PRESENT)
