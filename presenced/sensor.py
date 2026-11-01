"""What every sensor reduces its readings to, so the monitor can weigh them alike.

A sensor is either polled every tick ("poll": cheap, like the sonar) or read
only once the poll sensors have been quiet for a while ("look": expensive, like
the camera). Each has a name, a role, read() returning a Reading, and hold()
and release(), which cut a read under way short and keep it from starting
while sensing is paused or inhibited.
"""
import enum
from dataclasses import dataclass, field


class Verdict(enum.Enum):
    PRESENT = "present"  # someone is here: motion, a face, a target
    QUIET = "quiet"  # saw nothing, but cannot rule out someone sitting still
    ABSENT = "absent"  # nobody is here
    UNKNOWN = "unknown"  # could not tell: muted, blind, device gone


@dataclass
class Reading:
    verdict: Verdict
    detail: dict = field(default_factory=dict)  # sensor-specific numbers, for status and logs
