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


def test_a_wake_command_that_hangs_is_killed(monkeypatch):
    import asyncio
    import time

    from presenced import wake

    monkeypatch.setattr(wake, "COMMAND_TIMEOUT_S", 0.2)
    started = time.monotonic()
    assert asyncio.run(wake.Waker("sleep 30").wake()) is False
    assert time.monotonic() - started < 5


def test_a_wake_command_timeout_takes_what_the_shell_started(monkeypatch, tmp_path):
    import asyncio
    import os
    import time

    from presenced import wake

    monkeypatch.setattr(wake, "COMMAND_TIMEOUT_S", 0.3)
    pidfile = tmp_path / "pid"
    command = f"sleep 30 & echo $! > {pidfile}; wait"
    assert asyncio.run(wake.Waker(command).wake()) is False
    pid = int(pidfile.read_text())
    time.sleep(0.1)
    try:
        os.kill(pid, 0)
        alive = open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except ProcessLookupError:
        alive = False
    assert not alive
