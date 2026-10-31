# Keeping the desktop awake: design

Status: implemented. The desktop rig is still to come.

The desktop counts only keyboard and mouse input. Sit and read for five
minutes and GNOME locks the screen, even though the camera can see you. This
describes how presenced tells the desktop you are there.

## What it must do

- While a face was seen recently, the desktop does not dim, blank, lock or
  suspend for lack of input.
- Only a face counts. Sonar motion alone never holds the desktop off: a fan or
  a curtain must not keep an unattended laptop unlocked. With the camera blind
  or missing, the desktop's own timers run as they always did.
- When you leave, `on_away` locks as before. The desktop's timers stay as a
  backstop if presenced stops or crashes.
- It works on any desktop that offers the freedesktop idle inhibition
  interface. Elsewhere presenced logs that once and carries on as before.

## How the desktop is told

`org.freedesktop.ScreenSaver.Inhibit(app, reason)` on the session bus, released
with `UnInhibit(cookie)`. Measured on GNOME 50:

| Mechanism | Result |
|---|---|
| `org.freedesktop.ScreenSaver.Inhibit` | works; becomes an idle inhibitor (flag 8) |
| `org.freedesktop.ScreenSaver.SimulateUserActivity` | refused, `NotSupported` |
| `org.gnome.ScreenSaver.SimulateUserActivity` | does not exist |
| Mutter `ResetIdletime` | refused unless a debug variable is set |

Reasons for inhibiting rather than resetting the idle time:

- Input idle time stays true. A reset, or a virtual mouse nudge, would read as
  the user typing, so presenced would call `active()` on itself and an empty
  laptop could keep itself awake for ever.
- It needs no permissions; a nudge needs `/dev/uinput`.
- A client that vanishes loses its inhibitor: GNOME cleared one within a second
  of the connection dropping, without `UnInhibit`. A crashed presenced cannot
  hold the desktop awake.
- gsd-power's `idle_configure` drops its dim, sleep and sleep-warning timers
  while an idle inhibitor is held and the screensaver is off, so suspend needs
  no inhibitor of its own, and a locked session still dims and suspends.

## Deciding when: the monitor

`Monitor` gains `look_every` (seconds, or `None` when keep-awake is off), and two
fields:

- `sure_at`: when a look last found a face, `None` if not since the last restart.
  Unlike `seen_at` it counts forced looks, which must not arm the guard that
  keeps a flickering proximity sensor off the camera, or they would delay away.
- `since`: when the current run began, set by `restart()`.

`restart()` clears `sure_at`. Input calls `active()` and so `restart()`, which
is fine: GNOME is not idle while you type, and the first look comes soon after
the last input. It also covers resume from suspend, where the monotonic clock
stood still and `sure_at` would otherwise look fresh.

**Forced looks.** Today the camera is asked only after `quiet_for` of quiet
sonar, so someone fidgeting is never looked at. In `step_present`, when sonar
has not been quiet for `quiet_for`, take a look once `now` is `look_every` past
`looked_at`, or `min(quiet_for, look_every)` past `since` for the first look of
a run. The early first look starts the hold within about 40 s of the last
input, in time for GNOME's shortest delay of one minute:

- a face sets `sure_at`;
- no face, or a blind camera, schedules one retry after `recheck_after`, then
  the normal cadence, so a turned head does not cost two minutes. A forced miss
  does not set `missed_at`: the sonar saw motion, and two misses must still
  follow a quiet run before the user counts as away.

Looks in the existing quiet path set `sure_at` too, and `step_away` sets it after
`switch()`, because the switch runs `restart()`. A poll sensor saying nobody is
here skips forced looks, and a quiet-path look cancels a pending retry.

**The verdict.** `Monitor.attended`:

    not held, and sure_at is within KEEP_FOR (a switch to away clears it)

`KEEP_FOR` is `2.5 * look_every + recheck_after`, 310 s by default, so one missed look (a
turned head) does not drop the hold. If you leave while the sonar keeps seeing
motion, the hold lapses within five minutes of the last face, and GNOME then
counts a full idle delay from there (see below).

## Acting on it: the daemon

`keep.py` holds a `Keeper`:

    class Keeper:
        held: bool
        released_at: float | None           # the idle count may restart at this moment
        async def want(self, wanted: bool)  # take or release; bounded, never raises
        def lost(self)                      # the service went away: forget the cookie

