"""Honest numbers about a tailored resume, and the skills my relevant jobs ask for that my ticked facts do not cover.

Both are read-only and deterministic (no model). Nothing here changes a resume or adds a fact.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from typing import Any

from jobhunt.tailoring.keywords import concepts, job_keyword_weights

LEVELS = ("Low", "Medium", "High")
COVERAGE_SHARE = 0.7          # how much of the job the page covers matters more than how much was moved around
ADAPTATION_PARTS = ("summary", "projects", "bullets", "skills")
CONCEPT_LABELS = {
    "spring boot": "Spring Boot", "jpa/hibernate": "JPA/Hibernate", "junit": "JUnit", "ci/cd": "CI/CD", "aws": "AWS",
    "sql": "SQL", "rest": "REST", "api": "APIs", "llm": "LLMs", "rag": "RAG", "gcp": "GCP", "html/css": "HTML/CSS",
    "oop": "OOP", "dsa": "data structures", "nlp": "NLP", "mlops": "MLOps", "genai": "GenAI", "postgresql": "PostgreSQL",
    "mysql": "MySQL", "mongodb": "MongoDB", "node.js": "Node.js", "next.js": "Next.js", "typescript": "TypeScript",
    "javascript": "JavaScript", "java": "Java", "kubernetes": "Kubernetes", "docker": "Docker", "soap": "SOAP",
}


def label(concept: str) -> str:
    return CONCEPT_LABELS.get(concept, concept)


def tailoring_strength(summary: Mapping[str, Any] | None, settings: Any) -> dict[str, Any]:
    """Low / Medium / High for one change summary: how much was adapted to the job (summary variant, project selection or
    order, bullets, skills) and how much of the job's weighted concepts the page covers. Low says why and what would help."""
    if not summary or not (summary.get("recorded") or summary.get("partial")) or summary.get("partial"):
        return {"level": "Unknown", "score": None, "coverage": None, "adaptation": None, "parts": {}, "missing": [],
                "why": ["this version's comparison with the base resume was not recorded"], "help": None,
                "text": "Tailoring strength unknown: the comparison was not recorded for this version."}
    keywords = summary.get("keywords") or {}
    total = int(keywords.get("total") or 0)
    coverage = keywords.get("weighted")
    if coverage is None:
        coverage = (int(keywords.get("coverage_output") or 0) / total) if total else 0.0
    coverage = float(coverage)
    projects = summary.get("projects") or {}
    skills = summary.get("skills") or {}
    parts = {
        "summary": bool((summary.get("summary") or {}).get("changed")),
        "projects": bool(projects.get("removed") or projects.get("added_from_pool") or projects.get("reordered")),
        "bullets": bool(summary.get("bullets")),
        "skills": bool(skills.get("group_order_changed") or skills.get("lines")),
    }
    adaptation = sum(parts.values()) / len(parts)
    score = COVERAGE_SHARE * coverage + (1 - COVERAGE_SHARE) * adaptation
    low, high = float(settings.strength_low_below), float(settings.strength_high_from)
    level = "Low" if score < low else "High" if score >= high else "Medium"
    missing = [str(m) for m in keywords.get("missing", [])]
    why: list[str] = []
    selection = (summary.get("selection") or {})
    dropped_anything = bool(projects.get("removed")) or any(b.get("dropped") for b in summary.get("bullets", []))
    if level == "Low":
        if not dropped_anything:
            why.append("all your relevant facts already fit on one page, so there was nothing less relevant to swap out")
        if missing:
            why.append("missing from your resume: " + ", ".join(label(m) for m in missing[:6]) + " (not on your resume, so never added)")
        if not parts["summary"]:
            why.append("no approved summary variant matched this job better than your base summary")
    advice = None
    if level in ("Low", "Medium") and missing:
        advice = ("add facts for the missing items you really have (see Skill gaps on the Health page), or a base resume "
                  "written for this kind of job (for Java: data/resumes/resume_backend.tex)")
    numbers = (f"{round(100 * coverage)}% of the job's weighted concepts covered (70% of the score); different from the base in "
               f"{sum(parts.values())} of {len(parts)} ways ({', '.join(k for k, v in parts.items() if v) or 'none'}) (30%)")
    _ = selection
    return {"level": level, "score": round(score, 3), "coverage": round(coverage, 3), "adaptation": round(adaptation, 3),
            "parts": parts, "missing": missing, "why": why, "help": advice,
            "text": f"{level}: {numbers}." + ("".join(f" {w[0].upper()}{w[1:]}." for w in why) if why else "")
                    + (f" What would help: {advice}." if advice else "")}


# --- skill gaps across my relevant jobs -----------------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_SECONDS = 600
DEFAULT_JOB_LIMIT = 1200
RELEVANT_SQL = """
    SELECT j.id, j.title, j.company_name, j.description_text, COALESCE(r.final_score, 0) AS score
    FROM jobs j JOIN job_relevance r ON r.job_id = j.id
    WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
      AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
      AND NOT EXISTS (SELECT 1 FROM job_auto_hidden h WHERE h.job_id = j.id AND h.restored_at IS NULL)
      AND j.description_text IS NOT NULL AND LENGTH(j.description_text) > 200
    ORDER BY score DESC, j.id DESC LIMIT ?"""


def skill_gaps(connection: sqlite3.Connection, fact_texts: list[str], *, top: int = 25, job_limit: int = DEFAULT_JOB_LIMIT,
               per_job: int = 16, examples: int = 3, use_cache: bool = True) -> dict[str, Any]:
    """The `top` concepts that my relevant open jobs ask for (in a job's top `per_job` weighted concepts) and none of my
    ticked facts mention, with how many jobs ask and example titles. Read-only; it never adds anything to a resume."""
    key = f"{hash(tuple(fact_texts))}|{top}|{job_limit}|{per_job}"
    cached = _CACHE.get(key)
    if use_cache and cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    have: set[str] = set()
    for text in fact_texts:
        have |= set(concepts(text))
    rows = connection.execute(RELEVANT_SQL, (job_limit,)).fetchall()
    asking: dict[str, list[tuple[float, str, str]]] = {}
    for row in rows:
        weights = job_keyword_weights(row["title"] or "", row["description_text"] or "")
        for concept, weight in list(weights.items())[:per_job]:
            if concept not in have:
                asking.setdefault(concept, []).append((weight, row["title"] or "", row["company_name"] or ""))
    ranked = sorted(asking, key=lambda c: (-len(asking[c]), -sum(w for w, _, _ in asking[c]), c))[:top]
    result = {
        "jobs_checked": len(rows), "job_limit": job_limit, "concepts_on_your_facts": len(have),
        "items": [{"concept": c, "label": label(c), "jobs": len(asking[c]),
                   "examples": [f"{title} ({company})" for _, title, company in
                                sorted(asking[c], key=lambda t: -t[0])[:examples]]} for c in ranked],
        "note": "Read-only. Concepts the postings ask for that none of your ticked facts mention. Nothing here is added to "
                "any resume: decide which of these you genuinely have, then add them as facts yourself.",
    }
    _CACHE[key] = (time.monotonic(), result)
    return result


__all__ = ["ADAPTATION_PARTS", "LEVELS", "label", "skill_gaps", "tailoring_strength"]
