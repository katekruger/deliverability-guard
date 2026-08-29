"""Smoke test for the scaffold. Real coverage lands with the engine in Prompt 1."""

import deliverability_guard


def test_package_has_a_version() -> None:
    assert deliverability_guard.__version__
