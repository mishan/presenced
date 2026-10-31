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
