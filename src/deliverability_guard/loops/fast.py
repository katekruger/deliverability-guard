"""Fast loop: seconds-to-minutes reaction to leading indicators.

Not yet implemented — Prompt 3 (BUILD-PLAN.md §5). Bounce webhooks, SMTP codes
(4.7.31, 4.7.32, 5.7.515, 5.7.x), mailbox disconnect events, and TLS delivery
failures feed this loop. Complaint data lags 24h-3 days, so the fast loop must
act on what is observable now rather than waiting on a lagging signal.
"""
