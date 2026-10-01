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


def test_a_muted_microphone_is_unusable_not_a_crash():
    assert not analyze(np.zeros((RATE * 2, CHANNELS)), TONE_HZ).usable


def test_empty_and_tiny_recordings_are_unusable():
    assert not analyze(np.zeros((0, CHANNELS)), TONE_HZ).usable
    assert not analyze(np.zeros((100, CHANNELS)), TONE_HZ).usable


def test_the_shortest_allowed_burst_still_measures():
    reading = analyze(recording(echo_gain=0.0, seconds=1.0 + 2 * 0.3), TONE_HZ)
    assert reading.usable


def sink(node_id, name, description, api="alsa", device_id=67, index=0, bus="pci"):
    return {
        "id": node_id,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {
            "media.class": "Audio/Sink", "node.name": name, "node.description": description,
            "device.api": api, "device.id": device_id, "card.profile.device": index, "device.bus": bus,
        }},
    }


def card(device_id, routes):
    return {
        "id": device_id,
        "type": "PipeWire:Interface:Device",
        "info": {"props": {"media.class": "Audio/Device"}, "params": {"Route": [
            {"direction": "Output", "name": name, "description": name, "device": index, "devices": [index]}
            for index, name in routes
        ]}},
    }


def test_only_the_laptop_speaker_is_used():
    from presenced.sonar import find_node

    objects = [
        sink(80, "bluez_output.JBL", "JBL Flip Speaker", api="bluez5"),
        sink(71, "alsa_output.HiFi__HDMI1__sink", "HDMI / DisplayPort 1 Output", index=0),
        sink(74, "alsa_output.HiFi__Speaker__sink", "Speaker", index=3),
        card(67, [(3, "[Out] Speaker")]),
    ]
    assert find_node("Audio/Sink", "speaker", objects) == "alsa_output.HiFi__Speaker__sink"
    # naming another sink outright does not get around it
    assert find_node("Audio/Sink", "bluez_output.JBL", objects) is None


def test_a_shared_output_playing_through_headphones_is_refused():
    from presenced.sonar import find_node

    objects = [
        sink(74, "alsa_output.analog-stereo", "Built-in Audio Analog Stereo Speaker", index=0),
        card(67, [(0, "analog-output-headphones")]),
    ]
    assert find_node("Audio/Sink", "speaker", objects) is None
    objects[1] = card(67, [(0, "analog-output-speaker")])
    assert find_node("Audio/Sink", "speaker", objects) == "alsa_output.analog-stereo"


def test_pipewire_trouble_means_no_nodes_rather_than_a_crash(monkeypatch):
    import subprocess

    from presenced import sonar

    def broken(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "pw-dump")

    monkeypatch.setattr(sonar.subprocess, "run", broken)
    assert sonar.Sonar().nodes() == (None, None)
    assert not sonar.Sonar().probe().usable


def test_a_usb_headset_that_calls_itself_a_speaker_is_refused():
    from presenced.sonar import find_node

    objects = [
        sink(90, "alsa_output.usb-C-Media_USB_Headset-00.analog-stereo", "USB Speaker Headset Analog Stereo",
             device_id=88, bus="usb"),
        card(88, [(0, "analog-output-speaker")]),
    ]
    assert find_node("Audio/Sink", "speaker", objects) is None


def test_a_plain_hda_laptop_speaker_is_found_by_its_route():
    from presenced.sonar import find_node

    # most Intel and AMD laptops: one stereo output, and only the route says speaker
    objects = [
        sink(74, "alsa_output.pci-0000_00_1f.3.analog-stereo", "Built-in Audio Analog Stereo", index=0),
        card(67, [(0, "analog-output-speaker")]),
    ]
    assert find_node("Audio/Sink", "speaker", objects) == "alsa_output.pci-0000_00_1f.3.analog-stereo"


def test_a_shared_speaker_and_headphone_output_follows_its_route():
    from presenced.sonar import find_node

    objects = [
        sink(74, "alsa_output.pci-sof.HiFi__Speaker__sink", "Meteor Lake Speaker + Headphones", index=0),
        card(67, [(0, "[Out] Speaker")]),
    ]
    assert find_node("Audio/Sink", "speaker", objects) == "alsa_output.pci-sof.HiFi__Speaker__sink"
    objects[1] = card(67, [(0, "[Out] Headphones")])
    assert find_node("Audio/Sink", "speaker", objects) is None


def test_the_route_a_sink_actually_plays_on_decides():
    from presenced.sonar import find_node

    # two active routes; the stale "devices" lists overlap, the "device" fields do not
    objects = [
        sink(74, "alsa_output.HiFi__Headphones__sink", "Headphones", index=1),
        {
            "id": 67, "type": "PipeWire:Interface:Device",
            "info": {"props": {}, "params": {"Route": [
                {"direction": "Output", "name": "[Out] Headphones", "device": 1, "devices": [1]},
                {"direction": "Output", "name": "[Out] Speaker", "device": 3, "devices": [1, 3]},
            ]}},
        },
    ]
    assert find_node("Audio/Sink", "", objects) is None


def test_a_hold_cuts_a_playing_tone_short(monkeypatch):
    import threading
    import time

    from presenced import sonar

    monkeypatch.setattr(sonar, "player_command", lambda sink: ["sleep", "30"])
    player = sonar.Sonar()
    threading.Timer(0.2, player.abort.set).start()
    started = time.monotonic()
    assert player.play(sonar.make_tone(20000.0, 5.0), sink="speaker") is False
    assert time.monotonic() - started < 2
