"""Local Ollama client (mocked): embeddings, JSON output, errors, unload, local-only guard, provider switch."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from jobhunt.config import Config
from jobhunt.matching.embeddings import effective_config
from jobhunt.matching.ollama import OllamaClient, OllamaError, OllamaUnavailable


def client(handler) -> OllamaClient:  # noqa: ANN001
    return OllamaClient(transport=httpx.MockTransport(handler))


def test_refuses_a_non_local_server() -> None:
    with pytest.raises(ValueError):
        OllamaClient("http://10.0.0.5:11434")
    OllamaClient("http://127.0.0.1:11434").close()
    OllamaClient("http://localhost:11434").close()


def test_available_and_model_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40"})
        return httpx.Response(200, json={"models": [{"name": "nomic-embed-text:latest"}, {"name": "qwen3:4b"}]})

    c = client(handler)
    assert c.available() and c.models() == ["nomic-embed-text:latest", "qwen3:4b"]


def test_unavailable_server_is_reported_not_raised_by_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = client(handler)
    assert c.available() is False
    with pytest.raises(OllamaUnavailable):
        c.embed_batch("nomic-embed-text", ["x"])


def test_embed_batch_strips_the_prefix_and_checks_the_count() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        count = len(body["input"]) - (1 if body["input"][0] == "short" else 0)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]] * count, "prompt_eval_count": 7})

    c = client(handler)
    result = c.embed_batch("ollama:nomic-embed-text", ["a", "b"])
    assert seen[0]["model"] == "nomic-embed-text" and len(result.vectors) == 2 and result.input_tokens == 7
    with pytest.raises(OllamaError):
        c.embed_batch("nomic-embed-text", ["short", "x"])


def test_chat_json_sends_schema_temperature_zero_and_returns_text() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}, "prompt_eval_count": 50,
                                         "eval_count": 5})

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    result = client(handler).chat_json("qwen3:4b", system="s", user="u", schema=schema)
    assert result.text == '{"ok": true}' and result.input_tokens == 50 and result.output_tokens == 5
    assert seen["format"] == schema and seen["options"]["temperature"] == 0 and seen["stream"] is False


@pytest.mark.parametrize("response", [httpx.Response(500, text="boom"), httpx.Response(404, text="model not found"),
                                      httpx.Response(200, text="not json")])
def test_http_and_body_errors_become_ollama_errors(response: httpx.Response) -> None:
    with pytest.raises(OllamaError):
        client(lambda request: response).chat_json("m", system="s", user="u", schema={})


def test_timeout_is_an_error_not_a_hang() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(OllamaError):
        client(handler).chat_json("m", system="s", user="u", schema={}, timeout=1)


def test_unload_sends_keep_alive_zero_and_never_raises() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        raise httpx.ConnectError("gone")

    client(handler).unload("ollama:qwen3:4b")          # must not raise
    assert seen == [{"model": "qwen3:4b", "keep_alive": 0}]


def test_local_provider_swaps_model_and_removes_the_text_cap(config: Config) -> None:
    assert effective_config(config) is config
    local = dataclasses.replace(config, embedding=dataclasses.replace(config.embedding, provider="local"))
    effective = effective_config(local)
    assert effective.embedding.model == "ollama:nomic-embed-text"
    assert effective.embedding.dimensions == config.embedding.local_dimensions
    assert effective.budget.daily_embedding_texts >= 10**9
