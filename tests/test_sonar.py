import numpy as np

from presenced.sonar import CHANNELS, RATE, analyze

TONE_HZ = 20000.0


def recording(echo_gain, seconds=2.0, lead=0.3, direct_gain=0.05):
    """A burst with silence either side, plus an echo whose phase wanders."""
    rng = np.random.default_rng(0)
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    burst = (t > lead) & (t < seconds - lead)
    wander = np.cumsum(rng.normal(0, 1, n)) * 2 * np.pi * 15 / RATE
    signal = direct_gain * np.sin(2 * np.pi * TONE_HZ * t)
    signal += echo_gain * np.sin(2 * np.pi * TONE_HZ * t + wander)
    signal = signal * burst + rng.normal(0, 1e-4, n)
    return np.repeat(signal[:, None], CHANNELS, axis=1)


def test_still_room_is_quiet():
    reading = analyze(recording(echo_gain=0.0), TONE_HZ)
    assert reading.usable
    assert reading.motion_db < -60


def test_moving_echo_is_seen():
    reading = analyze(recording(echo_gain=0.005), TONE_HZ)
    assert reading.usable
    assert reading.motion_db > -45


def test_level_matches_the_tone():
    reading = analyze(recording(echo_gain=0.0), TONE_HZ)
    assert abs(reading.level_db - 20 * np.log10(0.05)) < 0.5


def test_silence_is_unusable():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1e-4, (RATE * 2, CHANNELS))
    assert not analyze(noise, TONE_HZ).usable


def test_antiphase_microphones_do_not_cancel():
    samples = recording(echo_gain=0.0)
    samples[:, 1] *= -1
    reading = analyze(samples, TONE_HZ)
    assert abs(reading.level_db - 20 * np.log10(0.05)) < 0.5


def test_a_click_does_not_hide_the_burst():
    samples = recording(echo_gain=0.0)
    samples[RATE : RATE + 40] += 0.9
    reading = analyze(samples, TONE_HZ)
    assert reading.usable
