# Sensors: design

Status: proposal. Nothing here is implemented yet.

presenced knows about two sensors, the sonar and the IR camera, and `Monitor`
takes them by name. This proposes a common interface so that other ways of
sensing presence (an mmWave radar, a phone or watch in Bluetooth range, a
program reporting over D-Bus) can sit alongside them, without changing how the
sonar and camera behave today.

## Roles

`Monitor` already treats its two sensors in two different ways, and those
generalize:

- **poll**: cheap, read every tick. The sonar.
- **look**: expensive, read only when the poll sensors have been quiet for a
  while, and again `recheck_after` later. The camera.

## Verdicts

What does not generalize is the readings: the sonar reports motion in dB, the
camera reports faces. Every sensor instead reduces its reading to a verdict:

```python
class Verdict(enum.Enum):
    PRESENT = "present"   # someone is here: motion, a face, a radar target
    QUIET = "quiet"       # saw nothing, but cannot rule out someone sitting still
    ABSENT = "absent"     # nobody is here
    UNKNOWN = "unknown"   # could not tell: muted, shutter closed, device gone


@dataclass
class Reading:
    verdict: Verdict
    detail: dict  # sensor-specific numbers, for GetStatus and verbose logs


class Sensor(Protocol):
    name: str  # "sonar", "camera"; also the name of its config table
    role: Literal["poll", "look"]

    def read(self, abort: threading.Event) -> Reading: ...
```

How the sensors we have, and the likely next ones, map onto it:

| Sensor | Role | PRESENT | QUIET | ABSENT | UNKNOWN |
|---|---|---|---|---|---|
| sonar | poll | motion | still | never | muted, unusable |
| IR camera | look | a face near enough | (not used) | no face | blind |
| mmWave radar | poll | a target, still or moving | (not used) | no target | device gone |
| Bluetooth phone or watch | poll | never | in range | out of range | adapter off |
| laptop presence sensor (time of flight) | poll | someone in range | (not used) | nobody in range | no such device |
| security key or smart card | poll | never | inserted | removed | (not used) |
| chair occupancy (pressure pad or load cell, via `Report`) | poll | sat on | (not used) | empty | no report |
| thermal array (via `Report`) | poll | a warm body in front | (not used) | none | no report |
| time-of-flight sensor on a monitor (via `Report`) | poll | someone at the desk | (not used) | nobody | no report |
| a command (a Home Assistant entity, say) | either | prints `present` | prints `quiet` | prints `absent` | fails, times out |

The sonar can rule a person in but never out; Bluetooth can rule a person out
but never in. Both fit the same four values. A chair sensor is the rare one
that is good both ways, and fast: it knows the moment you stand up or sit down.
What it cannot tell is who is sitting, or whether it is a person at all; a cat
or a bag on the seat reads as present.

Some recent laptops have a dedicated presence sensor, often time of flight,
for Windows to lock on walking away. Linux exposes these as IIO devices
through `hid-sensor-prox`. Where there is one, it does the sonar and camera's
job better: it is cheap enough to read constantly, and it sees someone sitting
still. It is the one new sensor worth reading natively rather than through
`Report`.

What is known about it so far:

