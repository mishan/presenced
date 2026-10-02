import pytest

from presenced.config import Proximity
from presenced.proximity import ProximitySensor
from presenced.sensor import Verdict
from presenced.service import make_proximity


def device(root, number, name, **files):
    path = root / f"iio:device{number}"
    path.mkdir()
    (path / "name").write_text(f"{name}\n")
    for file, value in files.items():
        (path / file).write_text(f"{value}\n")
    return path


@pytest.mark.parametrize(
    "files, verdict, detail",
    [
        ({"in_proximity0_raw": 1, "in_proximity1_raw": 640}, Verdict.PRESENT, {"presence": 1, "distance": 640}),
        ({"in_proximity0_raw": 0, "in_proximity1_raw": 0}, Verdict.ABSENT, {"presence": 0, "distance": 0}),
        ({"in_proximity_raw": 1}, Verdict.PRESENT, {"presence": 1}),  # kernels with the one channel
        ({"in_proximity0_raw": "garbage"}, Verdict.UNKNOWN, {}),
    ],
)
def test_presence_is_read_from_the_prox_device(tmp_path, files, verdict, detail):
    device(tmp_path, 0, "als", in_illuminance_raw=300)
    device(tmp_path, 1, "prox", **files)
    reading = ProximitySensor(root=tmp_path).read()
    assert (reading.verdict, reading.detail) == (verdict, detail)


def test_a_sensor_that_goes_away_is_unknown_until_it_comes_back(tmp_path):
    import shutil

    path = device(tmp_path, 0, "prox", in_proximity0_raw=1)
    sensor = ProximitySensor(root=tmp_path)
    shutil.rmtree(path)
    assert sensor.read().verdict is Verdict.UNKNOWN
    device(tmp_path, 3, "prox", in_proximity0_raw=1)
    assert sensor.read().verdict is Verdict.UNKNOWN  # found again, read next time
    assert sensor.read().verdict is Verdict.PRESENT


class Clock:
    """Each call is a moment after the last: a long one while slow."""

    def __init__(self):
        self.now, self.slow = 0.0, False

    def __call__(self):
        self.now += 5.0 if self.slow else 0.1
        return self.now


def test_a_sensor_that_keeps_not_answering_is_left_alone_for_a_while(tmp_path):
    from presenced.proximity import REST_S

    path = device(tmp_path, 0, "prox", in_proximity0_raw=0)
    clock = Clock()
    sensor = ProximitySensor(root=tmp_path, clock=clock)
    clock.slow = True
    assert [sensor.read().verdict for _ in range(3)] == [Verdict.UNKNOWN] * 3
    clock.slow = False
    (path / "in_proximity0_raw").write_text("1\n")
    assert sensor.read().verdict is Verdict.UNKNOWN  # resting
    clock.now += REST_S
    assert sensor.read().verdict is Verdict.PRESENT


def test_a_distance_that_does_not_answer_is_dropped(tmp_path):
    device(tmp_path, 0, "prox", in_proximity0_raw=1, in_proximity1_raw=0)
    times = iter([0.0, 0.1, 0.2, 5.2, 5.3, 5.4])  # presence answers, distance does not
    sensor = ProximitySensor(root=tmp_path, clock=lambda: next(times))
    assert sensor.read().detail == {"presence": 1}
    assert sensor.read().detail == {"presence": 1}  # and is not asked again


def test_a_sensor_that_does_not_answer_is_unknown_not_absent(tmp_path):
    device(tmp_path, 0, "prox", in_proximity0_raw=0)
    times = iter([0.0, 5.0])
    assert ProximitySensor(root=tmp_path, clock=lambda: next(times)).read().verdict is Verdict.UNKNOWN


def test_a_configured_device_need_not_be_named_prox_but_is_warned_about(tmp_path, caplog):
    device(tmp_path, 0, "prox", in_proximity0_raw=0)
    device(tmp_path, 1, "hpd", in_proximity0_raw=1)
    assert ProximitySensor("iio:device1", root=tmp_path).read().verdict is Verdict.PRESENT
    assert "not prox" in caplog.text


def test_a_laptop_without_one_runs_without_it(tmp_path, monkeypatch):
    from presenced import proximity

    device(tmp_path, 0, "als", in_illuminance_raw=300)
    monkeypatch.setattr(proximity, "IIO", tmp_path)
    assert make_proximity(Proximity()) is None


def test_only_enabled_and_present_sensors_are_run(tmp_path, monkeypatch):
    from presenced import proximity
    from presenced.config import Config
    from presenced.service import make_sensors

    monkeypatch.setattr(proximity, "IIO", tmp_path)
    config = Config()
    config.camera.enabled = False
    config.proximity.device = "iio:device7"  # configured, but not there
    assert [sensor.name for sensor in make_sensors(config)] == ["sonar"]
    device(tmp_path, 7, "prox", in_proximity0_raw=1)
    config.sonar.enabled = False
    assert [sensor.name for sensor in make_sensors(config)] == ["proximity"]
