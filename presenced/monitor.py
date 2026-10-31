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
KEEP_LOOKS = 2.5  # a face counts for this many look intervals and a retry, so one missed look does not end it
BLIND = Reading(Verdict.UNKNOWN)


class State(enum.Enum):
    PRESENT = "present"
    AWAY = "away"


class Monitor:
    def __init__(self, sensors, quiet_for, recheck_after, on_change=None, clock=time.monotonic, look_every=None):
        self.sensors = sensors
        self.polls = [sensor for sensor in sensors if sensor.role == "poll"]
        self.looks = [sensor for sensor in sensors if sensor.role == "look"]
        self.quiet_for = quiet_for
        self.recheck_after = recheck_after
        self.look_every = look_every  # least time between looks while there is motion; None: only when quiet
        self.on_change = on_change
        self.clock = clock
        self.state = State.PRESENT
        self.quiet_since = None  # start of the current unbroken run of quiet probes
        self.missed_at = None  # when the camera last came up empty in this run
        self.looked_at = None  # when the look sensors were last asked
        self.blind_at = None  # when the look sensors last saw nothing at all
        self.seen_at = None  # when a look last found someone in this run
        self.sure_at = None  # the same, counting the forced looks, which must not keep other sensors from deciding
        self.since = clock()  # when this run began
        self.retry_at = None  # when to try again after a forced look missed
        self.absent_since = None  # start of an unbroken run of poll sensors saying nobody is here
        self.away_wait = recheck_after  # least time between looks for motion while away; grows with misses
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
        since = self.quiet_since  # once: a step in the sensing thread may clear it meanwhile
        return 0.0 if since is None else self.clock() - since

    @property
    def attended(self):
        """Whether a face was seen lately, the only thing that may hold the desktop off idling."""
        sure = self.sure_at  # once: a step in the sensing thread may clear it meanwhile
        return (
            self.look_every is not None
            and not self.hold.is_set()
            and sure is not None
            and self.clock() - sure < KEEP_LOOKS * self.look_every + self.recheck_after
        )

    def freeze(self):
        with self.gate:
            self.hold.set()

    def switch(self, state):
        with self.gate:
            if state is self.state or self.hold.is_set():
                return
            self.state = state
            self.restart()
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
        self.sure_at = None
        self.since = self.clock()
        self.retry_at = None
        self.absent_since = None
        self.away_wait = self.recheck_after

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
        if not absent:
            self.absent_since = None
        elif self.absent_since is None:
            self.absent_since = now
        usable = bool(verdicts - {Verdict.UNKNOWN})
        if moving:
            self.quiet_since = None
            self.missed_at = None
        elif self.quiet_since is None:
            self.quiet_since = now
        quiet_s = 0.0 if self.quiet_since is None else now - self.quiet_since
        if self.state is State.PRESENT:
            self.step_present(now, quiet_s, usable, absent)
        else:
            self.step_away(now, quiet_s, moving, usable)

    def look_due(self, now):
        if self.look_every is None or not self.looks:
            return False
        if self.retry_at is not None:
            return now >= self.retry_at
        if self.looked_at is None:
            return now - self.since >= min(self.quiet_for, self.look_every)
        return now - self.looked_at >= self.look_every

    def forced_look(self, now):
        """Ask whether the face is still there, though the poll sensors are not quiet."""
        retry = self.retry_at is None
        self.retry_at = None
        self.looked_at = now
        if self.look().verdict is Verdict.PRESENT:
            self.sure_at = now
        elif retry:  # a turned head should not cost a whole interval, but an empty desk is not stared at
            self.retry_at = now + self.recheck_after

    def step_present(self, now, quiet_s, usable, absent):
        if quiet_s < self.quiet_for:
            if not absent and self.look_due(now):
                self.forced_look(now)
                return
            # a poll sensor that says nobody is here skips the wait, but one that
            # flickers (someone at the edge of its range) must not blink the camera:
            # not within recheck_after of the last look, nor within quiet_for of one
            # that found someone anyway
            if not absent:
                return
            if self.looked_at is not None and now - self.looked_at < self.recheck_after:
                return
            if self.seen_at is not None and now - self.seen_at < self.quiet_for:
                return
        # with nothing able to look, one absent reading is not enough: a presence
        # sensor that times out reads as nobody there, so it has to persist
        polls_say_away = (absent and now - self.absent_since >= self.recheck_after) or (
            usable and quiet_s >= BLIND_PATIENCE * self.quiet_for
        )
        if self.blind_at is not None and now - self.blind_at < self.quiet_for:
            # the camera was blind a moment ago (shutter closed?): rely on the
            # poll sensors for now rather than blink the camera every probe
            if polls_say_away:
                self.switch(State.AWAY)
            return
        if self.missed_at is not None and now - self.missed_at < self.recheck_after:
            return
        self.looked_at = now
        self.retry_at = None
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
            self.seen_at = self.sure_at = now
        elif self.missed_at is None:
            self.missed_at = now
        else:
            self.switch(State.AWAY)

    def step_away(self, now, quiet_s, moving, usable):
        if moving:
            # people walking past should not blink the camera every probe, and a
            # poll sensor that keeps seeing someone the camera does not (beside the
            # desk, say) should not blink it forever: back off while that lasts
            if self.looked_at is not None and now - self.looked_at < self.away_wait:
                return
        else:
            self.away_wait = self.recheck_after
            if usable or quiet_s < self.quiet_for:
                return
        self.looked_at = now
        if not moving:
            self.quiet_since = now  # without poll sensors, look again after another quiet period
        seen = self.look().verdict
        if seen is Verdict.PRESENT or (seen is Verdict.UNKNOWN and moving):
            self.switch(State.PRESENT)
            # the switch forgets this run; keep this look, so the next waits its turn
            self.looked_at = now
            if seen is Verdict.PRESENT:
                self.seen_at = self.sure_at = now
        elif moving:
            # double the wait up to quiet_for, but never below recheck_after
            self.away_wait = max(self.away_wait, min(2 * self.away_wait, self.quiet_for))
