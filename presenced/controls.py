"""Ways to switch sensing off for a while.

A pause is for a person: `presencectl pause 1h`, or a tray icon. It lasts
until its time is up or it is resumed, whoever asked for it.

An inhibitor is for a program, such as a video call that wants the camera to
itself. It lasts until the program releases it or leaves the bus, so a program
that crashes cannot leave sensing switched off.
"""
import math
import time
from dataclasses import dataclass


@dataclass
class Inhibitor:
    owner: str  # unique bus name of the program that holds it
    who: str
    why: str


class Controls:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.paused_until = None  # None when not paused, math.inf until resumed
        self.inhibitors = {}
        self.next_cookie = 1

    def pause(self, seconds):
        """Pause for this many seconds, or until resumed if seconds is not positive."""
        self.paused_until = math.inf if seconds <= 0 else self.clock() + seconds

    def resume(self):
        self.paused_until = None

    @property
    def paused(self):
        if self.paused_until is not None and self.clock() >= self.paused_until:
            self.paused_until = None
        return self.paused_until is not None

    def inhibit(self, owner, who, why):
        cookie = self.next_cookie
        self.next_cookie += 1
        self.inhibitors[cookie] = Inhibitor(owner, who, why)
        return cookie

    def uninhibit(self, cookie, owner):
        """Release an inhibitor; only the program that took it may."""
        held = self.inhibitors.get(cookie)
        if held is None or held.owner != owner:
            return False
        del self.inhibitors[cookie]
        return True

    def release_owner(self, owner):
        """Drop every inhibitor a program held; how many there were."""
        gone = [cookie for cookie, held in self.inhibitors.items() if held.owner == owner]
        for cookie in gone:
            del self.inhibitors[cookie]
        return len(gone)

    @property
    def inhibited(self):
        return bool(self.inhibitors)
