"""A sending circuit breaker for outbound email.

Watches reputation, bounce, complaint and warmup signals per mailbox and
throttles or pauses before a domain burns. See BUILD-PLAN.md for the design.
"""

__version__ = "0.1.0"
