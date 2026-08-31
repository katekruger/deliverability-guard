"""Module reachability (CLOSE3-5): every module under `src/deliverability_guard`
outside `experimental/` must be either imported during a real `check`
invocation, or explicitly named below with a reason it isn't.

Three prior audit rounds found the same shape -- a module built, tested,
and never called from anywhere real. This test doesn't let that regress
silently: any module not reached by `check` must be named in
`_NOT_REACHABLE_FROM_CHECK`, with a real reason, or this test fails. Adding
an entry there is a decision (a genuine library-only module, or a
separately-wired entry point), not a rubber stamp -- see each named
module's own docstring for the actual reasoning this test's message points
to.

This runs `check` in a real, fresh subprocess (not in-process): by the time
any test module is collected, pytest has already imported most of this
project transitively through other test files' own imports, so checking
`sys.modules` in-process after calling `cli.main` would prove nothing about
what `check` itself reaches -- every module would already show as
"imported" regardless. A fresh interpreter, importing only `cli` and then
running `check`, is what actually answers "does check's own code path
import this."
"""

import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every module here is deliberately not reachable via `check` -- see the
# referenced module's own docstring for why, not just this one-liner.
_NOT_REACHABLE_FROM_CHECK: dict[str, str] = {
    "deliverability_guard.mcp_server": (
        "A separate, independently-wired entry point -- the "
        "deliverability-guard-mcp console script -- not part of check/run. "
        "See mcp_server.py's own module docstring and main()."
    ),
    "deliverability_guard.signals.postmaster": (
        "The compliance hard gate is wired through loops.fast."
        "evaluate_all_mailboxes's compliance_gate_tripped_for parameter "
        "(CLOSE3-5), so compliance_gate_tripped IS passed at that "
        "chokepoint now -- but no CLI caller constructs a live "
        "PostmasterClient yet (that needs a configured domain and an OAuth "
        "token, real separate setup). See loops/fast.py's module "
        "docstring: documented as unwired, not silently dropped."
    ),
    "deliverability_guard.identity.feedback_id": (
        "A directly-callable library utility -- README's 'full public "
        "surface' explicitly includes the identity modules used directly, "
        "not only through the CLI. Generating a Feedback-ID header is "
        "something a caller does at send time, and this project never "
        "sends mail (BUILD-PLAN.md's own non-goals)."
    ),
    "deliverability_guard.identity.subdomain_advisor": (
        "Same as identity.feedback_id: a directly-callable advisory "
        "library function, not a CLI feature."
    ),
    "deliverability_guard.signals.dmarc": (
        "A directly-callable library signal (DMARC auth-health via "
        "parsedmarc) -- not yet threaded into evaluate_all_mailboxes's "
        "ladder the way signals.postmaster's hard gate now can be. What "
        "verdict DMARC auth failure should produce on the ladder is a "
        "real, separate design decision, not an oversight."
    ),
    "deliverability_guard.signals.spamhaus": (
        "Same as signals.dmarc: a directly-callable library signal, not yet wired into the ladder."
    ),
}

_NOOP_CONFIG_TEMPLATE = """
provider: noop
complaint_rate_ladder:
  warn: 0.0005
  throttle: 0.0010
  pause: 0.0020
prior:
  alpha: 0.5
  beta: 500
dry_run: true
decision_log_path: {log_path}
"""


def _all_project_modules() -> set[str]:
    """Every real module under `src/deliverability_guard`, dotted, excluding
    `experimental/` (which is exempt by design -- that's what quarantining
    a module there means) and `__init__`-only namespace packages."""
    package_root = _REPO_ROOT / "src" / "deliverability_guard"
    modules: set[str] = set()
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root)
        if "experimental" in relative.parts:
            continue
        if path.name == "__init__.py":
            continue
        dotted = ".".join(("deliverability_guard", *relative.with_suffix("").parts))
        modules.add(dotted)
    return modules


def _run_real_check_and_collect_imports(tmp_path: Path) -> set[str]:
    config_path = tmp_path / "thresholds.yml"
    log_path = tmp_path / "decisions.jsonl"
    config_path.write_text(_NOOP_CONFIG_TEMPLATE.format(log_path=log_path), encoding="utf-8")
    script = textwrap.dedent(f"""
        import sys
        from deliverability_guard.cli import main
        main(["--config", {str(config_path)!r}, "check"])
        for name in sorted(sys.modules):
            if name == "deliverability_guard" or name.startswith("deliverability_guard."):
                print(name)
    """)
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no untrusted input
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return set(result.stdout.strip().splitlines())


def test_every_non_experimental_module_is_reachable_from_check_or_explicitly_exempted(
    tmp_path: Path,
) -> None:
    all_modules = _all_project_modules()
    reached = _run_real_check_and_collect_imports(tmp_path)

    unreached = all_modules - reached
    undocumented = unreached - set(_NOT_REACHABLE_FROM_CHECK)
    assert not undocumented, (
        f"these modules are not imported by a real `check` run and have no "
        f"documented reason in _NOT_REACHABLE_FROM_CHECK: {sorted(undocumented)}"
    )

    # No silent caps the other direction either: an exemption for a module
    # that's actually gone (renamed/deleted) rots into a lie about why it's
    # excused. Every exemption must name a module that genuinely exists.
    stale_exemptions = set(_NOT_REACHABLE_FROM_CHECK) - all_modules
    assert not stale_exemptions, (
        f"these exemptions in _NOT_REACHABLE_FROM_CHECK no longer correspond to "
        f"a real module -- remove them: {sorted(stale_exemptions)}"
    )
