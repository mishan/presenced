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
  to go idle (during playback), nothing is sensed and nothing changes, just
  as the desktop itself will not blank or lock the screen then.
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

- **Presence sensor.** Some laptops have a dedicated human presence sensor
  for Windows to lock on walking away. Where Linux exposes one (as an IIO
  device named `prox`, through `hid-sensor-prox`), presenced reads it every few
  seconds alongside the sonar. It sees someone sitting still, so the camera
  rarely needs to look; when it reports nobody there, the camera looks at once
  rather than after the quiet period. This is new and untested on real
  hardware: reports from laptops that have one are especially welcome.

You count as away when the sonar has stayed quiet for `quiet_for` seconds
(default 30) and the camera then finds no face twice, `recheck_after` seconds
apart (default 10). Any motion or input in between starts the count again. With
the defaults, walking away is noticed after about 45 seconds.

You count as back as soon as there is input, the screen is unlocked, or the
sonar sees motion and the camera sees a face. With the camera blind (the
privacy shutter closed, say), the sonar decides alone: you count as away after
three quiet periods (90 seconds by default) and back on the first motion.

The desktop itself only counts the keyboard and mouse, so it would dim, lock and
suspend on you while you read. While the camera has seen your face in the last
five minutes or so, presenced holds those timers off with an idle inhibitor. To
know, it also looks about 30 seconds after your last input and then every two
minutes, so the camera LED lights now and then, even while you are moving. Sonar
motion alone never holds the desktop off, so with the camera blind or missing
the desktop's timers run as usual, and they still apply if presenced stops. If
you leave but something keeps moving, the desktop can take up to about ten
minutes to lock. This works on GNOME, KDE and Xfce, each tested on a real session in a container;
sway and other compositors with no idle inhibition on D-Bus go idle as usual.
Set `keep_awake = false` under `[sensing]` to turn it off. An idle delay under
about 40 seconds can run out before the first look.

While sensing is paused or inhibited, while a video is playing, and while the
screen is locked (unless waking is on), the state is frozen as it was and no
actions run. Unlocking the screen counts as present, since only a person can
do that. After a suspend, sensing simply starts counting again.

## Requirements

- Linux with PipeWire (`pw-play`, `pw-record` and `pw-dump`)
- `v4l2-ctl` (from v4l-utils) for the IR camera
- Python 3.12 or newer
- A laptop with built-in speakers and microphones that pass 20 kHz (most do;
  `presencectl calibrate` tells you), and ideally an IR camera
- For input idle time: GNOME, or a desktop that implements
  `org.freedesktop.ScreenSaver.GetSessionIdleTime`. Elsewhere sensing also runs
  while you type. KDE on Wayland refuses that call (measured on a headless
  session), so it is one of those places.

## Install

    git clone https://github.com/mishan/presenced
    cd presenced
    ./install.sh

The installer works entirely in your home directory and never needs root. It
puts presenced in its own virtual environment under `~/.local/share/presenced`,
links `presenced` and `presencectl` into `~/.local/bin`, downloads the face
detection model (about 230 KB), writes `~/.config/presenced/config.toml`
(asking whether to lock the screen when you leave and wake it when you come
back), and starts presenced as a systemd user service. If system packages
are missing, it lists them with the command to install them on your
distribution, and stops.

Then calibrate once, in a quiet room:

    presencectl calibrate --write

It plays some beeps and asks you to leave the room for about half a minute,
to measure the room empty. `./install.sh --calibrate` does this as part of
installing.

Run `./install.sh` again after pulling to upgrade; your settings are kept.
`./install.sh --uninstall` removes presenced but keeps your settings, and
`--uninstall --purge` removes those too. `./install.sh --help` lists the rest.

## Configure

Settings live in `~/.config/presenced/config.toml`. Every setting is optional;
the installer (or `presencectl init-config`) writes the file with all of them
and their defaults. The one you will want is an action:

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
inhibitor (see D-Bus below). `Pause` and `Inhibit` reply only once any check
under way has finished, so the camera and speaker are free when the caller
hears back.

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
for about two seconds at most per look (40 frames). The camera LED lights for
each look.

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
  output currently playing through a speaker, never headphones, HDMI, USB or
  Bluetooth, whatever the `speaker` setting matches. When an output is shared
  between the speaker and the headphone jack, the route it is playing through
  at that moment decides.
- **No IR camera?** presenced runs on the sonar alone, and says so in its log.
- **If presenced loses the session or system bus, or sensing keeps failing
  for a minute,** it exits with an error so that systemd starts it afresh. A
  bad configuration or a second copy already running exits too, and is not
  restarted.
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
