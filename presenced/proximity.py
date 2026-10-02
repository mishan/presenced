"""A laptop's own human presence sensor, read through the kernel's IIO interface.

Some laptops carry a dedicated presence sensor, often time of flight, for
Windows to lock on walking away. hid-sensor-prox exposes it as an IIO device
named "prox": in_proximity0_raw is nonzero while someone is in front of the
laptop (in_proximity_raw on kernels that only knew that one channel), and
in_proximity1_raw, where there is one, is the distance to them. Each read
powers the sensor up and down again, so it costs nothing between ticks.
"""
import logging
from pathlib import Path

from .sensor import Reading, Verdict

log = logging.getLogger("presenced")

IIO = Path("/sys/bus/iio/devices")
PRESENCE = ("in_proximity0_raw", "in_proximity_raw")
DISTANCE = "in_proximity1_raw"


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

    def __init__(self, device="", root=None):
        self.presence = find_sensor(device, root)
        self.distance = self.presence.parent / DISTANCE

    def hold(self):
        pass  # a read takes a moment and holds nothing

    def release(self):
        pass

    def describe(self):
        return str(self.presence.parent)

    def read(self):
        try:
            presence = int(self.presence.read_text())
        except (OSError, ValueError) as error:
            log.debug("proximity: %s", error)
            return Reading(Verdict.UNKNOWN)
        detail = {"presence": presence}
        try:
            detail["distance"] = int(self.distance.read_text())
        except (OSError, ValueError):
            pass  # not every sensor measures it
        return Reading(Verdict.PRESENT if presence else Verdict.ABSENT, detail)
