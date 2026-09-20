import asyncio
import json

import httpx
import pytest

from app.services.typesafe import (
    KEEPALIVE_EXPIRY_SECONDS,
    MAX_STATE_CHARS,
    NoulRequest,
    TypeSafeClient,
    TypeSafeConfig,
    TypeSafeError,
    TypeSafeNotConfigured,
    typesafe_config_from_settings,
)


def _config(**overrides) -> TypeSafeConfig:
    base = {
        "api_key": "ts-test-key",
        "base_url": "https://api.typesafe.example/v1",
        "model": "jev-latest",
        "timeout_seconds": 2.0,
        "max_concurrency": 4,
    }
    base.update(overrides)
    return TypeSafeConfig(**base)


def _noul_response(value: float, *, question_key: str = "q") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-latest",
            "answers": {question_key: {"type": "noul", "noul": value}},
            "usage": {"input_tokens": 312, "output_tokens": 48},
        },
    )


def _client(handler, **config_overrides) -> TypeSafeClient:
    transport = httpx.MockTransport(handler)
    return TypeSafeClient(
        _config(**config_overrides),
        client=httpx.AsyncClient(transport=transport),
    )


def _request(**overrides) -> NoulRequest:
    base = {
        "key": "candidate-1",
        "state": "Title: Chart promotion\n\nPassage: Publishing a chart does not deploy it.",
        "instructions": "The passage answers the query: how do palace charts deploy",
        "true_meaning": "The passage answers the query.",
        "false_meaning": "The passage is only on a similar topic.",
    }
    base.update(overrides)
    return NoulRequest(**base)


def test_config_requires_api_key() -> None:
    with pytest.raises(TypeSafeNotConfigured):
        TypeSafeClient(_config(api_key=""))


def test_config_rejects_non_http_base_url() -> None:
    with pytest.raises(TypeSafeNotConfigured):
        TypeSafeClient(_config(base_url="ftp://api.typesafe.example/v1"))


def test_config_from_settings_reads_expected_fields() -> None:
    class _Settings:
        typesafe_api_key = "key"
        typesafe_base_url = "https://example.test/v1"
        typesafe_model = "jev-1.13.0"
        typesafe_timeout_seconds = 3.5
        typesafe_max_concurrency = 12

    config = typesafe_config_from_settings(_Settings())

    assert config.api_key == "key"
    assert config.model == "jev-1.13.0"
    assert config.timeout_seconds == 3.5
    assert config.max_concurrency == 12
    assert config.systemone_endpoint == "https://example.test/v1/systemone"


def test_ask_noul_sends_documented_request_shape() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _noul_response(0.93)

    async def run() -> None:
        async with _client(handler) as client:
            result = await client.ask_noul(_request())
        assert result.key == "candidate-1"
        assert result.noul == 0.93
        assert result.input_tokens == 312
        assert result.output_tokens == 48

    asyncio.run(run())

    assert seen["url"] == "https://api.typesafe.example/v1/systemone"
    assert seen["auth"] == "Bearer ts-test-key"
    body = seen["body"]
    assert body["model"] == "jev-latest"
    assert body["questions"]["q"]["type"] == "noul"
    assert body["questions"]["q"]["criteria"] == {
        "true": "The passage answers the query.",
        "false": "The passage is only on a similar topic.",
    }


def test_ask_noul_omits_criteria_when_only_one_side_is_supplied() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _noul_response(0.5)

    async def run() -> None:
        async with _client(handler) as client:
            await client.ask_noul(_request(false_meaning=None))

    asyncio.run(run())

    # A half-filled criteria map is a contradictory instruction to Jev, which
    # is a documented accuracy failure. Sending none is the safe shape.
    assert "criteria" not in seen["body"]["questions"]["q"]


def test_ask_noul_truncates_oversized_state() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["state"] = json.loads(request.content)["state"]
        return _noul_response(0.5)

    async def run() -> None:
        async with _client(handler) as client:
            await client.ask_noul(_request(state="x" * (MAX_STATE_CHARS + 5_000)))

    asyncio.run(run())

    assert len(seen["state"]) == MAX_STATE_CHARS


