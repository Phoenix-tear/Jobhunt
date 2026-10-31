"""Approved summary variants. The model never free-writes a summary.

`data/profile/summaries.md` holds six drafted variants (unticked). Tailoring uses a variant only after you tick it;
until then the base resume's own summary is used verbatim. The only edit allowed on top of a variant is one closing
clause from a closed list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jobhunt.profile_terms import load_project_markers
from jobhunt.tailoring.facts import normalize
from jobhunt.tailoring.keywords import concepts
from jobhunt.tailoring.plan import numbers_in

CLOSING_CLAUSES = {
    "ai_agents_rag": "Looking for an AI engineering role.",
    "ml_data": "Looking for an applied machine learning role.",
    "fullstack": "Looking for a full-stack engineering role.",
    "backend_swe": "Looking for a backend software engineering role.",
    "salesforce": "Looking for a Salesforce and AI engineering role.",
    "fintech_bank": "Looking for an AI engineering role in financial technology.",
    "java_backend": "Looking for a Java backend engineering role.",
    "software_engineering": "Looking for a software engineering role.",
    "data_engineering": "Looking for a data engineering role.",
}


def closing_key(title: str, recipe_name: str) -> str:
    """The closing clause follows the job TITLE's family; the recipe decides only when the title says nothing."""
    low = title.lower()
    rules = (
        (r"salesforce|agentforce", "salesforce"),
        (r"full[- ]?stack|front[- ]?end|web developer", "fullstack"),
        (r"data engineer|analytics engineer|etl", "data_engineering"),
        (r"data scien|machine learning|\bml\b|applied scien|research scien", "ml_data"),
        (r"\bai\b|\bllm\b|genai|generative|agent|nlp", "ai_agents_rag"),
        (r"java\b|spring", "java_backend" if recipe_name == "java_backend" else "backend_swe"),
        (r"back[- ]?end|server[- ]?side|platform engineer|\bapi\b|systems engineer", "backend_swe"),
        (r"software (development )?engineer|\bsde\b|developer|programmer", "software_engineering"),
    )
    import re as _re

    for pattern, key in rules:
        if _re.search(pattern, low):
            if key == "ai_agents_rag" and recipe_name == "fintech_bank":
                return "fintech_bank"
            return key
    return recipe_name

DRAFTS: dict[str, tuple[str, str]] = {
    # Placeholder drafts. Replace them in the git-ignored data/profile/summaries.md with sentences that are true for you;
    # only claims backed by your confirmed facts should ever be ticked.
    "S1": ("AI / GenAI agents and RAG", "Software engineer who builds tool-using LLM agents and RAG assistants and measures whether they work, for example raising correct answers from 65% to 85%."),
    "S2": ("Applied ML / data science", "Engineer who trains and evaluates models against honest baselines, with Python, SQL and scikit-learn."),
    "S3": ("Full-stack web with AI", "Full-stack engineer shipping web systems with applied AI, from React frontends to Python and Node backends."),
    "S4": ("Backend / software engineering (intern or new grad)", "Backend engineer with production experience in APIs, databases and automated tests."),
    "S5": ("Salesforce / Agentforce plus AI", "Engineer who builds tool-using LLM agents with structured outputs and RAG on the Salesforce platform."),
    "S6": ("General AI & ML new grad", "Engineer who builds applied GenAI systems end to end and measures whether they work."),
}
_LINE = re.compile(r"^- \[(?P<mark>[ xX])\] \*\*(?P<id>S\d)\*\*[^:]*:\s*(?P<text>.+)$")


@dataclass(frozen=True)
class Variant:
    id: str
    label: str
    text: str
    approved: bool


def render_file(existing: dict[str, bool] | None = None, texts: dict[str, str] | None = None) -> str:
    existing = existing or {}
    out = [
        "# Summary variants (tick the ones you approve)",
        "",
        "Tailoring picks the best-matching ticked variant by role family and keywords, then may add one closing clause.",
        "Until one is ticked the base resume's own summary is used verbatim. Edit a variant's wording freely (keep it",
        "to facts on your resume); it is used byte for byte. Numbers are real, from ticked facts. Local file.",
        "",
    ]
    for key, (label, text) in DRAFTS.items():
        body = (texts or {}).get(key, text)
        out.append(f"- [{'x' if existing.get(key) else ' '}] **{key}** {label}: {body}")
    out.append("")
    out.append("Closing clauses (closed list, one may be appended): " + " | ".join(CLOSING_CLAUSES.values()))
    return "\n".join(out) + "\n"


