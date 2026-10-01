import struct


def test_uinput_structures_have_the_kernel_sizes():
    assert struct.calcsize("HHHH80sI") == 92  # struct uinput_setup
    assert struct.calcsize("llHHi") == 24  # struct input_event on 64-bit


def test_a_missing_uinput_is_a_warning_not_a_crash(monkeypatch, caplog):
    import asyncio

    from presenced import wake

    monkeypatch.setattr(wake, "UINPUT", "/nonexistent/uinput")
    waker = wake.Waker()
    assert asyncio.run(waker.wake()) is False
    assert asyncio.run(waker.wake()) is False
    assert caplog.text.count("cannot wake the screen") == 1
