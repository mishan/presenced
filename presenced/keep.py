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
PATHS = ("/org/freedesktop/ScreenSaver", "/ScreenSaver")  # the specification's, and the one older desktops have
APP = "presenced"
REASON = "Someone is at the laptop"
CALL_TIMEOUT_S = 5.0


class Keeper:
    def __init__(self, bus, keep_for, clock=time.monotonic):
        self.bus = bus
        self.keep_for = keep_for  # a hold nobody renews for this long ends: the sensing may have wedged
        self.clock = clock
        self.wanted = False
        self.cookie = None
        self.path = None
        self.taking = None  # the Inhibit call under way; it outlives a want that stops waiting for it
        self.deadline = None
        self.ending = None
        self.released_at = None  # the desktop may restart its idle count at this moment
        self.warned = False

    @property
    def held(self):
        return self.cookie is not None

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
        if self.cookie is None and self.taking is None:
            self.taking = asyncio.ensure_future(self.take())
            _, pending = await asyncio.wait({self.taking}, timeout=CALL_TIMEOUT_S)
            if pending:
                log.warning("the desktop's idle inhibition is not answering")

    def lost(self):
        """The service went away, and our inhibitor with it.

        An Inhibit under way stays: a reply now comes from the new owner.
        """
        self.cookie = None

    def expired(self):
        log.warning("no check renewed the idle inhibitor for %g s; giving it back", self.keep_for)
        self.wanted = False
        if self.cookie is not None:
            self.ending = asyncio.ensure_future(self.give_back(self.path, self.drop()))

    async def ask(self, path, member, signature, body):
        """The reply body, or None if the call failed."""
        try:
            reply = await self.bus.call(Message(
                destination=SCREENSAVER, path=path, interface=SCREENSAVER,
                member=member, signature=signature, body=list(body),
            ))
        except Exception as error:  # the bus is going, and the daemon with it
            log.debug("%s failed: %s", member, error)
            return None
        if reply is None or reply.message_type == MessageType.ERROR:
            return None
        return reply.body

    async def take(self):
        for path in PATHS:
            body = await self.ask(path, "Inhibit", "ss", (APP, REASON))
            if body:
                break
        self.taking = None
        if not body:
            if not self.warned:
                log.warning("the desktop offers no idle inhibition; it will go idle as usual")
                self.warned = True
        elif self.wanted:
            self.cookie, self.path = body[0], path
            log.info("keeping the desktop awake")
        else:
            await self.give_back(path, body[0])

    def drop(self):
        cookie, self.cookie = self.cookie, None
        log.info("no longer keeping the desktop awake")
        return cookie

    async def release(self):
        if self.cookie is not None:
            await self.give_back(self.path, self.drop())

    async def give_back(self, path, cookie):
        self.released_at = self.clock()
        try:
            await asyncio.wait_for(self.ask(path, "UnInhibit", "u", (cookie,)), CALL_TIMEOUT_S)
        except TimeoutError:
            log.debug("UnInhibit was not answered")
