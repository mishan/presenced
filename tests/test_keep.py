import asyncio

import pytest
from dbus_fast import MessageType

from presenced import keep
from presenced.keep import POWER, XFCE_SAVER, Keeper


class Reply:
    def __init__(self, body=(), error=False):
        self.body = list(body)
        self.message_type = MessageType.ERROR if error else MessageType.METHOD_RETURN


class Saver:
    """The idle inhibition service, on one object path."""

    def __init__(self, paths=("/org/freedesktop/ScreenSaver",), raises=None):
        self.paths = paths
        self.raises = raises
        self.gate = asyncio.Event()
        self.gate.set()
        self.cookies = set()
        self.next_cookie = 40
        self.takes = 0

    async def call(self, message):
        if self.raises:
            raise self.raises
        if message.path not in self.paths:
            return Reply(error=True)
        if message.member == "Inhibit":
            self.takes += 1
            await self.gate.wait()
            self.next_cookie += 1
            self.cookies.add(self.next_cookie)
            return Reply([self.next_cookie])
        self.cookies.discard(message.body[0])
        return Reply()


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_wanting_takes_one_inhibitor_and_not_wanting_gives_it_back():
    async def scenario():
        saver = Saver()
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        await keeper.want(True)
        assert keeper.held and saver.takes == 1 and len(saver.cookies) == 1
        await keeper.want(False)
        assert not keeper.held and not saver.cookies

    run(scenario)


def test_the_older_object_path_is_tried_too():
    async def scenario():
        saver = Saver(paths=("/ScreenSaver",))
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        assert keeper.held
        await keeper.want(False)
        assert not saver.cookies

    run(scenario)


