"""Wake the screen the way a person would: nudge the mouse.

Turning the display's power on is not enough: a locked GNOME session keeps a
black curtain over the screen until it sees input, and it will not blank the
screen again later unless it saw input to begin with. A virtual mouse that
moves one pixel and back counts as real input to every compositor, so the lock
screen comes up and later goes dark again on the desktop's own schedule.

This needs write access to /dev/uinput. Many distributions grant it to the
logged-in user already (Steam's udev rules do, for example); otherwise see the
README for a udev rule.
"""
import asyncio
import fcntl
import logging
import os
import struct
import time

log = logging.getLogger("presenced")

UINPUT = "/dev/uinput"
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y = 0, 1
BTN_LEFT = 0x110
BUS_VIRTUAL = 0x06
SETTLE_S = 1.0  # the compositor has to notice the new device before it listens to it


def nudge():
    """Move a virtual mouse one pixel right and back."""
    fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
    try:
        # both axes and a button, or it is not taken for a mouse at all
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_LEFT)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
        fcntl.ioctl(fd, UI_SET_RELBIT, REL_X)
        fcntl.ioctl(fd, UI_SET_RELBIT, REL_Y)
        name = b"presenced wake".ljust(80, b"\0")
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", BUS_VIRTUAL, 0, 0, 1, name, 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        try:
            time.sleep(SETTLE_S)
            for step in (1, -1):
                os.write(fd, struct.pack("llHHi", 0, 0, EV_REL, REL_X, step))
                os.write(fd, struct.pack("llHHi", 0, 0, EV_SYN, 0, 0))
                time.sleep(0.05)
            time.sleep(0.2)
        finally:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
    finally:
        os.close(fd)


class Waker:
    def __init__(self, command=""):
        self.command = command
        self.warned = False

    async def wake(self):
        """Wake the screen; whether that worked."""
        if self.command:
            process = await asyncio.create_subprocess_shell(self.command)
            return await process.wait() == 0
        try:
            await asyncio.to_thread(nudge)
            return True
        except OSError as error:
            if not self.warned:
                log.warning(
                    "cannot wake the screen: %s: %s (see the README for a udev rule, or set wake.command)",
                    UINPUT, error.strerror,
                )
                self.warned = True
            return False
