import asyncio
import math
from types import SimpleNamespace

import pytest
from dbus_fast import MessageType

from presenced import desktop


class SilentBus:
    """A bus whose services never answer."""

    async def call(self, message):
        await asyncio.sleep(3600)


def test_a_service_that_never_answers_times_out(monkeypatch):
    monkeypatch.setattr(desktop, "CALL_TIMEOUT_S", 0.1)
    with pytest.raises(TimeoutError):
        asyncio.run(desktop.Desktop(SilentBus(), SilentBus()).locked())


class Reply:
    def __init__(self, body=(), error=False):
        self.body = list(body)
        self.message_type = MessageType.ERROR if error else MessageType.METHOD_RETURN


class WatchBus:
    """Mutter's idle monitor, as far as watching for input goes."""

    def __init__(self, refuse=False, hang=False, fire_at_once=False):
        self.refuse, self.hang, self.fire_at_once = refuse, hang, fire_at_once
        self.desktop = None
        self.watches = 0

    def fire(self, interface=desktop.MUTTER_IDLE):
        self.desktop.on_message(
            SimpleNamespace(message_type=MessageType.SIGNAL, interface=interface, member="WatchFired")
        )

    async def call(self, message):
        if message.member == "GetIdletime":
            return Reply([600000])
        if self.hang:
            await asyncio.sleep(3600)
        if self.refuse:
            return Reply(error=True)
        self.watches += 1
        if self.fire_at_once:  # the signal can be handled before the reply is
            self.fire()
        return Reply([self.watches])


def watching(bus, now=(0.0,)):
    bus.desktop = desktop.Desktop(bus, SilentBus(), clock=lambda: now[0])
    asyncio.run(bus.desktop.idle_seconds())
    return bus.desktop


def test_a_silent_watch_says_since_when():
    now, bus = [100.0], WatchBus()
    d = watching(bus, now)
    assert asyncio.run(d.watch_input()) is None  # none was in place
    now[0] += 3.0
    assert asyncio.run(d.watch_input()) == 100.0
    assert bus.watches == 1


def test_a_fired_watch_is_reported_and_replaced():
    bus = WatchBus()
    d = watching(bus)
    asyncio.run(d.watch_input())
    bus.fire()
    assert asyncio.run(d.watch_input()) == math.inf
    assert bus.watches == 2


def test_a_watch_firing_before_its_reply_counts():
    bus = WatchBus(fire_at_once=True)
    d = watching(bus)
    asyncio.run(d.watch_input())
    assert asyncio.run(d.watch_input()) == math.inf


def test_a_signal_from_elsewhere_is_not_our_watch():
    bus = WatchBus()
    d = watching(bus)
    asyncio.run(d.watch_input())
    bus.fire("org.example.Other")
    assert asyncio.run(d.watch_input()) == 0.0


def test_a_restarted_idle_monitor_gets_a_new_watch_and_says_nothing_of_the_old():
    bus = WatchBus()
    d = watching(bus)
    asyncio.run(d.watch_input())
    d.owner_changed(desktop.MUTTER_IDLE)
    assert asyncio.run(d.watch_input()) is None
    assert bus.watches == 2


@pytest.mark.parametrize("bus", [WatchBus(refuse=True), WatchBus(hang=True)], ids=["refused", "hung"])
def test_a_watch_that_cannot_be_added_says_nothing(monkeypatch, bus):
    monkeypatch.setattr(desktop, "CALL_TIMEOUT_S", 0.05)
    d = watching(bus)
    asyncio.run(d.watch_input())
    assert asyncio.run(d.watch_input()) is None


def test_a_watch_goes_in_before_the_idle_time_was_ever_read():
    bus = WatchBus()
    bus.desktop = desktop.Desktop(bus, SilentBus())
    asyncio.run(bus.desktop.watch_input())
    assert bus.watches == 1


def test_other_desktops_have_no_watch():
    bus = WatchBus()
    d = desktop.Desktop(bus, SilentBus())
    d.idle_source = desktop.IDLE_SOURCES[1]
    assert asyncio.run(d.watch_input()) is None
    assert bus.watches == 0


class SessionBus:
    """GNOME's session manager, or else the power management interface."""

    def __init__(self, inhibitors=None, has_inhibit=False, get_active=None, power_list=None):
        self.inhibitors = inhibitors  # {path: (app id, flags)}; None: not GNOME
        self.has_inhibit = has_inhibit
        self.get_active = get_active
        self.power_list = power_list
        self.calls = []

    async def call(self, message):
        self.calls.append(message.member)
        if message.member == "GetActive":
            return Reply(error=True) if self.get_active is None else Reply([self.get_active])
        if message.member == "HasInhibit":
            return Reply([self.has_inhibit])
        if message.interface == "org.freedesktop.PowerManagement.Inhibit":
            return Reply(error=True) if self.power_list is None else Reply([self.power_list])
        if self.inhibitors is None:
            return Reply(error=True)
        if message.member == "IsInhibited":
            return Reply([any(flags & 8 for _, flags in self.inhibitors.values())])
        if message.member == "GetInhibitors":
            return Reply([list(self.inhibitors)])
        if message.path not in self.inhibitors:
            return Reply(error=True)  # gone since it was listed
        app, flags = self.inhibitors[message.path]
        return Reply([app if message.member == "GetAppId" else flags])