def test_ask_noul_rejects_state_that_is_too_short() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("request should not be sent")

    async def run() -> None:
        async with _client(handler) as client:
            with pytest.raises(TypeSafeError):
                await client.ask_noul(_request(state="tiny"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,retryable",
    [(401, False), (422, False), (429, True), (529, True), (503, True)],
)
def test_ask_noul_maps_status_codes_to_retryability(status: int, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    async def run() -> None:
        async with _client(handler) as client:
            with pytest.raises(TypeSafeError) as excinfo:
                await client.ask_noul(_request())
        assert excinfo.value.status_code == status
        assert excinfo.value.retryable is retryable
        # The key must never reach an error message that gets logged.
        assert "ts-test-key" not in str(excinfo.value)

    asyncio.run(run())


def test_ask_noul_rejects_timeout_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async def run() -> None:
        async with _client(handler) as client:
            with pytest.raises(TypeSafeError) as excinfo:
                await client.ask_noul(_request())
        assert excinfo.value.retryable is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {"answers": {}},
        {"answers": {"q": {"type": "noul"}}},
        {"answers": {"q": {"type": "noul", "noul": "0.9"}}},
        {"answers": {"q": {"type": "noul", "noul": True}}},
        {"answers": {"q": {"type": "noul", "noul": 1.4}}},
        {"answers": {"q": {"type": "noul", "noul": -0.1}}},
        {"answers": []},
        {},
    ],
)
def test_ask_noul_rejects_malformed_answers(payload: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run() -> None:
        async with _client(handler) as client:
            with pytest.raises(TypeSafeError):
                await client.ask_noul(_request())

    asyncio.run(run())


def test_ask_noul_rejects_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async def run() -> None:
        async with _client(handler) as client:
            with pytest.raises(TypeSafeError):
                await client.ask_noul(_request())

    asyncio.run(run())


def test_ask_noul_tolerates_missing_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.7}}})

    async def run() -> None:
        async with _client(handler) as client:
            result = await client.ask_noul(_request())
        assert result.noul == 0.7
        assert result.input_tokens == 0
        assert result.output_tokens == 0

    asyncio.run(run())


def test_score_noul_batch_returns_partial_results_when_some_pairs_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "bad" in body["state"]:
            return httpx.Response(422, json={"error": "nope"})
        return _noul_response(0.81)

    async def run() -> list:
        async with _client(handler) as client:
            return await client.score_noul_batch(
                [
                    _request(key="good-1", state="Passage: a good candidate body"),
                    _request(key="bad-1", state="Passage: a bad candidate body"),
                    _request(key="good-2", state="Passage: another good candidate"),
                ]
            )

    results = asyncio.run(run())

    assert sorted(result.key for result in results) == ["good-1", "good-2"]
    assert all(result.noul == 0.81 for result in results)


def test_score_noul_batch_returns_empty_for_no_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("request should not be sent")

    async def run() -> list:
        async with _client(handler) as client:
            return await client.score_noul_batch([])

    assert asyncio.run(run()) == []


def test_score_noul_batch_respects_max_concurrency() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
            return _noul_response(0.5)
        finally:
            in_flight -= 1

    async def run() -> None:
        async with _client(handler, max_concurrency=2) as client:
            await client.score_noul_batch(
                [_request(key=index, state=f"Passage: candidate body {index}") for index in range(8)]
            )

    asyncio.run(run())

    assert peak <= 2


def test_owned_client_holds_idle_connections_past_the_reranker_budget() -> None:
    """The pool must survive the gap between searches, not httpx's 5s default.

    A connection that expires between searches costs a fresh TLS handshake
    (~300ms from the cluster) inside the reranker deadline, which pushes the
    whole rerank over budget and discards answers the account already paid for.
    """

    client = TypeSafeClient(_config(max_concurrency=6))
    try:
        keepalive_expiry = client._ensure_client()._transport._pool._keepalive_expiry
    finally:
        asyncio.run(client.aclose())

    assert keepalive_expiry == KEEPALIVE_EXPIRY_SECONDS
    # The measured cold-connection penalty only pays off if idle connections
    # outlive a realistic gap between user searches.
    assert KEEPALIVE_EXPIRY_SECONDS >= 60.0


def test_owned_client_pool_matches_configured_concurrency() -> None:
    client = TypeSafeClient(_config(max_concurrency=6))
    try:
        pool = client._ensure_client()._transport._pool
    finally:
        asyncio.run(client.aclose())

    assert pool._max_connections == 6
    assert pool._max_keepalive_connections == 6
