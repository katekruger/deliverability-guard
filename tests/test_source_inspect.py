"""Tests for `fixtures/source_inspect.py`, and a structural guard against
the failure shape it exists to fix recurring a third time.

CLOSE8-3: an `in`/`.index()`/`.find()` check built on raw
`inspect.getsource` -- vulnerable to a docstring (CLOSE7-3) or a comment
(CLOSE8-3) alone making it pass -- has now appeared twice in two rounds.
`source_body` fixes both cases; this file makes a THIRD occurrence
something the suite itself catches, rather than something a future audit
has to find again.

CLOSE9-3: the guard added for CLOSE8-3 was itself measured against its own
target and found wanting, in exactly the way this project's audits keep
finding things wanting -- checked by assumption, not by execution. Two
holes, both confirmed by reapplying real mutations:

1. Shape. The original checker was a single-line regex,
   `r"(?<!not )\\bin\\s+inspect\\.getsource\\("`, which only matches `in`
   and `inspect.getsource(` on the SAME line. CLOSE8-3's own actual
   vacuous guard split this across two statements --
   `source = inspect.getsource(f)` on one line, `source.index(...)` or
   `"x" in source` on another -- which the regex never sees at all.
2. Scope. The original checker only globbed
   `Path(__file__).resolve().parent.glob("test_*.py")` -- NON-recursive,
   so `tests/experimental/`, `tests/providers/`, `tests/signals/`,
   `tests/loops/`, `tests/identity/`, and `tests/audit/` were never
   scanned.

Both fixed here: the checker is AST-based (parses each file, tracks which
names are bound directly to `inspect.getsource(...)` -- unwrapped by
`source_body` -- within a function, then flags any `in` comparison or
`.index()`/`.find()` call against either that name OR an inline
`inspect.getsource(...)` call, however many statements apart), and the
glob is `rglob`, recursive into every subdirectory.

CLOSE10-5: this guard covers the two INSTANCES CLOSE9-3 named, not the
CLASS of evasion a determined (or merely differently-styled) future
mutation could take. It is a literal-shape matcher, pinned to four things
at once: the attribute chain `inspect.getsource` specifically (not
`from inspect import getsource` or `import inspect as insp`), a direct
`Name = Call` assignment (not a walrus, an `import`-time alias, or a
helper function that wraps the call), the operators `in`/`.index`/`.find`
specifically (not `.count(...) > 0` or a raw byte-offset `re` search over
a file read directly from disk), and a `test_`-prefixed enclosing function
(the check itself is only ever invoked from inside one -- a non-`test_`
helper function calling `inspect.getsource` unsafely, then called BY a
test, is invisible to it). Nine such evasions were verified to pass this
guard vacuously before CLOSE10-5 corrected this docstring's own claim
(which previously said "however many statements apart" as though that
were the guard's only limitation):

```
from inspect import getsource        import inspect as insp
raw = getsource(f); src = raw        assert "x" in (src := inspect.getsource(f))
Path(mod.__file__).read_text()       inspect.getsource(f).count("x") > 0
def _src(f): return getsource(f)     the check in a non-test_ helper
```

`_KNOWN_VACUOUS_DEMONSTRATIONS` also exempts by function NAME, so an
exemption travels with a name rather than with the file or the specific
mutation shape -- a name reused by accident (or a rename that collides)
silently exempts unrelated code too.

This is a recorded judgment call, not a to-do: widening the guard to close
every one of the nine shapes above would mean re-implementing a real
taint-tracking analysis (which name is an alias for `inspect`, which
helper functions are themselves unsafe, ...) inside a test file, at a cost
this project has not decided is worth paying for a guard whose PRIMARY
value -- the two real, already-seen shapes -- is already closed. If a
tenth evasion shows up as a real mutation some future round finds, that is
the signal to revisit this trade-off with real evidence, not before.
"""

import ast
import textwrap
from pathlib import Path

import pytest

from fixtures.source_inspect import source_body

# The two existing regression tests that DELIBERATELY use raw
# `inspect.getsource` in an unsafe check -- to demonstrate what the
# vacuous, pre-fix guard looked like, right next to the fixed
# `source_body`-based version. Anything else this checker finds is a new
# instance of the class this file exists to prevent, not a demonstration
# of it.
_KNOWN_VACUOUS_DEMONSTRATIONS = frozenset(
    {
        "test_check_and_run_drift_guard_catches_a_hand_duplicated_loop_the_vacuous_one_missed",
        "test_act_paused_status_guard_catches_the_comment_only_mutation_the_vacuous_one_missed",
    }
)


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


