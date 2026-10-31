"""A small client for a local Ollama server (127.0.0.1 only): embeddings, schema-constrained JSON, unload.

Local models are no more injection-proof than Gemini, so callers keep job text inside delimiters and
accept nothing but schema-valid JSON. Nothing here ever changes configuration or rules.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobhunt.matching.gemini import EmbedResult

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:11434"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class OllamaError(Exception):
    """The local server answered, but not usefully (bad JSON, HTTP error, model missing)."""


class OllamaUnavailable(OllamaError):
    """No server on 127.0.0.1:11434 (not installed / not running)."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_URL, *, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None, keep_alive: str | int = "5m") -> None:
        host = (urlsplit(base_url).hostname or "").lower()
        if host not in LOCAL_HOSTS:
            raise ValueError(f"Ollama must be local (127.0.0.1); refusing {host!r}")
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._client.close()

    def close(self) -> None:
        self._client.close()

    # -- availability ----------------------------------------------------------------------

    def available(self) -> bool:
        try:
            return self._client.get("/api/version", timeout=3.0).status_code == 200
        except Exception:
            return False

    def models(self) -> list[str]:
        body = self._json("GET", "/api/tags")
        return [str(item.get("name")) for item in body.get("models", []) if isinstance(item, dict)]

    # -- embeddings ------------------------------------------------------------------------

    def embed_batch(self, model: str, texts: list[str], *, dimensions: int | None = None,
                    task_type: str | None = None) -> EmbedResult:
        """Same shape as `GeminiClient.embed_batch`, so it drops into `embed_jobs`."""
        if not texts:
            return EmbedResult([])
        model = model.removeprefix("ollama:")
        body = self._json("POST", "/api/embed", {"model": model, "input": texts, "keep_alive": self.keep_alive,
                                                 "truncate": True})
        vectors = body.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError(f"expected {len(texts)} embeddings from {model}, got "
                              f"{len(vectors) if isinstance(vectors, list) else 'none'}")
        return EmbedResult([[float(x) for x in vector] for vector in vectors],
                           input_tokens=int(body.get("prompt_eval_count") or 0) or None)

    # -- structured generation -------------------------------------------------------------

    def chat_json(self, model: str, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 500,
                  num_ctx: int = 3072, timeout: float | None = None) -> ChatResult:
        payload = {
            "model": model.removeprefix("ollama:"), "stream": False, "format": schema, "keep_alive": self.keep_alive,
            "think": False,         # qwen3-style models would otherwise spend the token budget thinking
            "options": {"temperature": 0, "num_predict": max_tokens, "seed": 7, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        started = time.monotonic()
        body = self._json("POST", "/api/chat", payload, timeout=timeout)
        message = body.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise OllamaError("no message content in the answer")
        return ChatResult(text=text, input_tokens=int(body.get("prompt_eval_count") or 0),
                          output_tokens=int(body.get("eval_count") or 0), seconds=time.monotonic() - started)

    def unload(self, model: str) -> None:
        """Free the GPU: keep_alive 0 makes the server drop the model right away. Never raises."""
        try:
            self._client.post("/api/generate", json={"model": model.removeprefix("ollama:"), "keep_alive": 0},
                              timeout=15.0)
        except httpx.HTTPError as error:       # noqa: PERF203 - best effort
            logger.debug("unload %s failed: %s", model, error)

    # -- plumbing --------------------------------------------------------------------------

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None,
              timeout: float | None = None) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=payload, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
        except httpx.ConnectError as error:
            raise OllamaUnavailable(f"no Ollama server at {self.base_url}") from error
        except httpx.TimeoutException as error:
            raise OllamaError(f"timeout after {timeout or 'default'}s on {path}") from error
        except httpx.HTTPError as error:
            raise OllamaError(f"{type(error).__name__} on {path}") from error
        if response.status_code == 404:
            raise OllamaError(f"model or endpoint not found ({path}): {response.text[:120]}")
        if response.status_code >= 400:
            raise OllamaError(f"HTTP {response.status_code} on {path}: {response.text[:120]}")
        try:
            body = response.json()
        except ValueError as error:
            raise OllamaError(f"non-JSON answer on {path}") from error
        if not isinstance(body, dict):
            raise OllamaError(f"unexpected JSON on {path}")
        return body