def read_variants(path: Path) -> list[Variant]:
    if not path.is_file():
        return []
    found: list[Variant] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(line.strip())
        if match:
            key = match["id"]
            label = DRAFTS.get(key, (key, ""))[0]
            found.append(Variant(key, label, match["text"].strip(), match["mark"].lower() == "x"))
    return found


def write_drafts(path: Path, fact_texts: list[str]) -> list[str]:
    """Create the file if missing (never overwrites a file you have edited). Returns problems found in the drafts:
    every number in a draft must appear in the ticked facts."""
    problems: list[str] = []
    corpus = " ".join(fact_texts)
    known = numbers_in(corpus)
    for key, (_, text) in DRAFTS.items():
        unknown = sorted(numbers_in(text) - known - {"1"})
        if unknown:
            problems.append(f"{key}: numbers not in ticked facts: {unknown}")
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_file(), encoding="utf-8")
    return problems


def choose_variant(variants: list[Variant], recipe_variant: str, keywords: list[str], family: str) -> Variant | None:
    """Best approved variant: the recipe's own when approved, else the one whose text shares most job concepts."""
    approved = [v for v in variants if v.approved]
    if not approved:
        return None
    for variant in approved:
        if variant.id == recipe_variant:
            return variant
    scored = sorted(approved, key=lambda v: (-len(set(concepts(v.text)) & set(keywords)), v.id))
    return scored[0]


# Phrases that point at a project (or a whole project's headline claim). A summary may only mention a project that
# is on the rendered page; the project is identified by a fragment of its heading.
PROJECT_MARKERS: dict[str, tuple[str, ...]] = {
    # Add your own in the git-ignored data/profile/project_markers.txt ("heading fragment: marker | marker").
    **load_project_markers(),
}


def rank_variants(variants: list[Variant], wanted: str, recipe_variant: str, keywords: list[str]) -> list[Variant]:
    """Every approved variant, best first: the advised one, the recipe's own, then by shared job concepts."""
    approved = [v for v in variants if v.approved]
    rest = sorted(approved, key=lambda v: (-len(set(concepts(v.text)) & set(keywords)), v.id))
    order = [v for key in (wanted, recipe_variant) for v in approved if v.id == key and key]
    return list(dict.fromkeys([*order, *rest]))


def unsupported_mentions(summary: str, page_plain: str, project_headings: list[str]) -> list[str]:
    """What the summary claims that the rendered page does not show: a project that was left off, or a number that
    appears nowhere on the page. Empty means the summary is supported."""
    low = summary.lower()
    headings = " ".join(project_headings).lower()
    missing = [f"project:{fragment}" for fragment, markers in PROJECT_MARKERS.items()
               if any(marker in low for marker in markers) and fragment not in headings]
    page_numbers = numbers_in(page_plain)
    missing += [f"number:{n}" for n in sorted(numbers_in(summary) - page_numbers - {"1"})]
    return missing


def with_closing(variant_text: str, recipe_name: str) -> str:
    clause = CLOSING_CLAUSES.get(recipe_name)
    return f"{variant_text.rstrip()} {clause}" if clause and not variant_text.rstrip().endswith(clause) else variant_text


def is_approved_summary(text: str, variants: list[Variant]) -> bool:
    """Byte-identical to an approved variant, optionally followed by exactly one closing clause."""
    candidate = " ".join(text.split())
    for variant in variants:
        if not variant.approved:
            continue
        base = " ".join(variant.text.split())
        if candidate == base or any(candidate == f"{base} {clause}" for clause in CLOSING_CLAUSES.values()):
            return True
    return False


__all__ = ["CLOSING_CLAUSES", "closing_key", "DRAFTS", "PROJECT_MARKERS", "Variant", "choose_variant", "is_approved_summary",
           "normalize", "rank_variants", "read_variants", "render_file", "unsupported_mentions", "with_closing",
           "write_drafts"]
