# presenced

A presence detection daemon for Linux laptops. It works out whether you are at
your computer using hardware the laptop already has, the speaker, the
microphones and the IR camera, and publishes that on D-Bus. It can run a
command when you leave and when you come back: lock the screen, pause music,
set your chat status, anything.

Early days: developed on a ThinkPad X13 Gen 5 with GNOME and PipeWire. Reports
from other laptops are very welcome.

## How it works

- **Input first.** While you are typing or moving the mouse you are obviously
  there, so nothing else runs. Sensing starts after a few seconds without
  input. Likewise, while a browser or video player has asked the desktop not
  to go idle (during playback), you count as present, just as the desktop
  itself will not blank or lock the screen then.
- **Sonar.** Every few seconds the speaker plays a short, inaudible 20 kHz
  tone and the microphones record it. In an empty room the returned tone holds
  still; a person in front of the laptop makes it wander. This is cheap, but it
  only sees motion, and someone sitting very still reads the same as an empty
  room.
- **IR camera.** So when the sonar has been quiet for a while, the IR camera
  takes a look. Its emitter lights alternate frames; subtracting an unlit frame
  from a lit one keeps only what is close to the screen, and a face detector
  ([YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet))
  decides whether someone is there. The camera's exposure is aimed at the face,
  since a face at arm's length is otherwise washed out.

You count as away when the sonar has stayed quiet for `quiet_for` seconds
(default 30) and the camera then finds no face twice, `recheck_after` seconds
apart (default 10). Any motion or input in between starts the count again. With
the defaults, walking away is noticed after about 45 seconds.

You count as back as soon as there is input, the screen is unlocked, or the
sonar sees motion and the camera sees a face. With the camera blind (the
privacy shutter closed, say), the sonar decides alone: you count as away after
three quiet periods (90 seconds by default) and back on the first motion.

While sensing is paused or inhibited, while a video is playing, and while the
screen is locked (unless waking is on), the state is frozen as it was and no
actions run. Unlocking the screen counts as present, since only a person can
do that. After a suspend, sensing simply starts counting again.

## Requirements

- Linux with PipeWire (`pw-play`, `pw-record` and `pw-dump`)
- `v4l2-ctl` (from v4l-utils) for the IR camera
- Python 3.11 or newer
- A laptop with built-in speakers and microphones that pass 20 kHz (most do;
  `presencectl calibrate` tells you), and ideally an IR camera
- For input idle time: GNOME, or a desktop that implements
  `org.freedesktop.ScreenSaver.GetSessionIdleTime` (KDE). Elsewhere sensing
  also runs while you type.

## Install

    pipx install git+https://github.com/mishan/presenced
    presencectl fetch-model          # the face detector, about 230 KB
    presencectl calibrate --write    # fit the sonar to your laptop and room
    presencectl install-service --enable

Calibration plays some beeps and asks you to leave the room for about half a
minute, so it can measure the empty room.

## Configure

Settings live in `~/.config/presenced/config.toml`. Every setting is optional;
`calibrate --write` creates the file with all of them and their defaults. The
one you will want is an action:

```toml
[actions]
on_away = "loginctl lock-session $PRESENCED_SESSION"
on_present = ""
```

Actions are shell commands. `PRESENCED_STATE` is set to `away` or `present`,
and `PRESENCED_SESSION` to the id of your desktop's login session. presenced
runs outside that session, so a bare `loginctl lock-session` would lock the
wrong one.
Restart the service after editing: `systemctl --user restart presenced`.

## Waking the screen

With `[wake] enabled = true`, presenced keeps watching while the screen is
locked, and when a face comes back to the laptop it wakes the screen, so the
lock screen is already up when you sit down. It only wakes for a face big
enough to be at the laptop (`[camera] min_face_width`), so people walking past
across the room do not light it up.

It wakes the screen the way a person would, by nudging a virtual mouse one
pixel and back. That works on any desktop, and the desktop then dims and locks
again on its own schedule if nobody stays. It needs write access to
`/dev/uinput`, which many distributions give the logged-in user already (Steam's
udev rules do, for example). Check with `getfacl /dev/uinput`; if your user is
not listed, this udev rule grants it to whoever is logged in at the seat:

