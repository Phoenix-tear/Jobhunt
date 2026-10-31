"""Optional live smoke test against the real Gemini API. Skipped unless run with --live-gemini.

    pytest --live-gemini tests/test_live_gemini.py -v -s

A handful of tiny calls (a few hundred tokens, well under one cent) with synthetic, PII-free text.
They confirm that the model ids, the embedding behavior (one vector per input), the thinking
levels and structured output all work with the key in .env, and print the tokens used.
"""

from __future__ import annotations

import math

import pytest

from jobhunt.config import Config
from jobhunt.envfile import load_secrets
from jobhunt.matching.budget import estimate_cost
from jobhunt.matching.gemini import GeminiClient
from jobhunt.matching.prompts import CLASSIFY_SCHEMA, FIT_SCHEMA, Classification, FitAssessment, parse_answer
from jobhunt.timeutil import utc_now

pytestmark = pytest.mark.live_gemini


@pytest.fixture(scope="module")
def live_client(config: Config):  # noqa: ANN201
    key = load_secrets(config.project_root).get("GEMINI_API_KEY", "")
    if not key:
        pytest.skip("GEMINI_API_KEY is not set")
    with GeminiClient(key, config.gemini) as client:
        yield client


USAGE: list[tuple[str, int, int]] = []


def test_embedding_2_returns_one_unit_vector_per_input(live_client: GeminiClient, config: Config) -> None:
    texts = [
        "title: Machine Learning Engineer | text: Build LLM agents in Python.",
        "title: Frontend Engineer | text: React and TypeScript web apps.",
        "title: Barista | text: Make coffee for customers.",
    ]
    result = live_client.embed_batch(config.embedding.model, texts, dimensions=config.embedding.dimensions)
    assert len(result.vectors) == 3
    assert len({tuple(vector[:6]) for vector in result.vectors}) == 3
    for vector in result.vectors:
        assert len(vector) == config.embedding.dimensions
        assert abs(math.sqrt(sum(value * value for value in vector)) - 1.0) < 1e-3
    USAGE.append((config.embedding.model, result.input_tokens or 0, 0))


def test_the_preview_endpoint_id_also_works(live_client: GeminiClient, config: Config) -> None:
    result = live_client.embed_batch("gemini-embedding-2-preview", ["title: none | text: ping"], dimensions=128)
    assert len(result.vectors) == 1 and len(result.vectors[0]) == 128


def test_embedding_001_needs_manual_normalization(live_client: GeminiClient, config: Config) -> None:
    result = live_client.embed_batch(config.embedding.fallback_model, ["ping"], dimensions=768, task_type="RETRIEVAL_DOCUMENT")
    norm = math.sqrt(sum(value * value for value in result.vectors[0]))
    assert 0.1 < norm < 0.999                      # not unit length below 3072 dims: our code must normalize


def test_classify_model_structured_output_with_minimal_thinking(live_client: GeminiClient, config: Config) -> None:
    result = live_client.generate_json(
        config.models.classify, system_instruction="Triage a job posting. Output JSON only.",
        user_text="<job_posting>\ntitle: Machine Learning Engineer\nlocation: Remote - Worldwide\n</job_posting>",
        response_schema=CLASSIFY_SCHEMA, thinking_level=config.models.classify_thinking, max_output_tokens=200,
    )
    parsed = parse_answer(result.text, Classification)
    assert parsed.role_family in {"ai_ml", "fullstack", "other"}
    USAGE.append((config.models.classify, result.input_tokens, result.output_tokens))


def test_score_model_structured_output_with_lowest_thinking(live_client: GeminiClient, config: Config) -> None:
    result = live_client.generate_json(
        config.models.score, system_instruction="Score the fit of one posting for a new graduate. JSON only.",
        user_text="<job_posting>\ntitle: Junior Python Backend Engineer\nlocation: Bengaluru\n</job_posting>",
        response_schema=FIT_SCHEMA, thinking_level=config.models.score_thinking, max_output_tokens=600,
    )
    parsed = parse_answer(result.text, FitAssessment)
    assert 0 <= parsed.fit_score <= 100
    USAGE.append((config.models.score, result.input_tokens, result.output_tokens))


def test_report_usage(config: Config) -> None:
    total = sum(estimate_cost(config.pricing, model, tokens_in, tokens_out, utc_now().date())
                for model, tokens_in, tokens_out in USAGE)
    print("\nlive smoke usage (model, input tokens, output tokens):")
    for model, tokens_in, tokens_out in USAGE:
        print(f"  {model:<26} in={tokens_in:<6} out={tokens_out}")
    print(f"  estimated cost: ${total:.6f}")
    assert total < 0.05