def _is_raw_getsource_call(node: ast.expr) -> bool:
    """`inspect.getsource(...)` -- specifically NOT `source_body(...)`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getsource"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "inspect"
    )


class _UnsafeRawGetsourceUseFinder(ast.NodeVisitor):
    """Walks one function's body (CLOSE9-3: including nested `def`s inside
    it -- CLOSE7-3's and CLOSE8-3's own mutations both built a nested
    stand-in function to demonstrate the vacuous guard, so a checker that
    stops at the first nested scope would miss exactly the shape this
    project has already used twice). Tracks which names are bound directly
    to `inspect.getsource(...)` (never wrapped in `source_body`), then
    flags:

    - Any name so bound, or any inline `inspect.getsource(...)` call, used
      as either side of an `in` comparison (`ast.In` -- `ast.NotIn` is
      safe by construction and never flagged).
    - Any `.index(...)` or `.find(...)` call on either shape.

    Deliberately NOT scoped to "the same statement" -- CLOSE9-3's own
    finding is that the real mutation split this across two, so tracking
    the raw-bound NAME across the whole function is what closes that gap.
    """

    def __init__(self) -> None:
        self.raw_names: set[str] = set()
        self.offending_lines: list[int] = []

    def _is_raw(self, node: ast.expr) -> bool:
        return _is_raw_getsource_call(node) or (
            isinstance(node, ast.Name) and node.id in self.raw_names
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_raw_getsource_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.raw_names.add(target.id)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for op, operand in zip(node.ops, operands[1:], strict=True):
            if isinstance(op, ast.In) and (self._is_raw(operands[0]) or self._is_raw(operand)):
                self.offending_lines.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("index", "find")
            and self._is_raw(func.value)
        ):
            self.offending_lines.append(node.lineno)
        self.generic_visit(node)


def _offending_lines_in_function(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    finder = _UnsafeRawGetsourceUseFinder()
    finder.visit(func_node)
    return finder.offending_lines


@pytest.mark.parametrize(
    "path",
    sorted(Path(__file__).resolve().parent.rglob("test_*.py")),
    ids=lambda p: str(p.relative_to(Path(__file__).resolve().parent)),
)
def test_no_test_function_uses_raw_inspect_getsource_unsafely(path: Path) -> None:
    """CLOSE8-3's structural guard, fixed for CLOSE9-3's two holes: scans
    every test file, RECURSIVELY (subdirectories included), via `ast`
    rather than a single-line regex -- so a raw `inspect.getsource(...)`
    result bound directly to a name and then used in an `in` check or
    `.index()`/`.find()` call, via THAT exact attribute chain, is caught
    regardless of how many statements separate the two, and regardless of
    which subdirectory the file lives in. Outside the two known,
    deliberate demonstrations above, any match means a new source-level
    guard was written the same vulnerable way CLOSE7-3, CLOSE8-3, AND
    CLOSE9-3's own reproduction all found -- fix it by routing through
    `source_body` instead, the same way every real guard in this suite
    already does.

    CLOSE10-5: this is a literal-shape matcher, not a general taint
    tracker -- it does not follow `from inspect import getsource`,
    `import inspect as insp`, an intermediate re-binding (`raw = src;
    src2 = raw`), a walrus (`:=`), `.count(...) > 0`, a direct
    `Path(...).read_text()` re-read of the module's own file, or an
    unsafe use tucked inside a non-`test_`-prefixed helper function. See
    the module docstring's "Known scope" note for the full list of shapes
    verified to pass this guard vacuously; this is a recorded limitation,
    not a claim the guard doesn't have."""
    tree = ast.parse(path.read_text())
    offending: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        if node.name in _KNOWN_VACUOUS_DEMONSTRATIONS:
            continue
        for lineno in _offending_lines_in_function(node):
            offending.append(f"{path.name}:{lineno} (in {node.name})")
    assert not offending, (
        "raw `inspect.getsource(...)` used unsafely (an `in` check, `.index()`, "
        "or `.find()`, with or without an intermediate variable) found outside "
        "the known vacuous-guard demonstrations -- route it through "
        "fixtures.source_inspect.source_body instead:\n" + "\n".join(offending)
    )


# --- CLOSE9-3's own reproduction: reapply both original holes -------------


def test_checker_catches_the_two_statement_shape() -> None:
    """Hole 1: CLOSE8-3's OWN actual vacuous guard -- `source = inspect.
    getsource(f)` on one line, `source.index(...)` on another -- which the
    old single-line regex never saw. Verified directly against this
    file's own `ast`-based finder, on synthetic source matching that exact
    shape (not the real, already-fixed `_act` guard)."""
    source = textwrap.dedent("""
        def test_something_vacuous() -> None:
            source = inspect.getsource(some_module.some_function)
            paused_check_index = source.index("MailboxBreakerStatus.PAUSED")
            throttle_call_index = source.index("effective_driver.throttle(")
            assert paused_check_index < throttle_call_index
    """)
    tree = ast.parse(source)
    (func_node,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    assert _offending_lines_in_function(func_node), (
        "the checker failed to catch the two-statement shape -- "
        "source = inspect.getsource(...) followed by source.index(...)"
    )


def test_checker_catches_a_bare_in_comparison_two_statements_later() -> None:
    """The `in`-flavoured sibling of the same two-statement shape (as
    opposed to `.index()`)."""
    source = textwrap.dedent("""
        def test_something_else_vacuous() -> None:
            body = inspect.getsource(some_module.some_function)
            unrelated = 1 + 1
            assert "forbidden_name" in body
    """)
    tree = ast.parse(source)
    (func_node,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    assert _offending_lines_in_function(func_node)


def test_checker_scans_subdirectories_recursively(tmp_path: Path) -> None:
    """Hole 2: the original glob was non-recursive, so `tests/experimental/`
    and friends were never scanned. Verified against a synthetic directory
    tree (never the real `tests/` -- this must not depend on any real file
    existing), asserting the same `rglob` pattern this checker's own
    top-level `parametrize` uses finds a file nested inside a
    subdirectory, not just one at the top level."""
    (tmp_path / "top_level_test.py").write_text("def test_x() -> None:\n    pass\n")
    nested_dir = tmp_path / "experimental"
    nested_dir.mkdir()
    offending_source = textwrap.dedent("""
        def test_nested_vacuous() -> None:
            source = inspect.getsource(some_module.some_function)
            assert "forbidden_name" in source
    """)
    (nested_dir / "test_nested.py").write_text(offending_source)

    found_paths = sorted(tmp_path.rglob("test_*.py"))
    assert nested_dir / "test_nested.py" in found_paths

    # And the AST check itself, run against the nested file exactly the
    # way the real parametrized test runs it, actually flags it.
    tree = ast.parse((nested_dir / "test_nested.py").read_text())
    (func_node,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "test_nested_vacuous"
    ]
    assert _offending_lines_in_function(func_node)
