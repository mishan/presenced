"""What the desktop knows: time since the last input, whether the screen is locked,
and whether a program has asked it not to go idle."""
import logging
import os

from dbus_fast import Message, MessageType

log = logging.getLogger("presenced")

# (bus name, object, interface, method, seconds per unit), tried in order
IDLE_SOURCES = [
    ("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
     "org.gnome.Mutter.IdleMonitor", "GetIdletime", 0.001),
    ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
     "org.freedesktop.ScreenSaver", "GetSessionIdleTime", 1.0),
]

LOGIND = "org.freedesktop.login1"

GNOME_INHIBIT_IDLE = 8  # the "don't go idle" flag of GNOME's session manager inhibitors


async def call(bus, destination, path, interface, member, signature="", body=()):
    """The reply body of a method call, or None if the call failed."""
    reply = await bus.call(
        Message(
            destination=destination,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=list(body),
        )
    )
    if reply is None or reply.message_type == MessageType.ERROR:
        return None
    return reply.body


async def get_property(bus, destination, path, interface, name):
    body = await call(
        bus, destination, path, "org.freedesktop.DBus.Properties", "Get", "ss", (interface, name)
    )
    return None if body is None else body[0].value


class Desktop:
    def __init__(self, session_bus, system_bus):
        self.session_bus = session_bus
        self.system_bus = system_bus
        self.idle_source = None
        self.warned_no_idle = False

    async def idle_seconds(self):
        """Seconds since the last keyboard or mouse input, or None if nobody says."""
        sources = [self.idle_source] if self.idle_source else IDLE_SOURCES
        for source in sources:
            destination, path, interface, method, unit = source
            body = await call(self.session_bus, destination, path, interface, method)
            if body is not None:
                if self.idle_source is None:
                    log.info("input idle time from %s", destination)
                self.idle_source = source
                return body[0] * unit
        if self.idle_source is not None:
            self.idle_source = None  # it went away; look again next time
            return await self.idle_seconds()
        if not self.warned_no_idle:
            log.warning("no input idle time available; sensing runs even while you type")
            self.warned_no_idle = True
        return None

    async def session(self):
        """Id and object path of the user's graphical session, or (None, None)."""
        user = await call(
            self.system_bus, LOGIND, "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager", "GetUser", "u", (os.getuid(),),
        )
        if user is None:
            return None, None
        display = await get_property(
            self.system_bus, LOGIND, user[0], "org.freedesktop.login1.User", "Display"
        )
        return (display[0], display[1]) if display and display[1] != "/" else (None, None)

    async def locked(self):
        """Whether the graphical session is locked; False if that cannot be told."""
        _, path = await self.session()
        if path is None:
            return False
        hint = await get_property(
            self.system_bus, LOGIND, path, "org.freedesktop.login1.Session", "LockedHint"
        )
        return bool(hint)


    async def idle_inhibited(self):
        """Whether a program asked the desktop not to go idle, as video players do."""
        body = await call(
            self.session_bus, "org.gnome.SessionManager", "/org/gnome/SessionManager",
            "org.gnome.SessionManager", "IsInhibited", "u", (GNOME_INHIBIT_IDLE,),
        )
        if body is None:
            body = await call(
                self.session_bus, "org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit",
                "org.freedesktop.PowerManagement.Inhibit", "HasInhibit",
            )
        return bool(body and body[0])
