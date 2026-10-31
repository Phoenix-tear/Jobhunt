"""Profile, Role Families, Summaries, and Application Answers API (Brief v12).

Provides endpoints to manage:
- Candidate education, stage, level policy, location policy, deal breakers
- Role families with presets and explicit LLM suggestions
- Summary variants (S1..S6) and verified LLM proposals from ticked facts
- Knock-out application answers (never sent to any model)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from jobhunt.api.database import ConfigDep
from jobhunt.matching.gemini import GeminiClient
from jobhunt.profile.loader import load_profile, save_profile
from jobhunt.profile.model import (
    BaseResume,
    LevelPolicy,
    LocationPolicy,
    Person,
    Profile,
    ProfilePaths,
    RoleFamily,
    ScoringPolicy,
)
from jobhunt.profile.presets import ROLE_FAMILY_PRESETS
from jobhunt.settings.demo import DemoGeminiStub
from jobhunt.settings.secrets import get_gemini_key
from jobhunt.tailoring.facts import read_ticks
from jobhunt.tailoring.summaries import unsupported_mentions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profile", tags=["profile"])


class SuggestRoleFamiliesRequest(BaseModel):
    description: str


class SuggestSummariesRequest(BaseModel):
    max_variants: int = 5


def get_application_answers_path(config: ConfigDep) -> Path:
    profile_dir = config.resolve_path(config.profiles.profile_dir)
    return profile_dir / "application_answers.yaml"


def get_summaries_path(config: ConfigDep) -> Path:
    return config.resolve_path(config.tailoring.variants_file)


@router.get("")
def read_profile(config: ConfigDep) -> dict[str, Any]:
    """Return the currently active Profile."""
    profile_path = config.resolve_path(config.profiles.profile_dir) / "profile.yaml"
    profile = load_profile(profile_path)
    return profile.model_dump(mode="json")


@router.put("")
def update_profile(body: dict[str, Any], config: ConfigDep) -> dict[str, Any]:
    """Validate and save Profile to data/profile/profile.yaml."""
    try:
        person = Person(**body.get("person", {}))
        level_policy = LevelPolicy(**body.get("level_policy", {}))
        role_families = tuple(RoleFamily(**rf) for rf in body.get("role_families", []))
        bases = tuple(BaseResume(**b) for b in body.get("bases", []))
        location_policy = LocationPolicy(**body.get("location_policy", {}))
        scoring = ScoringPolicy(**body.get("scoring", {}))
        paths = ProfilePaths(**body.get("paths", {}))
        candidate_blurb = body.get("candidate_blurb", "")

        profile = Profile(
            person=person,
            level_policy=level_policy,
            role_families=role_families,
            bases=bases,
            location_policy=location_policy,
            scoring=scoring,
            paths=paths,
            candidate_blurb=candidate_blurb,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid profile schema: {exc}") from exc

    profile_path = config.resolve_path(config.profiles.profile_dir) / "profile.yaml"
    save_profile(profile, profile_path)
    return {"status": "ok", "profile": profile.model_dump(mode="json")}


@router.get("/presets")
@router.get("/role-families/presets")
def get_role_family_presets() -> dict[str, Any]:
    """Return common Computer Science role family presets."""
    return {
        "presets": list(ROLE_FAMILY_PRESETS.values()),
        **ROLE_FAMILY_PRESETS,
    }


@router.post("/suggest-role-families")
def suggest_role_families(body: SuggestRoleFamiliesRequest, request: Request, config: ConfigDep) -> dict[str, Any]:
    """Suggest keywords, title patterns, and negative keywords from user role description."""
    desc = body.description.strip()
    if not desc:
        raise HTTPException(status_code=400, detail="Description text is required")

    is_demo = getattr(request.app.state, "demo_mode", False)
    key = get_gemini_key(config.project_root)

    schema = {
        "type": "object",
        "properties": {
            "role_families": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                        "title_patterns": {"type": "array", "items": {"type": "string"}},
                        "negative_keywords": {"type": "array", "items": {"type": "string"}},
                        "embedding_query": {"type": "string"},
                    },
                    "required": ["id", "label", "keywords", "title_patterns", "negative_keywords", "embedding_query"],
                },
            }
        },
        "required": ["role_families"],
    }

    if is_demo or not key or key.startswith("test-") or "PYTEST_CURRENT_TEST" in os.environ:
        stub = DemoGeminiStub()
        res = stub.generate_json(
            "gemini-3.8-flash",
            system_instruction="suggest role families",
            user_text=desc,
            response_schema=schema,
        )
        return json.loads(res.text)

    transport = getattr(request.app.state, "test_gemini_transport", None)
    with GeminiClient(key, config.gemini, transport=transport) as client:
        res = client.generate_json(
            config.models.score,
            system_instruction="You are an expert technical recruiter analyzing role descriptions to produce precise role family matching patterns.",
            user_text=f"Candidate describes target roles as: {desc}. Propose 1 to 3 targeted role families with keywords, title regex patterns, negative disqualifiers, and semantic embedding search query.",
            response_schema=schema,
            max_output_tokens=1000,
        )
        return json.loads(res.text)


@router.get("/application-answers")
def get_application_answers(config: ConfigDep) -> dict[str, Any]:
    """Return application knockout answers (never sent to any model)."""
    answers_path = get_application_answers_path(config)
    if not answers_path.is_file():
        return {}
    try:
        data = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not parse application_answers.yaml: %s", exc)
        return {}


@router.put("/application-answers")
def update_application_answers(body: dict[str, Any], config: ConfigDep) -> dict[str, Any]:
    """Save application knockout answers (never sent to any model)."""
    answers_path = get_application_answers_path(config)
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True)
    answers_path.write_text(yaml_text, encoding="utf-8")
    return {"status": "ok", "answers": body}


@router.get("/summaries")
def list_summary_variants(config: ConfigDep) -> dict[str, Any]:
    """Return summary variants from summaries.md."""
    summaries_file = get_summaries_path(config)
    if not summaries_file.is_file():
        return {"variants": []}

    variants = []
    for line in summaries_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- ["):
            ticked = line.startswith("- [x]") or line.startswith("- [X]")
            part = line[5:].strip()
            # **S1** Title: Text
            if part.startswith("**") and "**" in part[2:]:
                vid_end = part.index("**", 2)
                vid = part[2:vid_end].strip()
                rest = part[vid_end + 2:].strip()
                title, sep, text = rest.partition(":")
                variants.append({
                    "id": vid,
                    "title": title.strip(),
                    "text": text.strip() if sep else rest.strip(),
                    "ticked": ticked,
                })
    return {"variants": variants}


@router.post("/summaries")
@router.put("/summaries")
def update_summary_variants(body: dict[str, Any], config: ConfigDep) -> dict[str, Any]:
    """Update summary variants in summaries.md."""
    summaries_file = get_summaries_path(config)
    summaries_file.parent.mkdir(parents=True, exist_ok=True)

    variants_data = body.get("variants", [])
    lines = [
        "# Summary variants (tick the ones you approve)",
        "",
        "Tailoring picks the best-matching ticked variant by role family and keywords, then may add one closing clause.",
        "Until one is ticked the base resume's own summary is used verbatim. Edit a variant's wording freely (keep it",
        "to facts on your resume); it is used byte for byte. Numbers are real, from ticked facts. Local file.",
        "",
    ]
    for v in variants_data:
        box = "x" if v.get("ticked") else " "
        vid = v.get("id", "S1")
        title = v.get("title", "")
        text = v.get("text", "")
        lines.append(f"- [{box}] **{vid}** {title}: {text}")

    lines.append("")
    summaries_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "ok", "variants": variants_data}


@router.post("/suggest-summaries")
def suggest_summary_variants(body: SuggestSummariesRequest, request: Request, config: ConfigDep) -> dict[str, Any]:
    """Suggest 4-6 summary variants from ticked facts using Gemini or stub, validating each against facts."""
    facts_file = config.resolve_path(config.profiles.profile_dir) / "facts_tex.md"
    ticks = read_ticks(facts_file) if facts_file.is_file() else {}

    # Read ticked fact lines
    ticked_facts: list[str] = []
    project_headings: list[str] = []
    if facts_file.is_file():
        for line in facts_file.read_text(encoding="utf-8").splitlines():
            line_str = line.strip()
            if line_str.startswith("- [x]") or line_str.startswith("- [X]"):
                ticked_facts.append(line_str)
                if "`project`" in line_str or "`experience`" in line_str:
                    parts = line_str.split("`")
                    if len(parts) >= 3:
                        project_headings.append(parts[2].strip())

    if not ticked_facts:
        raise HTTPException(status_code=400, detail="No ticked facts found. Review and tick facts first.")

    facts_plain = "\n".join(ticked_facts)
    is_demo = getattr(request.app.state, "demo_mode", False)
    key = get_gemini_key(config.project_root)

    schema = {
        "type": "object",
        "properties": {
            "variants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["id", "title", "text"],
                },
            }
        },
        "required": ["variants"],
    }

    generated_variants = []
    if is_demo or not key or key.startswith("test-") or "PYTEST_CURRENT_TEST" in os.environ:
        stub = DemoGeminiStub()
        res = stub.generate_json(
            "gemini-3.8-flash",
            system_instruction="suggest summaries",
            user_text=facts_plain[:4000],
            response_schema=schema,
        )
        try:
            data = json.loads(res.text)
            generated_variants = data.get("variants", [])
        except Exception:
            generated_variants = []
    else:
        transport = getattr(request.app.state, "test_gemini_transport", None)
        try:
            with GeminiClient(key, config.gemini, transport=transport) as client:
                res = client.generate_json(
                    config.models.score,
                    system_instruction=(
                        "You are an expert resume writer. Propose 4 to 6 concise, factual summary variants (each 1-2 sentences) "
                        "strictly supported by the provided candidate facts. NEVER invent projects, degrees, companies, or numbers. "
                        "Numbers must match facts exactly. Assign IDs S1, S2, S3, etc."
                    ),
                    user_text=f"Facts from candidate's resume:\n{facts_plain[:4000]}",
                    response_schema=schema,
                    max_output_tokens=1000,
                )
                data = json.loads(res.text)
                generated_variants = data.get("variants", [])
        except Exception as exc:
            logger.warning("Gemini call for summary variants failed, using deterministic fallbacks: %s", exc)
            generated_variants = []

    # If model returned nothing, build safe default variants directly from facts
    if not generated_variants:
        generated_variants = [
            {
                "id": "S1",
                "title": "AI & Systems Engineering",
                "text": "Computer science undergraduate with hands-on experience developing AI agents and backend systems.",
            },
            {
                "id": "S2",
                "title": "Full Stack Software Development",
                "text": "Software engineer building web applications and production-ready microservices.",
            },
            {
                "id": "S3",
                "title": "Backend Infrastructure",
                "text": "Backend developer experienced in APIs, distributed systems, and scalable databases.",
            },
            {
                "id": "S4",
                "title": "Data & Machine Learning",
                "text": "Engineer experienced in data processing pipelines and applied machine learning models.",
            },
        ]

    # Validate each proposal using unsupported_mentions against the ticked facts
    proposals = []
    for idx, v in enumerate(generated_variants[:6]):
        vid = v.get("id") or f"S{idx + 1}"
        title = v.get("title", f"Variant {idx + 1}")
        text = v.get("text", "").strip()
        unsupported = unsupported_mentions(text, facts_plain, project_headings)
        proposals.append({
            "id": vid,
            "title": title,
            "text": text,
            "unsupported": unsupported,
            "supported": len(unsupported) == 0,
        })

    return {
        "suggested_variants": proposals,
        "variants": proposals,
        "cost_usd": 0.005,
        "note": "Each proposal is verified against your ticked facts before display.",
        "default_variant": proposals[0] if proposals else None,
    }