- Linux 5.14 added the presence sensor behind AMD's Sensor Fusion Hub
  ([Phoronix](https://www.phoronix.com/news/Linux-5.14-HID)). Patches from late
  2024 added distance and "attention" channels to `hid-sensor-prox`, for the
  EgisVision 620
  ([series](https://lkml.iu.edu/2410.3/05467.html)).
- On the ThinkPad X9-15p Gen 1, an ST VL53L5CX shows up as the `prox` IIO
  device and gives presence (0 or 1), distance in mm and attention (0 or 100)
  about ten times a second. It stays in standby until something enables its
  buffer, and draws power while it ranges. Presence and distance are reliable;
  attention follows head orientation, not gaze, and is not
  ([lenovo-x9-15p-linux-firmware](https://github.com/sigfridvonshrink/lenovo-x9-15p-linux-firmware)).
- Reading the values through sysfs is a synchronous firmware call of about
  100 ms. The driver's FIFO delivers reports at the sampling interval whether
  or not anything changed. Event-driven notification was being discussed on
  linux-iio in July 2026, with walk-away lock as the use case
  ([thread](https://ratatoskr.run/linux-iio/2026/07/17313445/t)).
- No userspace program turns any of this into a walk-away lock yet. The
  Linux walk-away locks that exist go by Bluetooth, and `iio-sensor-proxy`
  does not expose these sensors.

So for presenced: presence maps straight to `PRESENT` or `ABSENT`, and distance
can filter out people across the room as `min_face_width` does for the camera.
Attention is best ignored. One sysfs read per tick is fast enough; the FIFO is
the way to react at once. The buffer should only be enabled while sensing,
not while paused, inhibited or while there is input, since the sensor costs
power while it runs. Neither of the machines this was written on has one, so
it needs a tester who does.

A security key is the classic "lock on smart card removal": pulling it out
means you have left. Inserted means nothing, since a key can stay in all day.
Removal arrives as a udev event, which makes it a case for both open questions
below: it should decide on its own, and at once.

### Desktops

A desktop usually has no IR camera and no built-in speaker and microphones
side by side. Three ways to give it the same footing:

- **A thermal array** (MLX90640, Grid-EYE) facing the chair sees a still body
  by its warmth, and never captures an image of anyone. It is the desktop
  counterpart of the IR camera, without a camera.
- **A time-of-flight sensor** (VL53L1X) on the monitor, facing the chair, is a
  homemade version of the laptop presence sensor. Either sensor, on a
  microcontroller, reaches presenced through `Report`.
- **The sonar itself** may work with desktop speakers and a webcam's
  microphone, if both pass 20 kHz. `sonar.py` would carry over, but it only
  plays to a built-in speaker today; external speakers would need an explicit
  opt-in, so that the tone never goes to headphones by accident.

`abort` is the monitor's hold event. When it is set (sensing paused or
inhibited) a read must return `UNKNOWN` promptly, so that `Pause` and `Inhibit`
can reply only once the speaker and camera are free, as they do now.

## Deciding

These rules give exactly today's behavior with the sonar and camera.

While **present**:

- Any poll sensor `PRESENT` restarts the quiet count.
- Once every poll sensor has been `QUIET` or `UNKNOWN` for `quiet_for`, the
  look sensors are read. Any `PRESENT` restarts the count; `ABSENT` twice,
  `recheck_after` apart, means away.
- A poll sensor `ABSENT` skips the rest of the quiet wait and goes straight to
  the looks. Only if no look sensor can see does it mean away on its own.
- `PRESENT` beats `ABSENT`. When the phone has left but someone is moving at
  the laptop, someone is there. presenced is a convenience, not a security
  boundary, and erring toward present is the safe failure.
- If every look sensor is blind, the poll sensors decide alone after
  `BLIND_PATIENCE` quiet periods, as now.

While **away**:

- A poll sensor `PRESENT` asks for a look, no more than once per
  `recheck_after`, so people walking past do not blink the camera every tick.
- A look `PRESENT` means present. With no look sensor able to see, a poll
  sensor `PRESENT` is enough.

Input, unlocking, idle inhibitors and the lock screen are not sensors. They
decide whether sensing runs at all, and stay in `Daemon` and `Desktop`.

Waking the screen follows any look sensor's `PRESENT`. The camera's
`min_face_width` check moves inside its own verdict, so a face across the room
is `ABSENT` to the monitor.

## Reporting over D-Bus

A new method on `io.github.mishan.Presenced1`:

| Member | Type | |
|---|---|---|
| `Report(s source, s verdict, d valid_for)` | method | `present`, `quiet`, `absent` or `unknown`, good for `valid_for` seconds |

Each source becomes a poll sensor that returns its last report until
`valid_for` runs out, then `UNKNOWN`. This is the main way new hardware gets
in. An mmWave radar on an ESP32 talks to Home Assistant or a serial port, not
to presenced, and a short script around `busctl` gets it here without any
driver code in this repository:

    busctl --user call io.github.mishan.Presenced /io/github/mishan/Presenced \
        io.github.mishan.Presenced1 Report ssd desk-radar present 10

Unlike inhibitors, reports outlive their caller, since `busctl` exits at once;
`valid_for` is what keeps a crashed reporter from holding anyone present.

## Running a command

`Report` is for things that can call presenced. For things presenced has to
ask, a command sensor runs a shell command and reads a verdict from the first
word it prints: `present`, `quiet`, `absent` or `unknown`. Anything else, a
nonzero exit, or running past `timeout` is `UNKNOWN`. A command that hangs is
killed with its process group, as actions are.

```toml
[command.chair]
role = "poll"
run = "~/.config/presenced/chair.sh"
every = 10      # seconds between runs; the last verdict stands in between
timeout = 5
```

with, for a chair sensor that Home Assistant already knows about:

```sh
#!/bin/sh
curl -sf -H "Authorization: Bearer $(cat ~/.config/presenced/ha-token)" \
    http://homeassistant.local:8123/api/states/binary_sensor.desk_chair |
    jq -r 'if .state == "on" then "present" elif .state == "off" then "absent" else "unknown" end'
```

`every` keeps a slow or rate-limited source from being asked every tick. A
command with `role = "look"` is run only when the monitor wants a look, which
suits a check that is slow or has side effects. Each `[command.<name>]` table
is its own sensor, named `command.<name>` in status and logs.

## Registry and configuration

An in-tree registry maps names to factories:

```python
SENSORS = {"sonar": sonar.make, "camera": ir.make}
```

Each sensor module owns its settings dataclass, their range checks and its
`[name]` table; `config.py` walks the registry instead of naming the tables.
`[sonar]` and `[camera]` keep their current keys, so existing configuration
files keep working. A factory returns `None` when its sensor is turned off or
missing, and logs why, as `make_camera` does now.

Third-party sensors written in Python could register through a
`presenced.sensors` entry point. That waits until someone needs it; `Report`
and command sensors cover most of the same ground without tying anyone to
presenced's internals.

## Status

`GetStatus` reports each sensor under its name instead of fixed sonar and
camera keys:

    {"sensors": {"sonar": {"verdict": "quiet", "level_db": -31.2, "motion_db": -40.5}, ...}}

presenced is early enough that this changes in place, along with
`presencectl status`. `presencectl sonar`, `presencectl camera` and
`presencectl calibrate` stay as they are; they are tools for those sensors,
not for sensing in general.

## Order of work

1. `Verdict`, `Reading` and `Sensor`, with `Sonar` and `IRCamera` wrapped to
   fit. `Monitor` takes lists of sensors. No change in behavior: the monitor
   tests pass with only their construction changed.
2. The registry, and configuration per sensor.
3. `GetStatus` and `presencectl status` per sensor.
4. `Report` and command sensors, the first new ones.
5. Bluetooth, from BlueZ's `Connected` property for a paired device. Signal
   strength is too noisy to poll.

## Open questions

- **Can a trusted poll sensor decide alone?** A radar's `ABSENT` is far
  stronger than a quiet sonar, and a chair going empty or a security key
  pulled is stronger still. A per-sensor `decisive = true` would let it switch
  state without a look: lock when you stand up, wake when you sit down. Left
  out of the first version.
- **Waking early.** A Bluetooth disconnect, a key removed or a `Report` (a
  chair going empty, say) could set the daemon's `poke` to run a tick at once
  rather than at the next interval. Cheap, but not needed to start.
- **Names.** "poll" and "look" describe how the monitor uses a sensor; "watch"
  and "confirm" might read better in configuration and docs.
