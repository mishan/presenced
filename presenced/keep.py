"""Hold the desktop's idle timers off while someone is known to be there.

The desktop goes by keyboard and mouse alone, so it would lock on someone
sitting still. An idle inhibitor stops that without touching the idle time, and
it goes with the connection, so a presenced that dies cannot keep a laptop awake.
"""
import asyncio
import logging
import time

from dbus_fast import Message, MessageType

log = logging.getLogger("presenced")

SCREENSAVER = "org.freedesktop.ScreenSaver"
XFCE_SAVER = ("org.xfce.ScreenSaver", "/org/xfce/ScreenSaver", "org.xfce.ScreenSaver")
POWER = (
    "org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit",
    "org.freedesktop.PowerManagement.Inhibit",
)
# the first group any of which answers is taken whole. The first two are the
# specification's path and the one older desktops have; on Xfce the screen lock
# and suspending listen to separate services
TARGETS = (
    ((SCREENSAVER, "/org/freedesktop/ScreenSaver", SCREENSAVER),),
    ((SCREENSAVER, "/ScreenSaver", SCREENSAVER),),
    (XFCE_SAVER, POWER),
)
APP = "presenced"
REASON = "Someone is at the laptop"
CALL_TIMEOUT_S = 5.0


class Keeper:
    def __init__(self, bus, keep_for, clock=time.monotonic):
        self.bus = bus
        self.keep_for = keep_for  # a hold nobody renews for this long ends: the sensing may have wedged
        self.clock = clock
        self.wanted = False
        self.holds = []  # (target, cookie)
        self.taking = None  # the Inhibit call under way; it outlives a want that stops waiting for it
        self.deadline = None
        self.ending = None
        self.released_at = None  # the desktop may restart its idle count at this moment
        self.warned = False

    @property
    def held(self):
        return bool(self.holds)

    async def want(self, wanted):
        """Take or give back the inhibitor; waits a bounded time, and never raises."""
        self.wanted = wanted
        if self.deadline:
            self.deadline.cancel()
            self.deadline = None
        if not wanted:
            await self.release()
            return
        self.deadline = asyncio.get_running_loop().call_later(self.keep_for, self.expired)
        if not self.holds and self.taking is None:
            self.taking = asyncio.ensure_future(self.take())
            _, pending = await asyncio.wait({self.taking}, timeout=CALL_TIMEOUT_S)
            if pending:
                log.warning("the desktop's idle inhibition is not answering")

    def lost(self, name):
        """A service came or went, so a hold may be only half what it could be: start afresh.

        An Inhibit under way stays: a reply now comes from the new owner.
        """
        if any(target[0] == name for group in TARGETS for target in group):
            self.let_go()

    def expired(self):
        log.warning("no check renewed the idle inhibitor for %g s; giving it back", self.keep_for)
        self.wanted = False
        self.let_go()

    async def ask(self, target, member, signature, body):
        """The reply body, or None if the call failed."""
        try:
            destination, path, interface = target
            reply = await self.bus.call(Message(
                destination=destination, path=path, interface=interface,
                member=member, signature=signature, body=list(body),
            ))
        except Exception as error:  # the bus is going, and the daemon with it
            log.debug("%s failed: %s", member, error)
            return None
        if reply is None or reply.message_type == MessageType.ERROR:
            return None
        return reply.body

    async def take(self):
        taken = []
        for group in TARGETS:
            for target in group:
                body = await self.ask(target, "Inhibit", "ss", (APP, REASON))
                if body:
                    taken.append((target, body[0]))
            if taken:
                break
        self.taking = None
        if not taken:
            if not self.warned:
                log.warning("the desktop offers no idle inhibition; it will go idle as usual")
                self.warned = True
        elif self.wanted:
            self.holds = taken
            log.info("keeping the desktop awake")
        else:
            await self.give_back(taken)

    def drop(self):
        holds, self.holds = self.holds, []
        if holds:
            log.info("no longer keeping the desktop awake")
        return holds

    def let_go(self):
        self.ending = asyncio.ensure_future(self.give_back(self.drop()))

    async def release(self):
        await self.give_back(self.drop())

    async def give_back(self, holds):
        if holds:
            self.released_at = self.clock()
        asks = (self.ask(target, "UnInhibit", "u", (cookie,)) for target, cookie in holds)
        try:
            await asyncio.wait_for(asyncio.gather(*asks), CALL_TIMEOUT_S)
        except TimeoutError:
            log.debug("UnInhibit was not answered")
