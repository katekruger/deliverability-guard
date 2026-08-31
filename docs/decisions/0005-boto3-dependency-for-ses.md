---
status: "accepted"
date: "2026-08-30"
deciders: "Kate Kruger"
---

# Depend on boto3 for the SES driver, rather than hand-rolling AWS request signing

## Context and Problem Statement

`providers/ses.py` (BUILD-PLAN.md §4 item #18, v0.3) needs to call two AWS
APIs: SESv2 (to pause/resume sending) and CloudWatch (to read `Send`/
`Bounce` metrics). Every other driver in this project (`instantly.py`,
`smartlead.py`, `lemlist.py`, `apollo.py`) talks to a plain REST API over
`httpx` with a bearer token or API key -- AWS's APIs additionally require
SigV4 request signing, a real cryptographic protocol (a per-request HMAC
derived from the AWS access key, secret key, region, service, and date).
How should this driver authenticate to AWS?

## Decision Drivers

- This project has been deliberately lean: `httpx`, `pyyaml`, `scipy`, and
  nothing else, before this driver.
- SigV4 is security-sensitive code. Getting it subtly wrong (a canonical
  request built incorrectly, a credential scope mismatch) fails in ways
  that are easy to miss in review and expensive to discover in production
  (silently-rejected or, worse, silently-*mis-scoped* requests).
- AGENTS.md requires a one-line justification for any new dependency, not
  a ban on them -- the bar is "is this worth the weight," not "never add
  one."

## Considered Options

- Hand-roll AWS SigV4 signing directly with `httpx`, matching every other
  driver's dependency footprint.
- Depend on `boto3` (and its `botocore` dependency), the official AWS SDK.
- Skip the SES driver entirely and leave it to a future contributor with
  an AWS account to verify against.

## Decision Outcome

Chosen option: "depend on `boto3`," because reimplementing SigV4 is
exactly the kind of security-sensitive cryptographic code this project
should not be hand-rolling for a single driver, when a well-maintained,
security-reviewed official implementation already exists. `boto3` is
mature, is maintained by AWS itself, and correctly handles credential
resolution (environment variables, shared config files, instance/role
credentials) that a hand-rolled signer would either have to reimplement
or, more likely, get wrong or skip -- and credential handling mistakes are
exactly the class of bug this project's threat model (`docs/threat-model.md`)
already cares about for every other provider's API key.

### Consequences

- Good, because request signing and credential resolution are AWS's own
  problem to keep correct, not this project's.
- Good, because `boto3`'s client objects are duck-typed enough that this
  driver defines its own minimal `Protocol` (`SesV2Client`,
  `CloudWatchClient` in `providers/ses.py`) covering only the handful of
  methods it calls, so tests inject plain fake objects -- no `moto`, no
  live AWS account, no additional *test*-only dependency.
- Bad, because `boto3`/`botocore` is a genuinely heavy addition (~15MB) to
  a project that installs in a fraction of that otherwise, and it pulls in
  its own dependencies (`urllib3`, `python-dateutil`, `jmespath`, `s3transfer`).
  Every `uv sync` for every contributor now pays that cost, whether or not
  they ever touch the SES driver.
- Bad, because this is the first provider driver in the project that
  doesn't share `providers/_retry.py`'s httpx-based retry-with-backoff --
  `boto3` has its own (different) retry configuration, so the SES driver's
  failure-handling shape is inconsistent with every sibling driver's.

### Confirmation

`tests/test_ses.py` exercises the driver entirely against fake
`SesV2Client`/`CloudWatchClient` implementations -- no `moto`, no live AWS
account, no network call of any kind. `pyproject.toml`'s `dependencies`
list gains exactly one new entry (`boto3`); `botocore` and its transitive
dependencies are not declared directly, matching how `httpx`'s own
transitive dependencies aren't declared directly either.

## Assumption this relies on

That a contributor's environment can always resolve `import boto3`
without also needing real AWS credentials just to run the test suite --
true today because every AWS-calling method in `SesDriver` accepts an
injected client and never constructs a real one unless the caller
explicitly omits it (`_new_sesv2_client`/`_new_cloudwatch_client`, both
marked `pragma: no cover` since they're only exercised with real
credentials).

## Known limitation

The endpoint/parameter shapes this driver calls (`put_configuration_set_sending_options`,
`put_account_sending_attributes`, CloudWatch's `get_metric_statistics`)
are believed correct based on boto3's own documented API surface, but --
same caveat as `providers/instantly.py` -- this environment has no live
AWS account to verify against. BUILD-PLAN.md §13 flagged the exact action
names as an open question before this driver existed; this ADR resolves
*how to authenticate*, not *whether the exact calls are correct*. Verify
against a real AWS account before trusting this driver against production
traffic.

## Pros and Cons of the Options

### Hand-roll SigV4 with httpx

- Good, because it keeps the project's dependency footprint uniform
  across every driver
- Bad, because it means writing and maintaining real cryptographic
  request-signing code for one driver, with all the ways that can go
  subtly wrong

### Depend on boto3 (chosen)

- Good, because request signing and credential resolution become AWS's
  problem, not this project's
- Bad, because of the dependency weight and the retry-handling
  inconsistency noted above

### Skip the SES driver

- Good, because it adds no risk or weight at all
- Bad, because BUILD-PLAN.md §4 lists it as in-scope for v0.3, and SES is
  "the only platform with native breaker primitives" among the surveyed
  ESPs that hasn't been implemented yet

## More Information

See `src/deliverability_guard/providers/ses.py` for the implementation and
`tests/test_ses.py` for the fake-client-based test suite.
