"""Data-availability state machine: OK | INSUFFICIENT_DATA | STALE.

Not yet implemented — Prompt 1 (BUILD-PLAN.md §6, §8). Absence of data must
never be coerced to zero and read as good news. A domain that gets throttled
sends less, can drop below Postmaster's (unpublished) privacy threshold, and
disappear from reporting entirely — so a transition from having data to not
having data must be modeled as its own alert condition, not silence.
"""
