"""A laptop's own human presence sensor, read through the kernel's IIO interface.

Some laptops carry a dedicated presence sensor, often time of flight, for
Windows to lock on walking away. hid-sensor-prox exposes it as an IIO device
named "prox": in_proximity0_raw is nonzero while someone is in front of the
laptop (in_proximity_raw on kernels that only knew that one channel), and
in_proximity1_raw, where there is one, is the distance to them. Each read
powers the sensor up and down again, so it costs nothing between ticks.

A read waits up to five seconds for the sensor to answer, and if it does not,
the kernel reports 0, as if nobody were there. A read that took that long
counts as unknown instead, and a sensor that keeps not answering is left alone
for a while rather than adding five seconds to every tick.
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
GIVE_UP_AFTER = 3  # reads in a row without an answer before a rest
REST_S = 60.0


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
            if name != "prox":
                # IIO numbers can change between boots, so this may be another sensor
                log.warning("%s is named %s, not prox: is it the presence sensor?", path, name)
            return found
    raise LookupError(f"{device} has no presence channel" if device else "no presence sensor")


class ProximitySensor:
    name = "proximity"
    role = "poll"

    def __init__(self, device="", root=None, clock=time.monotonic):
        self.device, self.root, self.clock = device, root, clock
        self.presence = find_sensor(device, root)
        self.measures_distance = True  # until it fails to answer
        self.unanswered = 0
        self.rest_until = None

    def hold(self):
        pass  # a read holds neither the speaker nor the camera, so there is nothing to free

    def release(self):
        pass

    def describe(self):
        return str(self.presence.parent)

    def read(self):
        if self.rest_until is not None and self.clock() < self.rest_until:
            return Reading(Verdict.UNKNOWN)
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
        if not self.answered(started):
            return Reading(Verdict.UNKNOWN)
        detail = {"presence": presence}
        if self.measures_distance:
            started = self.clock()
            try:
                distance = int((self.presence.parent / DISTANCE).read_text())
            except (OSError, ValueError):
                self.measures_distance = False  # not every sensor does
            else:
                if self.clock() - started < NO_ANSWER_S:
                    detail["distance"] = distance
                else:
                    log.info("proximity: no answer for the distance; reading presence alone")
                    self.measures_distance = False
        return Reading(Verdict.PRESENT if presence else Verdict.ABSENT, detail)

    def answered(self, started):
        """Whether the read since started was answered in time; after a few that were not, rest."""
        if self.clock() - started < NO_ANSWER_S:
            self.unanswered = 0
            return True
        self.unanswered += 1
        log.debug("proximity: no answer from the sensor")
        if self.unanswered >= GIVE_UP_AFTER:
            log.info("proximity: no answer %d times in a row; trying again in %g s", self.unanswered, REST_S)
            self.unanswered = 0
            self.rest_until = self.clock() + REST_S
        return False