@pytest.mark.parametrize(
    "inhibitors, expected",
    [
        ({}, False),
        ({"/ours": ("presenced", 8)}, False),
        ({"/ours": ("presenced", 8), "/video": ("firefox", 8)}, True),
        ({"/ours": ("presenced", 8), "/suspend": ("electron", 4)}, False),
    ],
    ids=["none", "only ours", "ours and a video", "ours and another kind"],
)
def test_our_own_inhibitor_is_not_taken_for_a_video(inhibitors, expected):
    d = desktop.Desktop(SessionBus(inhibitors), SilentBus())
    assert asyncio.run(d.idle_inhibited()) is expected


def test_an_inhibitor_gone_since_it_was_listed_is_skipped():
    bus = SessionBus({"/ours": ("presenced", 8)})
    real = bus.call

    async def vanishing(message):
        reply = await real(message)
        if message.member == "GetInhibitors":
            reply.body[0].append("/gone")
        return reply

    bus.call = vanishing
    assert asyncio.run(desktop.Desktop(bus, SilentBus()).idle_inhibited()) is False


@pytest.mark.parametrize(
    "answers, expected",
    [
        ([(1.0, False), (20.0, True)], True),  # never showed ours, so this is a video
        ([(1.0, True), (20.0, True)], False),  # showed ours from the start
        ([(7.0, False), (9.0, True), (60.0, True)], False),  # showed ours, but late
    ],
    ids=["separate", "mixed", "mixed, late"],
)
def test_where_only_has_inhibit_exists_our_own_hold_is_told_from_a_video(answers, expected):
    now, bus = [0.0], SessionBus(has_inhibit=False)
    d = desktop.Desktop(bus, SilentBus(), clock=lambda: now[0])
    d.holding(True)
    for at, bus.has_inhibit in answers:
        now[0] = at
        result = asyncio.run(d.idle_inhibited())
    assert result is expected


def test_with_no_hold_has_inhibit_is_taken_at_its_word():
    bus = SessionBus(has_inhibit=False)
    d = desktop.Desktop(bus, SilentBus())
    d.holding(True)
    bus.has_inhibit = True
    asyncio.run(d.idle_inhibited())
    d.holding(False)
    assert asyncio.run(d.idle_inhibited()) is True


class Lock:
    def __init__(self, hint):
        self.hint = hint

    async def call(self, message):
        if message.member == "GetUser":
            return Reply(["/user"])
        if message.member == "Get" and message.body[1] == "Display":
            return Reply([SimpleNamespace(value=("c1", "/session"))])
        return Reply([SimpleNamespace(value=self.hint)])


@pytest.mark.parametrize(
    "hint, active, expected",
    [(True, None, True), (False, None, False), (False, False, False), (False, True, True)],
)
def test_a_screen_saver_can_say_locked_where_logind_does_not(hint, active, expected):
    d = desktop.Desktop(SessionBus(get_active=active), Lock(hint))
    assert asyncio.run(d.locked()) is expected


def test_a_refused_get_active_is_not_asked_again_until_its_owner_changes():
    bus = SessionBus()
    d = desktop.Desktop(bus, Lock(False))
    asyncio.run(d.locked())
    asyncio.run(d.locked())
    assert bus.calls.count("GetActive") == 2  # the freedesktop name and Xfce's
    d.owner_changed("org.freedesktop.ScreenSaver")
    asyncio.run(d.locked())
    assert bus.calls.count("GetActive") == 3


def test_a_hung_screen_saver_does_not_fail_the_lock_check(monkeypatch):
    monkeypatch.setattr(desktop, "CALL_TIMEOUT_S", 0.05)
    assert asyncio.run(desktop.Desktop(SilentBus(), Lock(False)).locked()) is False


def test_an_inhibitor_list_that_cannot_be_read_counts_as_someone_elses():
    bus = SessionBus({"/video": ("firefox", 8)})
    real = bus.call

    async def refusing(message):
        return Reply(error=True) if message.member == "GetInhibitors" else await real(message)

    bus.call = refusing
    assert asyncio.run(desktop.Desktop(bus, SilentBus()).idle_inhibited()) is True


@pytest.mark.parametrize(
    "power_list, expected",
    [([], False), (["presenced"], False), (["presenced", "parole"], True)],
    ids=["none", "only ours", "ours and a video"],
)
def test_a_power_manager_that_lists_inhibitors_is_filtered_like_gnome(power_list, expected):
    bus = SessionBus(has_inhibit=bool(power_list), power_list=power_list)
    d = desktop.Desktop(bus, SilentBus())
    assert asyncio.run(d.idle_inhibited()) is expected


def test_the_second_screen_saver_can_say_active():
    bus = SessionBus()
    real = bus.call

    async def xfce(message):
        if message.member == "GetActive" and message.destination == "org.xfce.ScreenSaver":
            return Reply([True])
        return await real(message)

    bus.call = xfce
    assert asyncio.run(desktop.Desktop(bus, Lock(False)).locked()) is True


def test_a_saver_saying_no_does_not_stop_the_other_being_asked():
    bus = SessionBus(get_active=False)
    real = bus.call

    async def xfce(message):
        if message.member == "GetActive" and message.destination == "org.xfce.ScreenSaver":
            return Reply([True])
        return await real(message)

    bus.call = xfce
    assert asyncio.run(desktop.Desktop(bus, Lock(False)).locked()) is True


@pytest.mark.parametrize(
    "reply", [Reply(), Reply([[("presenced", "reason")]])], ids=["empty", "pairs, not names"],
)
def test_an_inhibitor_list_of_another_shape_falls_back_to_has_inhibit(reply):
    bus = SessionBus(has_inhibit=False)
    real = bus.call

    async def odd(message):
        return reply if message.member == "GetInhibitors" else await real(message)

    bus.call = odd
    assert asyncio.run(desktop.Desktop(bus, SilentBus()).idle_inhibited()) is False
