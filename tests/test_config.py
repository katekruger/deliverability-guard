"""Tests for config.py -- loading config/thresholds.yml into an AppConfig.

ENG-6: at HEAD, nothing in `src/` reads YAML at all, despite `pyyaml` being
a declared runtime dependency and the README's quickstart telling users to
`cp config/thresholds.example.yml config/thresholds.yml`. This is the
module that makes that `cp` step do something.
"""

from pathlib import Path

import pytest

from deliverability_guard.config import ConfigError, load_config
from deliverability_guard.engine.breaker import ThresholdLadder
from deliverability_guard.engine.posterior import BetaDistribution

_VALID_YAML = """
provider: instantly
complaint_rate_ladder:
  warn: 0.0005
  throttle: 0.0010
  pause: 0.0020
prior:
  alpha: 0.5
  beta: 500
dry_run: true
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "thresholds.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_valid_config_file(tmp_path: Path) -> None:
    path = _write(tmp_path, _VALID_YAML)
    config = load_config(path)
    assert config.thresholds == ThresholdLadder(warn=0.0005, throttle=0.0010, pause=0.0020)
    assert config.prior == BetaDistribution(alpha=0.5, beta=500.0)
    assert config.dry_run is True
    assert config.provider == "instantly"


def test_the_example_file_itself_loads() -> None:
    """The quickstart's `cp config/thresholds.example.yml config/thresholds.yml`
    must produce a file this module can actually load."""
    example = Path(__file__).parent.parent / "config" / "thresholds.example.yml"
    config = load_config(example)
    assert config.thresholds == ThresholdLadder(warn=0.0005, throttle=0.0010, pause=0.0020)
    assert config.dry_run is True


def test_dry_run_defaults_to_true_when_omitted(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("dry_run: true\n", "")
    path = _write(tmp_path, text)
    assert load_config(path).dry_run is True


def test_provider_defaults_to_instantly_when_omitted(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("provider: instantly\n", "")
    path = _write(tmp_path, text)
    assert load_config(path).provider == "instantly"


def test_decision_log_path_has_a_default(tmp_path: Path) -> None:
    path = _write(tmp_path, _VALID_YAML)
    assert load_config(path).decision_log_path == Path("var/decisions.jsonl")


def test_decision_log_path_is_configurable(tmp_path: Path) -> None:
    custom = tmp_path / "custom-decisions.jsonl"
    text = _VALID_YAML + f"\ndecision_log_path: {custom}\n"
    path = _write(tmp_path, text)
    assert load_config(path).decision_log_path == custom


def test_fast_and_slow_interval_seconds_have_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path, _VALID_YAML)
    config = load_config(path)
    assert config.fast_interval_seconds == 300
    assert config.slow_interval_seconds == 86400


def test_fast_and_slow_interval_seconds_are_configurable(tmp_path: Path) -> None:
    text = _VALID_YAML + "\nfast_interval_seconds: 30\nslow_interval_seconds: 3600\n"
    path = _write(tmp_path, text)
    config = load_config(path)
    assert config.fast_interval_seconds == 30
    assert config.slow_interval_seconds == 3600


def test_non_integer_fast_interval_seconds_raises_config_error(tmp_path: Path) -> None:
    text = _VALID_YAML + "\nfast_interval_seconds: not-a-number\n"
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="fast_interval_seconds"):
        load_config(path)


def test_nonpositive_fast_interval_seconds_raises_config_error(tmp_path: Path) -> None:
    text = _VALID_YAML + "\nfast_interval_seconds: 0\n"
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="fast_interval_seconds"):
        load_config(path)


def test_non_integer_slow_interval_seconds_raises_config_error(tmp_path: Path) -> None:
    text = _VALID_YAML + "\nslow_interval_seconds: not-a-number\n"
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="slow_interval_seconds"):
        load_config(path)


def test_nonpositive_slow_interval_seconds_raises_config_error(tmp_path: Path) -> None:
    text = _VALID_YAML + "\nslow_interval_seconds: -1\n"
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="slow_interval_seconds"):
        load_config(path)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"thresholds\.yml"):
        load_config(tmp_path / "does-not-exist" / "thresholds.yml")


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "complaint_rate_ladder: [this is not a mapping\n")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_non_mapping_top_level_raises_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


def test_missing_complaint_rate_ladder_names_the_key(tmp_path: Path) -> None:
    path = _write(tmp_path, "prior:\n  alpha: 0.5\n  beta: 500\n")
    with pytest.raises(ConfigError, match="complaint_rate_ladder"):
        load_config(path)


def test_missing_prior_names_the_key(tmp_path: Path) -> None:
    text = "complaint_rate_ladder:\n  warn: 0.0005\n  throttle: 0.0010\n  pause: 0.0020\n"
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="prior"):
        load_config(path)


def test_missing_ladder_rung_names_the_key(tmp_path: Path) -> None:
    text = (
        "complaint_rate_ladder:\n  warn: 0.0005\n  throttle: 0.0010\n"
        "prior:\n  alpha: 0.5\n  beta: 500\n"
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match=r"complaint_rate_ladder\.pause"):
        load_config(path)


def test_non_numeric_ladder_value_names_the_key(tmp_path: Path) -> None:
    text = (
        "complaint_rate_ladder:\n"
        "  warn: 0.0005\n"
        "  throttle: 0.0010\n"
        "  pause: not-a-number\n"
        "prior:\n  alpha: 0.5\n  beta: 500\n"
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match=r"complaint_rate_ladder\.pause"):
        load_config(path)


def test_out_of_order_thresholds_raise_config_error(tmp_path: Path) -> None:
    text = (
        "complaint_rate_ladder:\n"
        "  warn: 0.002\n"
        "  throttle: 0.001\n"
        "  pause: 0.0005\n"
        "prior:\n  alpha: 0.5\n  beta: 500\n"
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="warn"):
        load_config(path)


def test_nonpositive_prior_raises_config_error(tmp_path: Path) -> None:
    text = (
        "complaint_rate_ladder:\n"
        "  warn: 0.0005\n"
        "  throttle: 0.0010\n"
        "  pause: 0.0020\n"
        "prior:\n  alpha: 0\n  beta: 500\n"
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="positive"):
        load_config(path)


def test_non_boolean_dry_run_raises_config_error(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("dry_run: true", "dry_run: yesplease")
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="dry_run"):
        load_config(path)


def test_non_string_decision_log_path_raises_config_error(tmp_path: Path) -> None:
    text = _VALID_YAML + "\ndecision_log_path: 12345\n"
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="decision_log_path"):
        load_config(path)


def test_empty_provider_raises_config_error(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("provider: instantly", "provider: ''")
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="provider"):
        load_config(path)
