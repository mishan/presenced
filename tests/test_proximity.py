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


def test_a_sensor_that_goes_away_is_unknown(tmp_path):
    path = device(tmp_path, 0, "prox", in_proximity0_raw=1)
    sensor = ProximitySensor(root=tmp_path)
    (path / "in_proximity0_raw").unlink()
    assert sensor.read().verdict is Verdict.UNKNOWN


def test_a_configured_device_need_not_be_named_prox(tmp_path):
    device(tmp_path, 0, "prox", in_proximity0_raw=0)
    device(tmp_path, 1, "hpd", in_proximity0_raw=1)
    assert ProximitySensor("iio:device1", root=tmp_path).read().verdict is Verdict.PRESENT


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