```
# /etc/udev/rules.d/70-presenced-uinput.rules
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
```

Anything that can write to `/dev/uinput` can type and click as you, so only
add the rule if you are comfortable with that. Alternatively, set
`[wake] command` to your own command.

Presenced cannot wake a laptop that has gone to sleep. Once your desktop
suspends the laptop after being idle, waking on approach stops until you wake
it yourself.

Waking the screen does not unlock it. presenced is not an authentication
method, and never will be; face unlock belongs to tools like
[Howdy](https://github.com/boltgolt/howdy), which work at the lock screen and
need the IR camera to themselves. presenced only uses the camera for under two
seconds at a time, and such a tool can hold presenced off entirely with an
inhibitor (see D-Bus below).

## Use

    presencectl status          # what presenced sees right now
    presencectl watch           # print state changes as they happen
    presencectl pause 1h        # stop sensing for an hour (or until resume)
    presencectl resume
    presencectl inhibit -- zoom # hold sensing off while a program runs
    presencectl sonar           # raw sonar readings, for troubleshooting
    presencectl camera          # raw camera readings

## D-Bus

presenced owns `io.github.mishan.Presenced` on the session bus, with the
object `/io/github/mishan/Presenced` and the interface
`io.github.mishan.Presenced1`:

| Member | Type | |
|---|---|---|
| `State` | property `s` | `present` or `away` |
| `StateSince` | property `d` | Unix time of the last change |
| `Sensing` | property `s` | `input`, `sensing`, `watching` (sensing while locked), `held` (a program asked the desktop not to go idle), `locked`, `paused` or `inhibited` |
| `Paused` | property `b` | |
| `PausedUntil` | property `d` | Unix time, or 0 when not paused or paused until resumed |
| `Inhibitors` | property `a(uss)` | cookie, who, why |
| `StateChanged(s)` | signal | the new state |
| `Pause(d seconds)` | method | 0 means until `Resume` |
| `Resume()` | method | |
| `Inhibit(s who, s why) → u` | method | held until `Uninhibit(cookie)` or until the caller leaves the bus |
| `Uninhibit(u cookie) → b` | method | |
| `GetStatus() → a{sv}` | method | latest readings, for display |

Properties emit `PropertiesChanged`, so a tray icon can follow along without
polling. A pause is meant for people and lasts until its time is up; an
inhibitor is meant for programs and disappears with the program, so one that
crashes cannot leave sensing off.

## Privacy

Audio and camera frames are analyzed in memory and never stored or sent
anywhere. The microphones record only while a tone plays, and the camera runs
for under two seconds per look. The camera LED lights for each look.

## Limits

- **It is a convenience, not a security boundary.** Someone can sit down at
  your laptop in the time before you are counted as away, and nothing checks
  that a face is live, so any face in front of the camera, or perhaps a photo
  of one, keeps you counted as present.
- **Some people and animals can hear 20 kHz**, especially children and
  dogs. `calibrate` picks the highest tone your hardware passes well, which
  helps. `tone_hz` can be set by hand.
- **The tone follows the speaker volume.** When the speaker is muted or
  headphones are plugged in, the sonar cannot hear anything and presenced
  relies on the camera alone. The tone is only ever sent to the laptop's own
  speaker: a sink counts only if PipeWire reports it as a built-in (ALSA)
  output currently playing through a speaker, never headphones, HDMI or
  Bluetooth, whatever the `speaker` setting matches.
- **No IR camera?** presenced runs on the sonar alone, and says so in its log.
- **Sitting very still for a long time** means the camera looks now and then
  (once per `quiet_for`) to make sure you are still there.
- **Only faces count,** and only faces close enough to be at the laptop.
  Looking down at a phone or turning away for the whole quiet period and both
  looks can count as away.

## Development

    python3 -m venv .venv
    .venv/bin/pip install -e '.[dev]'
    .venv/bin/pytest
    .venv/bin/presenced -v -c some-config.toml   # run in the foreground, logging every reading

## License

MIT. The face detection model it downloads is YuNet from OpenCV's model zoo,
also MIT licensed, and is not part of this repository.
