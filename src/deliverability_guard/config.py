"""Loads `config/thresholds.yml` (BUILD-PLAN.md §10) into an `AppConfig`.

At HEAD before this module existed, `pyyaml` was a declared runtime
dependency that nothing in `src/` ever imported, and the README's
quickstart told users to `cp config/thresholds.example.yml
config/thresholds.yml` -- a file no code ever read (audit finding ENG-6).
This module is what makes that `cp` step do something.

Every value is validated on load, and a failure names the offending key
(dotted, e.g. `complaint_rate_ladder.pause`) rather than surfacing a bare
`KeyError` or `TypeError` from deep inside `yaml.safe_load`'s output.
Provider credentials never live here -- see `AGENTS.md` ("no secrets in
the repo") and `cli.py`, which reads those from the environment instead.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from deliverability_guard.engine.breaker import ThresholdLadder
from deliverability_guard.engine.posterior import BetaDistribution

_DEFAULT_PROVIDER = "instantly"
_DEFAULT_DECISION_LOG_PATH = "var/decisions.jsonl"


class ConfigError(Exception):
    """The config file is missing, unreadable, not valid YAML, or has a
    missing/invalid value for a required key. The message always names the
    offending key so a user doesn't have to guess which of several nested
    mappings was wrong."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    thresholds: ThresholdLadder
    prior: BetaDistribution
    dry_run: bool
    provider: str
    decision_log_path: Path


def load_config(path: Path) -> AppConfig:
    """Load and validate `path` into an `AppConfig`.

    Raises `ConfigError` -- never a bare `yaml.YAMLError`, `KeyError`,
    `TypeError`, or `ValueError` -- for every way this can go wrong: the
    file doesn't exist or can't be read, it isn't valid YAML, its top level
    isn't a mapping, a required key is missing, or a value has the wrong
    type or fails `ThresholdLadder`'s/`BetaDistribution`'s own validation
    (e.g. thresholds out of order, a non-positive prior parameter).
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level config must be a mapping, got {type(data).__name__}")
    config_data = cast(dict[str, object], data)

    ladder_raw = _require_mapping(config_data, "complaint_rate_ladder", path)
    try:
        thresholds = ThresholdLadder(
            warn=_require_number(ladder_raw, "complaint_rate_ladder.warn", path),
            throttle=_require_number(ladder_raw, "complaint_rate_ladder.throttle", path),
            pause=_require_number(ladder_raw, "complaint_rate_ladder.pause", path),
        )
    except ValueError as exc:
        raise ConfigError(f"{path}: invalid 'complaint_rate_ladder': {exc}") from exc

    prior_raw = _require_mapping(config_data, "prior", path)
    try:
        prior = BetaDistribution(
            alpha=_require_number(prior_raw, "prior.alpha", path),
            beta=_require_number(prior_raw, "prior.beta", path),
        )
    except ValueError as exc:
        raise ConfigError(f"{path}: invalid 'prior': {exc}") from exc

    dry_run_raw = config_data.get("dry_run", True)
    if not isinstance(dry_run_raw, bool):
        raise ConfigError(f"{path}: 'dry_run' must be a boolean, got {type(dry_run_raw).__name__}")
    dry_run = dry_run_raw

    provider_raw = config_data.get("provider", _DEFAULT_PROVIDER)
    if not isinstance(provider_raw, str) or not provider_raw:
        raise ConfigError(f"{path}: 'provider' must be a non-empty string")
    provider = provider_raw

    decision_log_raw = config_data.get("decision_log_path", _DEFAULT_DECISION_LOG_PATH)
    if not isinstance(decision_log_raw, str):
        raise ConfigError(
            f"{path}: 'decision_log_path' must be a string, got {type(decision_log_raw).__name__}"
        )
    decision_log_path = Path(decision_log_raw)

    return AppConfig(
        thresholds=thresholds,
        prior=prior,
        dry_run=dry_run,
        provider=provider,
        decision_log_path=decision_log_path,
    )


def _require_mapping(data: dict[str, object], key: str, path: Path) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        found = "missing" if key not in data else type(value).__name__
        raise ConfigError(f"{path}: '{key}' must be a mapping, got {found}")
    return cast(dict[str, object], value)


def _require_number(mapping: dict[str, object], dotted_key: str, path: Path) -> float:
    leaf_key = dotted_key.rsplit(".", 1)[-1]
    value = mapping.get(leaf_key)
    if value is None:
        raise ConfigError(f"{path}: '{dotted_key}' is required")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{path}: '{dotted_key}' must be a number, got {type(value).__name__}")
    return float(value)
