from presenced.calibrate import config_text, pick_threshold, pick_tone, update_config
from presenced.config import parse


def test_picks_the_highest_tone_that_comes_through():
    levels = {19500: -16.3, 20000: -19.2, 21000: -28.2, 21500: -22.3, 22000: -30.3, 22500: -49.7}
    assert pick_tone(levels) == 21500


def test_no_tone_when_nothing_comes_back():
    assert pick_tone({20000: -110.0, 21000: -112.0}) is None


def test_threshold_sits_above_the_empty_room():
    assert pick_threshold([-37.0, -33.5, -31.0]) == -28.0


def test_threshold_is_kept_in_a_sane_range():
    assert pick_threshold([-70.0]) == -40.0
    assert pick_threshold([-5.0]) == -20.0


def test_update_keeps_everything_else():
    text = '# mine\n[sonar]\ntone_hz = 20000\nburst = 2\n\n[actions]\non_away = "lock"\n'
    updated = update_config(text, 21500.0, -30.5)
    config = parse(updated)
    assert config.sonar.tone_hz == 21500.0
    assert config.sonar.threshold_db == -30.5
    assert config.sonar.burst == 2.0
    assert config.actions.on_away == "lock"
    assert updated.startswith("# mine\n")


def test_update_adds_a_sonar_section_when_missing():
    config = parse(update_config('[actions]\non_away = "lock"\n', 21000.0, -29.0))
    assert config.sonar.tone_hz == 21000.0
    assert config.actions.on_away == "lock"


def test_new_file_is_a_full_template():
    config = parse(config_text(None, 21000.0, -29.0))
    assert config.sonar.tone_hz == 21000.0
    assert config.sonar.threshold_db == -29.0


def test_a_commented_section_header_is_found():
    updated = update_config("[sonar]  # tuned by hand\nburst = 2\n", 21000.0, -29.0)
    assert updated.count("[sonar]") == 1
    config = parse(updated)
    assert config.sonar.tone_hz == 21000.0
    assert config.sonar.burst == 2.0
