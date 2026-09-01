"""Tests for `fixtures/source_inspect.py`, and a structural guard against
the failure shape it exists to fix recurring a third time.

CLOSE8-3: an `in` assertion built on raw `inspect.getsource` -- vulnerable
to a docstring (CLOSE7-3) or a comment (CLOSE8-3) alone making it pass --
has now appeared twice in two rounds. `source_body` fixes both cases; this
file also makes a THIRD bare occurrence something the suite itself catches,
rather than something a future audit has to find again.
"""

import re
from pathlib import Path

import pytest

from fixtures.source_inspect import source_body

# The two existing regression tests that DELIBERATELY use raw
# `inspect.getsource` in an `in` assertion -- to demonstrate what the
# vacuous, pre-fix guard looked like, right next to the fixed
# `source_body`-based version. Anything else matching the pattern below is
# a new instance of the class this file exists to prevent, not a
# demonstration of it.
_KNOWN_VACUOUS_DEMONSTRATIONS = frozenset(
    {
        "test_check_and_run_drift_guard_catches_a_hand_duplicated_loop_the_vacuous_one_missed",
        "test_act_paused_status_guard_catches_the_comment_only_mutation_the_vacuous_one_missed",
    }
)

_DEF_PATTERN = re.compile(r"^def (test_\w+)\(")
_BARE_IN_GETSOURCE_PATTERN = re.compile(r"(?<!not )\bin\s+inspect\.getsource\(")


def test_source_body_strips_a_multiline_docstring() -> None:
    def example() -> None:
        """A multi-line docstring naming forbidden_name across two lines
        forbidden_name."""

    assert example.__doc__ is not None and "forbidden_name" in example.__doc__  # sanity
    assert "forbidden_name" not in source_body(example)


def test_source_body_strips_a_comment_but_keeps_the_code_around_it() -> None:
    def example() -> None:
        # forbidden_name mentioned only in this comment
        return None

    body = source_body(example)
    assert "forbidden_name" not in body
    assert "return None" in body


def test_source_body_never_mistakes_a_hash_inside_a_string_for_a_comment() -> None:
    def example() -> str:
        return "not-a-comment #forbidden_name"

    assert "forbidden_name" in source_body(example)


def test_source_body_handles_a_function_with_no_docstring_at_all() -> None:
    def example(x: int) -> int:
        return x + 1

    assert "return x + 1" in source_body(example)


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in sorted(Path(__file__).resolve().parent.glob("test_*.py"))
        # This file's own prose ABOUT the pattern (this docstring included)
        # would otherwise flag itself -- it's the enforcement mechanism,
        # not a guard the pattern could apply to.
        if p.name != "test_source_inspect.py"
    ],
)
def test_no_test_function_asserts_in_over_raw_inspect_getsource(path: Path) -> None:
    """CLOSE8-3's structural guard: every test file, scanned for a bare
    `in inspect.getsource(...)` (as opposed to `not in`, which is immune
    by construction, or a call routed through `source_body`) outside the
    two known, deliberate demonstrations above. A new one appearing here
    means a new source-level guard was written the same vulnerable way
    CLOSE7-3 and CLOSE8-3 both found -- fix it by routing through
    `source_body` instead, the same way every real guard in this suite
    already does."""
    current_function: str | None = None
    offending_lines: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        def_match = _DEF_PATTERN.match(line)
        if def_match is not None:
            current_function = def_match.group(1)
        if _BARE_IN_GETSOURCE_PATTERN.search(line) and current_function not in (
            _KNOWN_VACUOUS_DEMONSTRATIONS
        ):
            offending_lines.append(f"{path.name}:{lineno} (in {current_function}): {line.strip()}")
    assert not offending_lines, (
        "bare `in inspect.getsource(...)` found outside the known vacuous-guard "
        "demonstrations -- route it through fixtures.source_inspect.source_body "
        "instead:\n" + "\n".join(offending_lines)
    )
