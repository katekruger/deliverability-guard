"""Beta-binomial posterior on complaint and bounce rates, with hierarchical pooling.

Not yet implemented — this is the subject of Prompt 1 (BUILD-PLAN.md §6). The
breaker must trip on the lower bound of the posterior credible interval, never
on a raw point estimate: at cold-outbound volume (e.g. 50 sends/day), a single
complaint is a 2% point estimate but is not, on its own, statistically
distinguishable from a healthy mailbox.
"""