def test_no_service_is_logged_once_and_tried_again(caplog):
    async def scenario():
        saver = Saver(paths=())
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        await keeper.want(True)
        assert not keeper.held
        saver.paths = ("/org/freedesktop/ScreenSaver",)
        await keeper.want(True)
        assert keeper.held

    run(scenario)
    assert sum("idle inhibition" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("error", [EOFError("bus gone"), OSError("bus gone")], ids=["eof", "os"])
def test_a_broken_bus_never_reaches_the_caller(error):
    async def scenario():
        keeper = Keeper(Saver(raises=error), 300.0)
        await keeper.want(True)
        assert not keeper.held
        await keeper.want(False)

    run(scenario)


def test_releasing_on_a_broken_bus_still_forgets_the_cookie():
    async def scenario():
        saver = Saver()
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        saver.raises = EOFError("bus gone")
        await keeper.want(False)
        assert not keeper.held

    run(scenario)


def test_a_slow_inhibit_is_not_sent_twice_and_is_kept_if_still_wanted(monkeypatch):
    monkeypatch.setattr(keep, "CALL_TIMEOUT_S", 0.01)

    async def scenario():
        saver = Saver()
        saver.gate.clear()
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        await keeper.want(True)
        assert not keeper.held and saver.takes == 1
        saver.gate.set()
        await asyncio.sleep(0.01)
        assert keeper.held

    run(scenario)


def test_a_slow_inhibit_no_longer_wanted_is_given_back(monkeypatch):
    monkeypatch.setattr(keep, "CALL_TIMEOUT_S", 0.01)

    async def scenario():
        saver = Saver()
        saver.gate.clear()
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        await keeper.want(False)
        saver.gate.set()
        await asyncio.sleep(0.01)
        assert not keeper.held and not saver.cookies

    run(scenario)


def test_a_hold_nobody_renews_ends_by_itself():
    async def scenario():
        saver = Saver()
        keeper = Keeper(saver, 0.02)
        await keeper.want(True)
        await asyncio.sleep(0.01)
        await keeper.want(True)
        await asyncio.sleep(0.015)
        assert keeper.held  # renewed
        await asyncio.sleep(0.02)
        assert not keeper.held and not saver.cookies

    run(scenario)


def test_a_service_that_went_away_is_asked_afresh():
    async def scenario():
        saver = Saver()
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        keeper.lost(keep.SCREENSAVER)
        assert not keeper.held
        await keeper.want(True)
        assert keeper.held and saver.takes == 2

    run(scenario)


def test_a_release_is_timed_for_the_idle_reset_it_causes():
    async def scenario():
        now = [50.0]
        keeper = Keeper(Saver(), 300.0, clock=lambda: now[0])
        await keeper.want(False)  # as every tick without a face does: nothing was released
        assert keeper.released_at is None
        await keeper.want(True)
        assert keeper.released_at is None
        now[0] = 60.0
        await keeper.want(False)
        assert keeper.released_at == 60.0

    run(scenario)


def test_a_reply_arriving_after_the_service_changed_hands_is_still_held_and_given_back():
    async def scenario():
        saver = Saver()
        saver.gate.clear()
        keeper = Keeper(saver, 300.0)
        waiting = asyncio.ensure_future(keeper.want(True))
        await asyncio.sleep(0.001)  # the Inhibit is on its way
        keeper.lost(keep.SCREENSAVER)
        saver.gate.set()
        await waiting
        await keeper.want(True)
        assert saver.takes == 1 and len(saver.cookies) == 1
        await keeper.want(False)
        assert not saver.cookies

    run(scenario)


def test_a_hung_inhibit_stalls_one_tick_only_and_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(keep, "CALL_TIMEOUT_S", 0.05)

    async def scenario():
        saver = Saver()
        saver.gate.clear()
        keeper = Keeper(saver, 300.0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(3):
            await keeper.want(True)
        assert loop.time() - started < 0.1
        saver.gate.set()
        await asyncio.sleep(0)

    run(scenario)
    assert sum("not answering" in record.message for record in caplog.records) == 1


def test_wanting_again_as_the_deadline_passes_starts_afresh():
    async def scenario():
        saver = Saver()
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        keeper.expired()
        await keeper.want(True)
        await asyncio.sleep(0)
        assert keeper.held and len(saver.cookies) == 1

    run(scenario)


def test_giving_back_a_late_inhibitor_is_timed_too(monkeypatch):
    monkeypatch.setattr(keep, "CALL_TIMEOUT_S", 0.01)

    async def scenario():
        saver = Saver()
        saver.gate.clear()
        keeper = Keeper(saver, 300.0, clock=lambda: 7.0)
        await keeper.want(True)
        await keeper.want(False)
        saver.gate.set()
        await asyncio.sleep(0.01)
        assert keeper.released_at == 7.0

    run(scenario)


def test_an_empty_inhibit_reply_is_no_inhibitor():
    async def scenario():
        saver = Saver()
        real = saver.call

        async def empty(message):
            await real(message)
            return Reply()

        saver.call = empty
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        assert not keeper.held and keeper.warned

    run(scenario)


XFCE_PATHS = (XFCE_SAVER[1], POWER[1])


def test_xfce_is_asked_for_its_lock_and_its_suspend_separately_and_both_are_given_back():
    async def scenario():
        saver = Saver(paths=XFCE_PATHS)
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        assert keeper.held and len(saver.cookies) == 2
        await keeper.want(False)
        assert not keeper.held and not saver.cookies

    run(scenario)


@pytest.mark.parametrize("path", XFCE_PATHS, ids=["lock only", "suspend only"])
def test_one_of_xfces_two_is_better_than_none(path):
    async def scenario():
        saver = Saver(paths=(path,))
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        assert keeper.held and len(saver.cookies) == 1

    run(scenario)


def test_the_freedesktop_interface_is_enough_where_it_exists():
    async def scenario():
        saver = Saver(paths=("/org/freedesktop/ScreenSaver", *XFCE_PATHS))
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        assert len(saver.cookies) == 1

    run(scenario)


def test_one_of_two_services_leaving_starts_the_hold_afresh():
    async def scenario():
        saver = Saver(paths=XFCE_PATHS)
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        keeper.lost(XFCE_SAVER[0])
        saver.cookies.discard(min(saver.cookies))  # it took its inhibitor with it
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not keeper.held and not saver.cookies
        await keeper.want(True)
        assert len(saver.cookies) == 2

    run(scenario)


def test_some_other_service_leaving_changes_nothing():
    async def scenario():
        keeper = Keeper(Saver(), 300.0)
        await keeper.want(True)
        keeper.lost("org.example.Other")
        assert keeper.held

    run(scenario)


def test_a_half_hold_starts_afresh_when_the_other_service_arrives():
    async def scenario():
        saver = Saver(paths=(POWER[1],))
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        saver.paths = XFCE_PATHS
        keeper.lost(XFCE_SAVER[0])  # NameOwnerChanged, as it comes up
        await asyncio.sleep(0)
        await keeper.want(True)
        assert len(keeper.holds) == 2

    run(scenario)


def test_two_hung_services_give_back_within_one_timeout(monkeypatch):
    monkeypatch.setattr(keep, "CALL_TIMEOUT_S", 0.05)

    async def scenario():
        saver = Saver(paths=XFCE_PATHS)
        keeper = Keeper(saver, 300.0)
        await keeper.want(True)
        real = saver.call

        async def hung(message):
            await asyncio.sleep(3600)

        saver.call = hung
        loop = asyncio.get_running_loop()
        started = loop.time()
        await keeper.want(False)
        assert loop.time() - started < 0.09
        saver.call = real

    run(scenario)
