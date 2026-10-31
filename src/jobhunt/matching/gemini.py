"""A small Gemini REST client (embeddings and structured generation).

Plain `httpx` against the documented REST endpoints, so it needs no SDK, is trivially mocked with
`respx`, and is the only place that ever holds the API key. The key travels in the
`x-goog-api-key` header (never in a URL, so it cannot end up in a log line) and is never logged.

Retry policy mirrors the ingestion HTTP client: 429 and 5xx are retried with exponential backoff
and jitter, `Retry-After` is honored when present (Google's docs do not promise it), a retry delay
beyond the cap gives up at once. Everything else is returned as a typed error.

Spring Boot analogy: a `@FeignClient` with a retry interceptor.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from jobhunt.config import GeminiSettings
from jobhunt.errors import JobHuntError
from jobhunt.http import RETRYABLE_STATUS_CODES, parse_retry_after

logger = logging.getLogger(__name__)

_NORMAL_NORM_TOLERANCE = 1e-3


class GeminiError(JobHuntError):
    """Any failed Gemini call."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GeminiAuthError(GeminiError):
    """401 / 403: the key is missing, revoked or not allowed for this model."""


class GeminiQuotaError(GeminiError):
    """429 that did not clear within the retry budget (rate limit or spend limit)."""


class GeminiBadRequest(GeminiError):
    """4xx other than auth/quota: the request itself is wrong (model id, schema, size)."""


class GeminiResponseError(GeminiError):
    """A 200 whose body is not what the contract promises (count mismatch, empty, blocked)."""


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    input_tokens: int | None = None


@dataclass(frozen=True)
class GenerateResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0          # candidates + thinking tokens: both bill as output
    thinking_tokens: int = 0
    finish_reason: str | None = None
    model_version: str | None = None


@dataclass
class CallLog:
    """What the client did, for the usage ledger and for tests."""

    requests: int = 0
    retries: int = 0
    waits: list[float] = field(default_factory=list)


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        raise GeminiResponseError("embedding has zero length")
    return [value / norm for value in vector]


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        settings: GeminiSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not api_key or not api_key.strip():
            raise GeminiAuthError("GEMINI_API_KEY is not set (put it in .env)")
        self._settings = settings
        self._sleep = sleep
        self._jitter = jitter
        self.log = CallLog()
        self._client = httpx.Client(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={"x-goog-api-key": api_key.strip(), "Content-Type": "application/json"},
            timeout=httpx.Timeout(settings.timeout_seconds, connect=15.0),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GeminiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- embeddings -------------------------------------------------------------------------

    def embed_batch(
        self, model: str, texts: list[str], *, dimensions: int, task_type: str | None = None
    ) -> EmbedResult:
        """One vector per text, in order. Uses `batchEmbedContents` with one request per input.

        (Sending several texts as parts of ONE content makes `gemini-embedding-2` return a single
        aggregated vector; this method can never do that.) `task_type` is only for models that
        support it (`gemini-embedding-001`); embedding-2 takes the task in the prompt text.
        """
        if not texts:
            return EmbedResult([])
        requests: list[dict[str, Any]] = []
        for text in texts:
            item: dict[str, Any] = {
                "model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": dimensions,
            }
            if task_type:
                item["taskType"] = task_type
            requests.append(item)
        body = self._post(f"models/{model}:batchEmbedContents", {"requests": requests})
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            got = len(embeddings) if isinstance(embeddings, list) else "none"
            raise GeminiResponseError(f"expected {len(texts)} embeddings from {model}, got {got}")
        vectors: list[list[float]] = []
        for index, embedding in enumerate(embeddings):
            values = embedding.get("values") if isinstance(embedding, dict) else None
            if not isinstance(values, list) or len(values) != dimensions:
                size = len(values) if isinstance(values, list) else "none"
                raise GeminiResponseError(f"embedding {index} from {model} has {size} values, expected {dimensions}")
            vectors.append([float(value) for value in values])
        usage = body.get("usageMetadata") if isinstance(body.get("usageMetadata"), dict) else {}
        tokens = usage.get("promptTokenCount") or usage.get("totalTokenCount")
        return EmbedResult(vectors, int(tokens) if isinstance(tokens, int) else None)

    # -- generation -------------------------------------------------------------------------

    def generate_json(
        self,
        model: str,
        *,
        system_instruction: str,
        user_text: str,
        response_schema: dict[str, Any],
        thinking_level: str | None = None,
        max_output_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_budget: int | None = None,
    ) -> GenerateResult:
        """One `generateContent` call constrained to a JSON schema (structured output). `thinking_budget` (tokens) is for
        the 2.5 models, whose thinking otherwise shares `max_output_tokens` and can use all of it (the JSON is then cut off)."""
        body = build_generate_body(system_instruction, user_text, response_schema, thinking_level,
                                   max_output_tokens, temperature, thinking_budget)
        response = self._post(f"models/{model}:generateContent", body)
        return parse_generate_response(model, response)

    # -- Batch API (50% of the interactive price, results within 24 hours) ----------------------

    def submit_batch(self, model: str, items: list[tuple[str, dict[str, Any]]], display_name: str) -> str:
        """Create a batch of `generateContent` requests inline; returns the batch resource name."""
        body = {"batch": {"display_name": display_name, "input_config": {"requests": {"requests": [
            {"request": request, "metadata": {"key": key}} for key, request in items]}}}}
        response = self._post(f"models/{model}:batchGenerateContent", body)
        name = response.get("name") or (response.get("metadata") or {}).get("name")
        if not isinstance(name, str) or not name.startswith("batches/"):
            raise GeminiResponseError(f"batch creation returned no batch name: {str(response)[:160]}")
        return name

    def get_batch(self, name: str) -> dict[str, Any]:
        return self._post(name, {}, method="GET")

    # -- transport --------------------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any], method: str = "POST") -> dict[str, Any]:
        attempts = self._settings.max_retries + 1
        problem = "no attempt made"
        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                self.log.requests += 1
                response = (self._client.get(path) if method == "GET" else self._client.post(path, json=body))
            except httpx.TimeoutException:
                problem = "timeout"
            except httpx.TransportError as error:
                problem = f"network error ({type(error).__name__})"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        data = response.json()
                    except ValueError as error:
                        raise GeminiResponseError(f"{path}: response is not JSON") from error
                    if not isinstance(data, dict):
                        raise GeminiResponseError(f"{path}: unexpected response shape")
                    return data
                if status not in RETRYABLE_STATUS_CODES:
                    raise _error_for(status, path, response)
                problem = f"HTTP {status}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > self._settings.retry_after_cap_seconds:
                    raise GeminiQuotaError(f"{path}: {problem}, Retry-After {retry_after:.0f}s exceeds the cap",
                                           status=status)
            if attempt == attempts - 1:
                break
            delay = max(self._backoff(attempt), retry_after or 0.0)
            self.log.retries += 1
            self.log.waits.append(delay)
            logger.debug("%s: %s, retry %d/%d in %.1fs", path.split(":")[0], problem, attempt + 1, attempts - 1, delay)
            self._sleep(delay)
        if problem.startswith("HTTP 429"):
            raise GeminiQuotaError(f"{path.split(':')[0]}: rate limited after {attempts} attempts", status=429)
        raise GeminiError(f"{path.split(':')[0]}: {problem} after {attempts} attempts")

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self._settings.backoff_max_seconds, self._settings.backoff_base_seconds * (2 ** attempt))
        return ceiling / 2 + self._jitter() * ceiling / 2


