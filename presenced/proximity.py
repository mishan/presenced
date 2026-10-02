"""A laptop's own human presence sensor, read through the kernel's IIO interface.

Some laptops carry a dedicated presence sensor, often time of flight, for
Windows to lock on walking away. hid-sensor-prox exposes it as an IIO device
named "prox": in_proximity0_raw is nonzero while someone is in front of the
laptop (in_proximity_raw on kernels that only knew that one channel), and
in_proximity1_raw, where there is one, is the distance to them. Each read
powers the sensor up and down again, so it costs nothing between ticks.

A read waits up to five seconds for the sensor to answer, and if it does not,
the kernel reports 0, as if nobody were there. A read that took that long
counts as unknown instead.
"""
import logging
import time
from pathlib import Path

from .sensor import Reading, Verdict

log = logging.getLogger("presenced")

IIO = Path("/sys/bus/iio/devices")
PRESENCE = ("in_proximity0_raw", "in_proximity_raw")
DISTANCE = "in_proximity1_raw"
NO_ANSWER_S = 4.5  # the kernel gives up on the sensor after 5 s


def presence_file(path):
    return next((path / name for name in PRESENCE if (path / name).exists()), None)


def find_sensor(device="", root=None):
    """The presence channel of the configured or the first prox device; LookupError if none."""
    root = root or IIO
    if device:
        candidates = [Path(device) if "/" in device else root / device]
    else:
        candidates = sorted(root.glob("iio:device*"))
    for path in candidates:
        try:
            name = (path / "name").read_text().strip()
        except OSError:
            continue
        found = presence_file(path)
        if found and (device or name == "prox"):
            return found
    raise LookupError(f"{device} has no presence channel" if device else "no presence sensor")


class ProximitySensor:
    name = "proximity"
    role = "poll"

    def __init__(self, device="", root=None, clock=time.monotonic):
        self.device, self.root, self.clock = device, root, clock
        self.presence = find_sensor(device, root)

    def hold(self):
        pass  # a read holds neither the speaker nor the camera, so there is nothing to free

    def release(self):
        pass

    def describe(self):
        return str(self.presence.parent)

    def read(self):
        started = self.clock()
        try:
            presence = int(self.presence.read_text())
        except (OSError, ValueError) as error:
            log.debug("proximity: %s", error)
            try:
                # the sensor hub may have come back as another device, after a resume, say
                self.presence = find_sensor(self.device, self.root)
            except LookupError:
                pass
            return Reading(Verdict.UNKNOWN)
        if self.clock() - started >= NO_ANSWER_S:
            log.debug("proximity: no answer from the sensor")
            return Reading(Verdict.UNKNOWN)
        detail = {"presence": presence}
        try:
            detail["distance"] = int((self.presence.parent / DISTANCE).read_text())
        except (OSError, ValueError):
            pass  # not every sensor measures it
        return Reading(Verdict.PRESENT if presence else Verdict.ABSENT, detail)
