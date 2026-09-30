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