`Daemon.tick` calls `Daemon.keep_awake` in a `finally`, so every path through
`sense` ends in the right state. The value is `monitor.attended` and the mode
is `sensing`. `watching` (locked), `paused`, `inhibited` and `locked` all release,
and so does `input`, because input clears `sure_at`: a hold ends with each burst
of typing and returns with the next look. That is harmless, as the desktop is not
idle then. No forced looks run while locked, so the LED stays dark. `locked()` also asks `org.freedesktop.ScreenSaver.GetActive`, since
a compositor may never set logind's `LockedHint`. GNOME refuses that call
(measured); it does set the hint, so a refusal stops the asking until the
service changes hands.

`attended` is false during any hold and `release()` runs `restart()`, so the
daemon cannot just follow it here: it keeps the hold through `held` itself, and
leaving a hold has to make the next look due at once rather than after `since`.

`held` (another program's inhibitor, such as a video) keeps our hold as it is.
On a desktop that does not restart its idle count, releasing would lock at once
a user seen a moment ago. Leaving `held`, the paused state or an inhibit makes
a look due on the next step, and that look decides: a face keeps the hold, a
miss releases it.

The tick's `finally` is not enough if the sensing thread wedges (a child stuck
in `wait()`): the loop and the bus stay up and the hold would never end. So
`want(True)` re-arms `loop.call_later(KEEP_FOR, ...)`, a deadline that releases
if no tick renews it.

`on_message` already sees `NameOwnerChanged`; when
`org.freedesktop.ScreenSaver` changes owner it calls `keeper.lost()`, so the
next tick takes a new inhibitor.

`Status` gains `keeping_awake`, and the change is logged.

## Releasing resets the idle time on GNOME

Mutter restarts its idle count whenever the last idle inhibitor goes, with no
input at all. Measured on GNOME Shell 50.5, idle went from 7.7 s to 0.7 s on
each of three `UnInhibit` calls and from 7.7 s to 1.0 s on a dropped connection,
where a control with no inhibitor kept counting.

So a release reads as the user typing: `idle_seconds` falls below
`start_after_idle`, the daemon calls `active()`, and a real `away` flips back to
`present`. The same already happens today when a video ends, because any
program's inhibitor does this.

`Daemon` keeps `idle_reset_at`, set when our hold is released and when
`idle_inhibited` goes from true to false. A short idle reading counts as input
only if the input it implies, `now - idle_seconds`, falls more than
`IDLE_RESET_SLACK_S` (1 s) after `idle_reset_at`.

