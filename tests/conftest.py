"""Session-wide pytest configuration.

CLOSE11-4: a real (non-xfail) skip is invisible in an otherwise-green run
unless something is specifically watching for it -- the top-level summary
line still reads "N passed" and the process still exits 0. That is not
hypothetical here: `tests/test_cli.py::
test_main_resume_against_a_read_only_log_directory_gets_its_own_exit_code`
(CLOSE9-2's own real-filesystem permission test) silently skips under any
process running as root -- root bypasses the file-permission check the
test depends on -- which includes a root-running CI job or a
containerized agent, not just an unusual local setup.

`pytest_sessionfinish` below turns any skip into a failing session. The
premise: a skip nobody explicitly asked for deserves exactly as much
attention as a failure. If a future skip really is intentional and
permanent (an OS-specific test on the wrong platform, say), it should
carve itself out here explicitly -- by name, with its own reasoning --
rather than simply passing unnoticed the way this one did.
"""

from typing import cast

import pytest
from _pytest.terminal import TerminalReporter


def pytest_terminal_summary(
    terminalreporter: TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    terminalreporter.write_sep("=", "CLOSE11-4: a skip is treated as a failing build")
    for report in skipped:
        longrepr = cast("object", report.longrepr)
        reason = (
            str(cast("tuple[object, ...]", longrepr)[-1])
            if isinstance(longrepr, tuple)
            else str(longrepr)
        )
        terminalreporter.write_line(f"SKIPPED {report.nodeid}: {reason}")
    terminalreporter.write_line(
        "A skip nobody asked for is invisible in a green summary unless something "
        "is watching for it. If this skip is now permanent and intentional, carve "
        "it out here by name rather than letting it fail the build silently."
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus != pytest.ExitCode.OK:
        return
    terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is not None and terminalreporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
