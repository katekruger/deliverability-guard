"""Sequential change detection (CUSUM/SPRT) on the bounce/complaint stream.

Not yet implemented — Prompt 1 (BUILD-PLAN.md §6). Fixed windows are the wrong
tool for a monitoring problem with a known dead time; sequential detection
catches a trend shift faster than any fixed-window rate.
"""
