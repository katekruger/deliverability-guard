"""The graduated response ladder: warn -> throttle -> pause.

Not yet implemented — Prompt 3 (BUILD-PLAN.md §5, §7). Dry-run is the default
and must produce decisions identical to the live path; dry-run is implemented
as a no-op provider decorator, never as a separate logic branch. No code path
may pause a mailbox without dry_run=False set explicitly.
"""
