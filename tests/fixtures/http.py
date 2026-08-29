"""Shared httpx mock-transport helpers for provider driver tests.

No live API calls in tests, ever (AGENTS.md) -- every driver test builds its
`httpx.Client` from a queue of canned responses read from
tests/fixtures/{instantly,smartlead}/*.json via this module.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx

FIXTURES_DIR = Path(__file__).parent


def load_json(relative_path: str) -> object:
    return json.loads((FIXTURES_DIR / relative_path).read_text())


def response(status_code: int, relative_path: str) -> httpx.Response:
    return httpx.Response(status_code, json=load_json(relative_path))


def queued_client(responses: list[httpx.Response], *, base_url: str) -> httpx.Client:
    """An httpx.Client that returns `responses` in order, one per request,
    regardless of method/path -- enough to test a driver's parsing and its
    retry loop without asserting on request shape in the same helper."""
    queue: Iterator[httpx.Response] = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return next(queue)
        except StopIteration:
            raise AssertionError(
                f"mock transport ran out of canned responses (request: {request.method} "
                f"{request.url.path})"
            ) from None

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


def recording_client(
    responses: list[httpx.Response], *, base_url: str
) -> tuple[httpx.Client, list[httpx.Request]]:
    """Like `queued_client`, but also returns the list of requests actually
    made, so a test can assert on the request (e.g. that a URL never
    contains a secret in its query string)."""
    made: list[httpx.Request] = []
    queue: Iterator[httpx.Response] = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        made.append(request)
        try:
            return next(queue)
        except StopIteration:
            raise AssertionError(
                f"mock transport ran out of canned responses (request: {request.method} "
                f"{request.url.path})"
            ) from None

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)
    return client, made


def constant_sleep_recorder() -> tuple[Callable[[float], None], list[float]]:
    """A `sleep` replacement for request_with_retry that records durations
    instead of actually sleeping, so retry tests run instantly."""
    waited: list[float] = []

    def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    return fake_sleep, waited