def build_generate_body(system_instruction: str, user_text: str, response_schema: dict[str, Any],
                        thinking_level: str | None, max_output_tokens: int, temperature: float,
                        thinking_budget: int | None = None) -> dict[str, Any]:
    generation_config: dict[str, Any] = {
        "responseMimeType": "application/json", "responseJsonSchema": response_schema,
        "maxOutputTokens": max_output_tokens, "temperature": temperature,
    }
    if thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    elif thinking_budget is not None:
        generation_config["thinkingConfig"] = {"thinkingBudget": int(thinking_budget)}
    return {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": generation_config,
    }


def parse_generate_response(model: str, response: dict[str, Any]) -> GenerateResult:
    feedback = response.get("promptFeedback") or {}
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise GeminiResponseError(f"{model} blocked the prompt ({feedback.get('blockReason')})")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiResponseError(f"{model} returned no candidates")
    candidate = candidates[0]
    parts = ((candidate.get("content") or {}).get("parts")) if isinstance(candidate, dict) else None
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict) and not part.get("thought")) \
        if isinstance(parts, list) else ""
    usage = response.get("usageMetadata") if isinstance(response.get("usageMetadata"), dict) else {}
    thinking = int(usage.get("thoughtsTokenCount") or 0)
    return GenerateResult(
        text=text,
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0) + thinking,
        thinking_tokens=thinking,
        finish_reason=candidate.get("finishReason") if isinstance(candidate, dict) else None,
        model_version=response.get("modelVersion"),
    )


def _error_for(status: int, path: str, response: httpx.Response) -> GeminiError:
    summary = ""
    try:
        error = response.json().get("error", {})
        summary = f"{error.get('status', '')}: {error.get('message', '')}"[:300]
    except (ValueError, AttributeError):
        summary = response.text[:200]
    message = f"{path.split(':')[0]}: HTTP {status} {summary}".strip()
    if status in (401, 403):
        return GeminiAuthError(message, status=status)
    if status == 429:
        return GeminiQuotaError(message, status=status)
    return GeminiBadRequest(message, status=status)
