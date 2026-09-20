"""Bounded async client for the TypeSafe System One API (Jev).

Jev is a non-autoregressive "System One" model. It takes a ``state`` plus a map
of typed ``questions`` and returns one typed answer per question in a single
pass. It does not generate text, and it cannot return a value outside the
options supplied in the request.

Palace uses exactly one primitive here: ``noul`` -- the model's probability
(0..1) that a stated claim about the state is true. That is the shape
TypeSafe's own re-ranking cookbook uses, and it avoids inventing a scoring
scale the model was never calibrated against.

Everything in this module is best-effort. Callers sit on the retrieval hot
path, so every transport, protocol, and validation failure is surfaced as
:class:`TypeSafeError` for the caller to convert into a baseline-ranking
fallback. Nothing here should ever fail a search request.

API contract (docs.typesafe.ai/api):

    POST {base_url}/systemone
    Authorization: Bearer <key>
    {"state": "...", "model": "jev-latest", "questions": {"k": {...}}}
    -> {"model": "...", "answers": {"k": {"type": "noul", "noul": 0.93}},
        "usage": {"input_tokens": 312, "output_tokens": 48}}
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TYPESAFE_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_TYPESAFE_MODEL = "jev-latest"

# Jev accepts 64k tokens total with 32k reserved for state plus the longest
# question. Palace never needs anything close to that: the documented failure
# mode is the opposite direction -- "accuracy falls as the state grows with
# content unrelated to the decision". One retrieval chunk plus the query is
# the whole decision, so the state stays deliberately small.
MAX_STATE_CHARS = 8_000

# Retrieval answers must stay comparable across candidates. A truncated tail
# is better than a dropped candidate, but a state this short carries no signal
# worth trusting, so the pair is skipped instead.
MIN_STATE_CHARS = 16


class TypeSafeError(RuntimeError):
    """Raised for any TypeSafe request that did not yield a usable answer."""

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class TypeSafeNotConfigured(TypeSafeError):
    """Raised when TypeSafe work is requested without a usable configuration."""


@dataclass(frozen=True)
class TypeSafeConfig:
    """Resolved TypeSafe connection settings.

    ``api_key`` is never logged and never included in error messages. Compare
    with :meth:`enabled` before building a client.
    """

    api_key: str = ""
    base_url: str = DEFAULT_TYPESAFE_BASE_URL
    model: str = DEFAULT_TYPESAFE_MODEL
    timeout_seconds: float = 2.0
    max_concurrency: int = 8

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def systemone_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/systemone"

    def validate(self) -> None:
        if not self.enabled:
            raise TypeSafeNotConfigured("TYPESAFE_API_KEY is required for TypeSafe requests")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TypeSafeNotConfigured("TYPESAFE_BASE_URL must be an http(s) URL")
        if self.timeout_seconds <= 0:
            raise TypeSafeNotConfigured("TYPESAFE_TIMEOUT_SECONDS must be greater than 0")
        if self.max_concurrency < 1:
            raise TypeSafeNotConfigured("TYPESAFE_MAX_CONCURRENCY must be at least 1")


def typesafe_config_from_settings(settings: Any) -> TypeSafeConfig:
    return TypeSafeConfig(
        api_key=str(getattr(settings, "typesafe_api_key", "") or ""),
        base_url=str(getattr(settings, "typesafe_base_url", DEFAULT_TYPESAFE_BASE_URL)),
        model=str(getattr(settings, "typesafe_model", DEFAULT_TYPESAFE_MODEL)),
        timeout_seconds=float(getattr(settings, "typesafe_timeout_seconds", 2.0)),
        max_concurrency=int(getattr(settings, "typesafe_max_concurrency", 8)),
    )


@dataclass(frozen=True)
class NoulRequest:
    """One (state, claim) pair to evaluate.

    ``key`` is echoed back on the result so callers can rejoin answers to
    their own candidates without depending on ordering.
    """

    key: Any
    state: str
    instructions: str
    true_meaning: str | None = None
    false_meaning: str | None = None


@dataclass(frozen=True)
class NoulResult:
    key: Any
    noul: float
    input_tokens: int = 0
    output_tokens: int = 0


def _truncate_state(state: str) -> str:
    cleaned = state.strip()
    if len(cleaned) <= MAX_STATE_CHARS:
        return cleaned
    return cleaned[:MAX_STATE_CHARS]


def _question_payload(request: NoulRequest) -> dict[str, Any]:
    question: dict[str, Any] = {"type": "noul", "instructions": request.instructions}
    # Jev reads instructions literally, so spelling out what "yes" and "no"
    # mean measurably tightens the distribution. Both sides must be present:
    # a half-filled criteria map is a contradictory instruction, which is a
    # documented failure mode.
    if request.true_meaning and request.false_meaning:
        question["criteria"] = {"true": request.true_meaning, "false": request.false_meaning}
    return question


def _parse_noul(payload: Any, question_key: str) -> float:
    if not isinstance(payload, dict):
        raise TypeSafeError("TypeSafe response was not a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise TypeSafeError("TypeSafe response is missing an answers object")
    answer = answers.get(question_key)
    if not isinstance(answer, dict):
        raise TypeSafeError(f"TypeSafe response is missing answer {question_key!r}")
    raw = answer.get("noul")
    # bool is a subclass of int; a boolean here means the contract changed.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeSafeError("TypeSafe noul answer was not a number")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise TypeSafeError("TypeSafe noul answer was outside the 0..1 range")
    return value


def _parse_usage(payload: Any) -> tuple[int, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    def _count(key: str) -> int:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(int(value), 0)
    return _count("input_tokens"), _count("output_tokens")


class TypeSafeClient:
    """Minimal async client over ``POST /v1/systemone``.

    The client owns no global state. Callers that issue many requests should
    reuse one instance so the underlying connection pool survives; opening a
    fresh TLS connection per candidate would dominate the retrieval budget.
    """

    _QUESTION_KEY = "q"

    def __init__(self, config: TypeSafeConfig, *, client: httpx.AsyncClient | None = None) -> None:
        config.validate()
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    @property
    def config(self) -> TypeSafeConfig:
        return self._config

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout_seconds),
                # One search fans out to many candidates at once. Size the pool
                # to the concurrency gate so requests never queue behind it.
                limits=httpx.Limits(
                    max_connections=self._config.max_concurrency,
                    max_keepalive_connections=self._config.max_concurrency,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "TypeSafeClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def ask_noul(self, request: NoulRequest) -> NoulResult:
        """Evaluate one claim against one state.

        Raises :class:`TypeSafeError` on any transport, status, or contract
        failure. Never raises anything else.
        """

        state = _truncate_state(request.state)
        if len(state) < MIN_STATE_CHARS:
            raise TypeSafeError("TypeSafe state is too short to evaluate")

        body = {
            "state": state,
            "model": self._config.model,
            "questions": {self._QUESTION_KEY: _question_payload(request)},
        }
        client = self._ensure_client()
        async with self._semaphore:
            try:
                response = await client.post(
                    self._config.systemone_endpoint,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise TypeSafeError("TypeSafe request timed out", retryable=True) from exc
            except httpx.HTTPError as exc:
                # Deliberately drops the exception text: httpx includes the
                # request URL, and the key travels in a header, not the URL --
                # but keeping the surface narrow is cheaper than auditing it.
                raise TypeSafeError(
                    f"TypeSafe transport error: {exc.__class__.__name__}", retryable=True
                ) from exc

        status = response.status_code
        if status != 200:
            # 429 rate limited and 529 overloaded are the two the docs call out
            # as worth retrying; 401 and 422 are configuration bugs.
            raise TypeSafeError(
                f"TypeSafe request failed with status {status}",
                retryable=status in {429, 529} or status >= 500,
                status_code=status,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise TypeSafeError("TypeSafe response was not valid JSON") from exc

        noul = _parse_noul(payload, self._QUESTION_KEY)
        input_tokens, output_tokens = _parse_usage(payload)
        return NoulResult(
            key=request.key,
            noul=noul,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def score_noul_batch(self, requests: Sequence[NoulRequest]) -> list[NoulResult]:
        """Evaluate many pairs concurrently, dropping the ones that failed.

        A partial result is still useful to a reranker: candidates without an
        answer keep their baseline score. The caller decides whether too few
        answers means the whole rerank should be abandoned.
        """

        if not requests:
            return []
        settled = await asyncio.gather(
            *(self.ask_noul(request) for request in requests),
            return_exceptions=True,
        )
        results: list[NoulResult] = []
        failures = 0
        first_error: BaseException | None = None
        for outcome in settled:
            if isinstance(outcome, NoulResult):
                results.append(outcome)
                continue
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            failures += 1
            if first_error is None and isinstance(outcome, BaseException):
                first_error = outcome
        if failures:
            logger.warning(
                "TypeSafe batch returned %d/%d answers; first failure: %s",
                len(results),
                len(requests),
                first_error,
            )
        return results


# A rerank fans out one request per candidate and must finish inside a
# sub-second budget, so the TLS handshake cannot be paid per search. The
# client is cached per (config, event loop): httpx.AsyncClient binds to the
# loop that created it, and tests drive several loops through asyncio.run().
_SHARED_CLIENT: TypeSafeClient | None = None
_SHARED_CLIENT_KEY: tuple[TypeSafeConfig, int] | None = None


async def get_shared_typesafe_client(config: TypeSafeConfig) -> TypeSafeClient:
    global _SHARED_CLIENT, _SHARED_CLIENT_KEY

    key = (config, id(asyncio.get_running_loop()))
    if _SHARED_CLIENT is not None and _SHARED_CLIENT_KEY == key:
        return _SHARED_CLIENT
    # The previous client belongs to a different config or a dead loop. Drop
    # the reference without awaiting aclose(): closing a client from a loop it
    # does not belong to raises, and a dead loop has already released its
    # sockets.
    _SHARED_CLIENT = TypeSafeClient(config)
    _SHARED_CLIENT_KEY = key
    return _SHARED_CLIENT


async def reset_shared_typesafe_client() -> None:
    """Drop the cached client, closing it when the current loop owns it."""

    global _SHARED_CLIENT, _SHARED_CLIENT_KEY

    client = _SHARED_CLIENT
    key = _SHARED_CLIENT_KEY
    _SHARED_CLIENT = None
    _SHARED_CLIENT_KEY = None
    if client is None or key is None:
        return
    try:
        if key[1] == id(asyncio.get_running_loop()):
            await client.aclose()
    except RuntimeError:  # pragma: no cover - no running loop
        pass
