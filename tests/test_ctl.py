import argparse

import pytest

from presenced.ctl import parse_duration


@pytest.mark.parametrize(
    "text, seconds",
    [("90", 90), ("90s", 90), ("15m", 900), ("1h", 3600), ("1h30m", 5400), ("1.5h", 5400)],
)
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "1x", "1h junk"])
def test_bad_durations(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration(text)


def test_init_config_writes_a_complete_file_and_keeps_an_existing_one(tmp_path):
    import asyncio

    from presenced.config import load
    from presenced.ctl import LOCK_ACTION, cmd_init_config

    path = tmp_path / "presenced" / "config.toml"
    args = argparse.Namespace(config=str(path), lock=True, wake=True, force=False)
    asyncio.run(cmd_init_config(args))
    config = load(path)
    assert config.actions.on_away == LOCK_ACTION
    assert config.wake.enabled

    path.write_text("# mine\n")
    asyncio.run(cmd_init_config(args))
    assert path.read_text() == "# mine\n"
