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
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def slow(monkeypatch):
    """A clock, and a set of file names whose reads take five seconds on it."""
    from pathlib import Path

    clock, names = Clock(), set()
    read_text = Path.read_text

    def slow_read_text(self, *args, **kwargs):
        if self.name in names:
            clock.now += 5.0
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", slow_read_text)
    return clock, names


def test_a_sensor_that_does_not_answer_is_unknown_not_absent(tmp_path, slow):
    clock, names = slow
    device(tmp_path, 0, "prox", in_proximity0_raw=0)
    names.add("in_proximity0_raw")
    assert ProximitySensor(root=tmp_path, clock=clock).read().verdict is Verdict.UNKNOWN


def test_a_sensor_that_keeps_not_answering_is_left_alone_for_a_while(tmp_path, slow):
    from presenced.proximity import REST_S

    clock, names = slow
    path = device(tmp_path, 0, "prox", in_proximity0_raw=0)
    sensor = ProximitySensor(root=tmp_path, clock=clock)
    names.add("in_proximity0_raw")
    assert [sensor.read().verdict for _ in range(3)] == [Verdict.UNKNOWN] * 3
    names.clear()
    (path / "in_proximity0_raw").write_text("1\n")
    clock.now += REST_S - 1
    assert sensor.read().verdict is Verdict.UNKNOWN  # still resting
    clock.now += 1
    assert sensor.read().verdict is Verdict.PRESENT


def test_a_distance_that_does_not_answer_is_dropped(tmp_path, slow):
    clock, names = slow
    device(tmp_path, 0, "prox", in_proximity0_raw=1, in_proximity1_raw=0)
    names.add("in_proximity1_raw")
    sensor = ProximitySensor(root=tmp_path, clock=clock)
    assert sensor.read().detail == {"presence": 1}
    before = clock.now
    assert sensor.read().detail == {"presence": 1}
    assert clock.now == before  # and is not asked again


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
