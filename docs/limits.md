# What this tool cannot see

This is the honest-limits companion to `docs/statistics.md`. That document
is about a limit in the MATH -- what a posterior can and can't tell you at
low volume. This one is about a limit in the DATA itself: even with perfect
statistics, some questions are unanswerable from what's available, because
the information needed to answer them was never captured in the first
place.

## The attribution problem, stated plainly

Postmaster Tools gives you a domain-day scalar: "this domain's spam rate on
Tuesday was X." The sequencer (Instantly, Smartlead, whatever's actually
sending) gives you per-message events: "mailbox A sent message M to
recipient R in campaign C at time T." These are two completely different
shapes of data, produced by two completely different systems, and

**there is no join key between them.**

If Tuesday's domain-wide spam rate spikes, and three different campaigns
sent mail from that domain on Tuesday, there is no field anywhere in either
data source that lets you compute which campaign caused it. Not "difficult
to compute" -- not present in the data at all. Postmaster doesn't know
which campaign a complained-about message belonged to; it only knows the
domain that sent it. The sequencer knows the campaign; it has no visibility
into which of its messages generated a complaint that reached Google, or
when Google's own aggregation window closed around that complaint.

## Why this can't be fixed after the fact

This is the part worth being blunt about: **the tool's hardest problem is
fixed at SEND time, not measure time.** No amount of clever analysis
downstream of "Postmaster said X, the sequencer said Y" can manufacture a
join key that was never recorded. If you want to know which campaign caused
Tuesday's spike, you have to have arranged, before Tuesday, for Tuesday's
data to be separable by campaign in the first place.

Two mechanisms are available in this project (BUILD-PLAN.md §9):

1. **`Feedback-ID` headers** (`identity/feedback_id.py`). Postmaster v2
   exposes `FEEDBACK_LOOP_ID` and `FEEDBACK_LOOP_SPAM_RATE` specifically:
   set a `Feedback-ID:` header per campaign, and Postmaster reports spam
   rate *for that Feedback-ID*, not just the domain as a whole. This is a
   real, working join key -- but only for the `FEEDBACK_LOOP_SPAM_RATE`
   metric specifically, and only for mail that actually carries the header.
   `identity/feedback_id.py`'s `check_coverage` exists because a header
   that's supposed to be on every message and isn't is a silent gap in
   exactly the data you'd need during an incident.

2. **Subdomain segregation** (`identity/subdomain_advisor.py`). Every other
   Postmaster metric this project reads (`AUTH_SUCCESS_RATE`,
   `DELIVERY_ERROR_RATE`, and so on) has no per-campaign breakdown at all --
   Feedback-ID doesn't cover them. The only way to get campaign-class-level
   visibility into THOSE metrics is to make Postmaster's own per-domain
   aggregation do the work, by sending each campaign class from a distinct
   subdomain. Postmaster then reports `AUTH_SUCCESS_RATE` etc. per
   subdomain, which is per-campaign-class in practice.

**Neither of these is something this tool can do for you.** Both are
operational requirements imposed on whoever operates the sender
infrastructure -- a header that has to actually be set on outgoing mail, a
sending architecture that has to actually route campaigns to the right
subdomain. `identity/feedback_id.py` and `identity/subdomain_advisor.py`
generate the scheme and check self-reported consistency against it; neither
module sends mail, modifies outgoing messages, or can independently verify
what a mail server actually did. If you don't do the operational half, the
code half doesn't produce attribution -- it produces a scheme nobody's mail
follows, which is worse than no scheme at all, because it invites false
confidence.

## What this means in practice

Before adopting the identity scheme: a domain-wide spike is diagnosable as
"something happened to this domain," full stop. No further breakdown is
computable, no matter how the available data is sliced.

After adopting it, faithfully: a domain-wide spike becomes diagnosable down
to campaign class (via subdomain) and, for spam rate specifically, down to
individual campaign (via Feedback-ID) -- but only for mail sent after
adoption. Historical data from before the scheme was in place is still
unattributable; there's no way to retroactively assign a join key to
messages that already went out without one.

See also: `docs/statistics.md` (the low-volume statistical limit),
`docs/threat-model.md` (credential handling), and the README's honest-limits
section once it lands in the v0.1.0 release (Prompt 5).