This is deliberate and not going away: mutter 4af00ae296 (2018, bgo#705942) does
it so that idle timeouts do not fire the moment a long inhibitor ends.

On GNOME, `Desktop.watch_input` uses Mutter's `AddUserActiveWatch` instead of the
timing above. Its `WatchFired` stayed silent across three `UnInhibit` calls in a
real session, and fired on real input on a live one. A watch fires once, so the
tick puts one in place, before any hold. A short idle time is not input if the
event it implies came after a watch went in place and that watch stayed silent.
With no watch in place the timing decides, as on a first tick.

Elsewhere the timing stays, and polling leaves it two limits. Input between the inhibitor ending and that second
is not counted unless more follows, so the click that stops a video may go
unseen. An inhibitor that lives and dies between two ticks is invisible, and
its reset still reads as input.

It also means leaving with the sonar seeing motion takes up to `KEEP_FOR` plus
the idle delay to lock, ten minutes by default. A quiet departure still ends in
`on_away` within a minute.

## Not seeing our own inhibitor

`respect_idle_inhibitors` must not hold sensing off because of us.
`Desktop.idle_inhibited` becomes:

1. GNOME: `GetInhibitors`, then each one's `GetAppId` and `GetFlags`; true if
   any has the idle flag and is not `presenced`.
2. Elsewhere: `HasInhibit` on `org.freedesktop.PowerManagement.Inhibit`. With
   `respect_idle_inhibitors` on, a true answer means `held`, so no hold starts
   during a video. During a hold, a true answer within `MIXED_WITHIN_S` (10 s) of
   taking it is ours, however late it shows, and from then on `HasInhibit` is
   ignored while we hold, so a video started during a hold goes unseen. If it
   stayed false that long, the interfaces are separate and a true answer is a
   video. Read wrongly the other way, ours would pass for a video and stop
   sensing, so `held` must not renew the hold's deadline.

An inhibitor that vanishes between `GetInhibitors` and `GetAppId` is skipped.
GNOME reports our app id as `presenced` (measured).

## Configuration

    [sensing]
    keep_awake = true
    # Seconds between camera looks while there is no input. The first comes
    # sooner, after sensing.quiet_for if that is shorter.
    keep_awake_every = 120

On by default. The cost is the camera LED about every two minutes during idle
stretches. `keep_awake_every` has a minimum of 10.

## Failure modes

| What | What happens |
|---|---|
| No `org.freedesktop.ScreenSaver` | logged once, no hold, tried again whenever a hold is wanted |
| `Inhibit` errors | logged once, retried next tick; no hold |
| `Inhibit` never answers | logged once; only the tick that sent it waits, and no second one is sent until it ends |
| `Inhibit` answers late | the call is shielded and stays in flight, so no second one is sent; a late cookie that is no longer wanted gets `UnInhibit` |
| Sensing thread wedges | the deadline releases after `KEEP_FOR` |
| `UnInhibit` fails | cookie forgotten, logged; the service has probably gone |
| presenced crashes or is stopped | the bus drops the client, the hold goes with it |
| The service restarts | `lost()` forgets the cookie; retaken next tick |
| Suspend during a hold | `restart()` clears `sure_at`; released on the first tick after resume |
| Camera blind or missing | `sure_at` never set or lapses; desktop timers run |
| Idle delay under about 40 s | the desktop can lock before the first look; lower `quiet_for` and `keep_awake_every`; documented |
| A video outlasts the deadline, on a desktop that does not restart its idle count | our hold has lapsed, so the desktop can lock as the video ends, before the look. GNOME is safe. Not renewing during `held` is what ends a misread hold |
| Inhibit answers after a tick, where HasInhibit is mixed | our hold reads as a video until the deadline, at most `KEEP_FOR` |
| Face in a photo on the desk | held indefinitely, with no liveness check. Worse than today, when the desktop timer still locks; and `on_away` is empty by default |

## Test plan

- **Monitor, table-driven:** forced look timing; a forced miss leaves
  `missed_at` alone and retries once; `step_away`'s return keeps `sure_at`;
  `attended` lapses after `KEEP_FOR`; sonar motion alone
  never sets it; `restart()` clears it; no look sensors means never attended.
- **Property test:** random reading sequences. `attended` implies a face within
  `KEEP_FOR`, and never while away or frozen. With `look_every=None` the
  state sequence equals today's.
- **Keeper and Desktop against a fake bus:** take, release, error, timeout,
  lost; our own inhibitor filtered, a video's not.
- **Daemon, sense included (a monitor test cannot see `release()`):** an idle
  reset after a release or a video ending is not input, and does not flip
  `away`; real input just after one still counts; each mode
  gives the right `want`; held across a failing tick; `held` keeps the hold and
  its exit looks at once; a stalled step reaches the deadline; a late `Inhibit`
  reply is released.
- **Failure injection:** the service leaving mid-hold; a real dropped connection.
- **Desktop rig (Docker, its own CI job):** real desktops running headless, so
  the contract is checked against them rather than against our fake. Sensors
  stay faked; containers have no camera or speakers. The GNOME spike worked:
  systemd as PID 1 without `--privileged` (`SYS_ADMIN`, cgroups remounted
  read-write), a session started through PAM with class `user` and type
  `wayland`, and `gnome-shell --headless`. Headless Shell has no screen shield,
  so the rig cannot show a lock happening; that stays a live check.
- **Live on GNOME:** idle past `idle-delay` with a face in view; then leave;
  then a video ending past `idle-delay` with a face in view.

## What real desktops do

Measured with `rig/run.sh`, which runs presenced's own `Keeper` and `Desktop` on
a real desktop in a container. Each row a desktop has a value for is required of
it, so a change fails the run, and a run that tested nothing does not pass:

| | GNOME 50 | KDE (Fedora 44) | Xfce | sway |
|---|---|---|---|---|
| `org.freedesktop.ScreenSaver` | yes | yes | no | no |
| `Inhibit` works | yes | yes | no | no |
| `PowerManagement.HasInhibit` | no service | yes | yes | no service |
| It shows our hold | n/a | yes, 5 s late or so | n/a | n/a |
| Lists inhibitors by owner | yes | no | yes, unused | n/a |
| Input idle time | Mutter | refused | none | none |
| `GetActive` | refused | works | n/a | n/a |
| A release resets the idle time | yes | unmeasured, no idle time | n/a | n/a |
| A dropped connection clears the hold | yes | yes | n/a | n/a |

KDE is the mixed case, so `MIXED_WITHIN_S` is doing real work there, and no
MPRIS fallback is needed: `HasInhibit` is reliable enough once ours is told
apart. Xfce has `org.xfce.ScreenSaver.Inhibit` instead, and `rig/run.sh xfce`
fails until presenced uses it. The rig cannot show a lock happen, as headless
Shell has no screen shield.

## Decisions

- Inhibit, not simulated activity or a nudge (see above).
- Only a face is evidence, so a proximity sensor does not count yet. Revisit
  when one exists on real hardware.
- One default: on.
- No cap on hold time since the last input. A cap would guard against a photo
  but lock people reading or watching, which is the bug this fixes.
