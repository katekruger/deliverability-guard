"""Shared helper for `in`-over-`inspect.getsource` source-level test guards.

CLOSE7-3: `inspect.getsource` includes a function's docstring, so an `in`
assertion checking for a name can pass purely because the docstring
mentions that name in prose -- even after the function's actual CODE stops
referencing it. That is a false pass, not a false failure: the opposite
idiom, `not in` (as `test_evaluate_never_calls_resume_after_human_review`
and `test_engine_breaker_module_never_constructs_a_campaign_ref` both use),
is safe from this exact mistake, because a docstring mention of the
forbidden name can only ever make a `not in` assertion correctly FAIL, never
incorrectly pass. Copying that idiom while flipping `not in` to `in` is
what let `test_check_and_run_share_the_same_evaluation_chokepoint` pass
against a hand-duplicated aggregation loop that never touched
`evaluate_all_mailboxes` at all -- the docstring still named it in prose.

`source_body` strips a function's own docstring out of its source before
returning it, so any `in` assertion built on it only passes because the
CODE references the name, never because the docstring does.

Deliberately AST-based, not `source.replace(func.__doc__, "")`: Python
3.13 dedents a multi-line `__doc__` at compile time (its leading whitespace
per continuation line is stripped from the STORED string), so `func.__doc__`
is no longer a verbatim substring of `inspect.getsource(func)` on 3.13+ --
a `.replace()`-based version silently does nothing there. Locating the
docstring's exact line span via `ast` instead is immune to that: it looks
at where the docstring statement sits in the source, never at the string
value CPython chose to store for `__doc__`.
"""

import ast
import inspect
import textwrap
from collections.abc import Callable


def source_body(func: Callable[..., object]) -> str:
    """`inspect.getsource(func)`, with `func`'s own docstring statement (if
    it has one) removed entirely.

    Use this instead of a bare `inspect.getsource(func)` any time the
    assertion built on top is `in`, not `not in`. A `not in` assertion
    needs no help -- a docstring mention only makes it fail, correctly, if
    anything; only an `in` assertion is vulnerable to a docstring alone
    making it pass.
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    func_node = tree.body[0]
    if not isinstance(func_node, ast.FunctionDef | ast.AsyncFunctionDef):
        return source  # pragma: no cover -- always a function/method in practice
    has_docstring = (
        func_node.body
        and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, ast.Constant)
        and isinstance(func_node.body[0].value.value, str)
    )
    if not has_docstring:
        return source
    doc_statement = func_node.body[0]
    lines = source.splitlines(keepends=True)
    del lines[doc_statement.lineno - 1 : doc_statement.end_lineno]
    return "".join(lines)
