import pytest

from presenced.config import Config, ConfigError, parse, render


def test_empty_file_gives_defaults():
    assert parse("") == Config()


def test_settings_override_defaults():
    config = parse('[sonar]\ntone_hz = 21500\n[actions]\non_away = "loginctl lock-session"\n')
    assert config.sonar.tone_hz == 21500.0
    assert isinstance(config.sonar.tone_hz, float)
    assert config.actions.on_away == "loginctl lock-session"
    assert config.sensing == Config().sensing


def test_typos_are_errors():
    with pytest.raises(ConfigError, match="sonar.tone"):
        parse("[sonar]\ntone = 20000\n")
    with pytest.raises(ConfigError, match="sonr"):
        parse("[sonr]\n")


def test_wrong_types_are_errors():
    with pytest.raises(ConfigError, match="must be bool"):
        parse("[camera]\nenabled = 1\n")


def test_rendered_template_reads_back_the_same():
    config = Config()
    config.sonar.tone_hz = 21500.0
    config.actions.on_away = 'notify-send "gone" && loginctl lock-session'
    assert parse(render(config)) == config
