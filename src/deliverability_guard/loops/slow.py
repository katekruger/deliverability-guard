"""Slow loop: daily, tunes the fast loop's thresholds. Never trips the breaker itself.

Not yet implemented — Prompt 3 (BUILD-PLAN.md §5). Feeds on Postmaster data,
compliance verdicts, and hierarchical pooling. This separation is enforced in
types: the slow loop must not be able to call pause().
"""
