"""Tests for loops/slow.py, including the structural guarantee that this
module can never call pause() or throttle() -- not just by convention."""

import inspect

import pytest

from deliverability_guard.engine.breaker import DEFAULT_LADDER, ThresholdLadder
from deliverability_guard.loops import slow as slow_module
from deliverability_guard.loops.slow import tune_thresholds


def test_no_adjustment_when_no_recent_data() -> None:
    assert tune_thresholds(recent_lower_bounds=[], current=DEFAULT_LADDER) is None


def test_no_adjustment_when_recent_evidence_is_well_below_warn() -> None:
    assert tune_thresholds(recent_lower_bounds=[0.00001, 0.00002], current=DEFAULT_LADDER) is None


def test_no_adjustment_when_warn_was_already_crossed() -> None:
    """If it already crossed, that's the fast loop's job to have acted on
    -- not something the slow loop should react to by tightening further."""
    assert (
        tune_thresholds(recent_lower_bounds=[DEFAULT_LADDER.warn], current=DEFAULT_LADDER) is None
    )


def test_tightens_when_recent_evidence_is_close_to_warn_without_crossing() -> None:
    close_to_warn = DEFAULT_LADDER.warn * 0.95
    adjustment = tune_thresholds(recent_lower_bounds=[close_to_warn], current=DEFAULT_LADDER)
    assert adjustment is not None
    assert adjustment.new_thresholds.warn < DEFAULT_LADDER.warn
    assert adjustment.new_thresholds.throttle < DEFAULT_LADDER.throttle
    assert adjustment.new_thresholds.pause < DEFAULT_LADDER.pause
    assert "tightening" in adjustment.reason


def test_tightened_ladder_is_still_internally_valid() -> None:
    close_to_warn = DEFAULT_LADDER.warn * 0.95
    adjustment = tune_thresholds(recent_lower_bounds=[close_to_warn], current=DEFAULT_LADDER)
    assert adjustment is not None
    # ThresholdLadder's own __post_init__ validates warn <= throttle <= pause;
    # constructing it here would already have raised if the proposal were invalid.
    assert isinstance(adjustment.new_thresholds, ThresholdLadder)


def test_uses_the_worst_of_several_recent_bounds() -> None:
    bounds = [0.00001, DEFAULT_LADDER.warn * 0.95, 0.00002]
    adjustment = tune_thresholds(recent_lower_bounds=bounds, current=DEFAULT_LADDER)
    assert adjustment is not None


def test_rejects_a_tighten_factor_out_of_range() -> None:
    with pytest.raises(ValueError, match="tighten_factor"):
        tune_thresholds(recent_lower_bounds=[0.001], current=DEFAULT_LADDER, tighten_factor=1.0)
    with pytest.raises(ValueError, match="tighten_factor"):
        tune_thresholds(recent_lower_bounds=[0.001], current=DEFAULT_LADDER, tighten_factor=0.0)


# --- The structural guarantee ---------------------------------------------


def test_slow_loop_module_imports_nothing_from_providers() -> None:
    """The separation from the fast loop is enforced in types: this module
    must never import ProviderDriver, MailboxRef, or anything else from the
    providers package that could make pausing/throttling reachable."""
    import ast

    source = inspect.getsource(slow_module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert not node.module.startswith("deliverability_guard.providers"), (
                f"loops/slow.py must not import from {node.module}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("deliverability_guard.providers"), (
                    f"loops/slow.py must not import {alias.name}"
                )


def test_tune_thresholds_signature_has_no_capability_carrying_parameter() -> None:
    """No parameter of tune_thresholds is typed to carry a ProviderDriver,
    a BreakerStateStore, or anything else that could pause/throttle a
    mailbox -- its only inputs are numbers."""
    signature = inspect.signature(tune_thresholds)
    for name, param in signature.parameters.items():
        annotation = str(param.annotation)
        assert "Driver" not in annotation, name
        assert "StateStore" not in annotation, name
