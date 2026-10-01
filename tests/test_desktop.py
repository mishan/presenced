import asyncio

import pytest

from presenced import desktop


class SilentBus:
    """A bus whose services never answer."""

    async def call(self, message):
        await asyncio.sleep(3600)


def test_a_service_that_never_answers_times_out(monkeypatch):
    monkeypatch.setattr(desktop, "CALL_TIMEOUT_S", 0.1)
    with pytest.raises(TimeoutError):
        asyncio.run(desktop.Desktop(SilentBus(), SilentBus()).locked())
