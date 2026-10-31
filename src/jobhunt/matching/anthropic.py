"""Optional Anthropic provider for tailoring advice (off by default; needs ANTHROPIC_API_KEY in .env).

Same interface as `GeminiClient.generate_json`, so the tailoring engine, the validator and the budget guards are
identical for either provider. Structured output is forced with a single tool whose input schema is the plan schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from jobhunt.matching.gemini import (
    GenerateResult, GeminiAuthError, GeminiBadRequest, GeminiError, GeminiQuotaError, GeminiResponseError,
)

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicClient:
    def __init__(self, api_key: str, *, timeout: float = 90.0, transport: httpx.BaseTransport | None = None) -> None:
        if not api_key:
            raise GeminiAuthError("ANTHROPIC_API_KEY is not set")
        self._client = httpx.Client(timeout=timeout, transport=transport, headers={
            "x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AnthropicClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def generate_json(self, model: str, *, system_instruction: str, user_text: str, response_schema: dict[str, Any],
                      thinking_level: str | None = None, max_output_tokens: int = 1024,    # noqa: ARG002
                      temperature: float = 0.0) -> GenerateResult:
        body = {
            "model": model, "max_tokens": max_output_tokens, "temperature": temperature, "system": system_instruction,
            "messages": [{"role": "user", "content": user_text}],
            "tools": [{"name": "submit", "description": "Return the structured answer.", "input_schema": response_schema}],
            "tool_choice": {"type": "tool", "name": "submit"},
        }
        try:
            response = self._client.post(API_URL, json=body)
        except httpx.TimeoutException as error:
            raise GeminiError("anthropic: timeout") from error
        except httpx.TransportError as error:
            raise GeminiError(f"anthropic: network error ({type(error).__name__})") from error
        if response.status_code in (401, 403):
            raise GeminiAuthError(f"anthropic: HTTP {response.status_code} (check ANTHROPIC_API_KEY)")
        if response.status_code == 429 or response.status_code == 529:
            raise GeminiQuotaError(f"anthropic: HTTP {response.status_code}", status=response.status_code)
        if response.status_code >= 400:
            raise GeminiBadRequest(f"anthropic: HTTP {response.status_code}: {response.text[:160]}")
        try:
            payload = response.json()
        except ValueError as error:
            raise GeminiResponseError("anthropic: response is not JSON") from error
        for block in payload.get("content", []) if isinstance(payload, dict) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                usage = payload.get("usage") or {}
                return GenerateResult(
                    text=json.dumps(block.get("input", {})), input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0), finish_reason=payload.get("stop_reason"),
                    model_version=payload.get("model"))
        raise GeminiResponseError("anthropic: no structured answer in the response")
