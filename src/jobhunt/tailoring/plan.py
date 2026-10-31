"""The tailoring plan (what the model may return) and the deterministic no-fabrication validator.

A plan can only SELECT and REORDER facts you ticked, write a short summary from them, and (optionally) lightly
rephrase a bullet. Nothing new may enter: every number, technology term, employer, title and date must trace back
to a cited, ticked fact. The validator is pure Python (no model) and returns a structured report that is stored
with the result and shown in the UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jobhunt.tailoring.facts import TexFact, normalize

# --- schema ---------------------------------------------------------------------------------------------------------


class EntryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bullets: list[str] = Field(default_factory=list, max_length=8)


class SkillPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    items: list[str] = Field(default_factory=list, max_length=20)


class SummaryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=600)
    facts: list[str] = Field(default_factory=list, max_length=12)


class Rephrase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bullet: str
    text: str = Field(max_length=500)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projects: list[EntryPlan] = Field(default_factory=list, max_length=6)
    experience: list[EntryPlan] = Field(default_factory=list, max_length=4)
    skills: list[SkillPlan] = Field(default_factory=list, max_length=8)
    summary: SummaryPlan | None = None
    rephrased: list[Rephrase] = Field(default_factory=list, max_length=10)

    @field_validator("projects", "experience")
    @classmethod
    def _unique(cls, value: list[EntryPlan]) -> list[EntryPlan]:
        seen: set[str] = set()
        return [item for item in value if not (item.id in seen or seen.add(item.id))]


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "projects": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}, "maxItems": 8}},
            "required": ["id", "bullets"], "additionalProperties": False}},
        "experience": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}, "maxItems": 8}},
            "required": ["id", "bullets"], "additionalProperties": False}},
        "skills": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
            "group": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}, "maxItems": 20}},
            "required": ["group", "items"], "additionalProperties": False}},
        "summary": {"type": "object", "properties": {
            "text": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}, "maxItems": 12}},
            "required": ["text", "facts"], "additionalProperties": False},
        "rephrased": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
            "bullet": {"type": "string"}, "text": {"type": "string"}},
            "required": ["bullet", "text"], "additionalProperties": False}},
    },
    "required": ["projects", "experience", "skills"],
    "additionalProperties": False,
}


def parse_plan(text: str) -> Plan:
    import json

    try:
        return Plan.model_validate(json.loads(text))
    except (ValueError, ValidationError) as error:
        raise PlanError(str(error)[:300]) from error


class PlanError(ValueError):
    """The model's answer is not a valid plan."""


# --- vocabulary ---------------------------------------------------------------------------------------------------------

ALIASES: dict[str, str] = {
    "postgres": "postgresql", "postgresql": "postgresql", "js": "javascript", "javascript": "javascript",
    "next": "next.js", "nextjs": "next.js", "next.js": "next.js", "node": "node.js", "nodejs": "node.js",
    "node.js": "node.js", "react": "react", "reactjs": "react", "react.js": "react", "ts": "typescript",
    "typescript": "typescript", "k8s": "kubernetes", "gcp": "gcp", "mongo": "mongodb", "mongodb": "mongodb",
    "tailwind": "tailwindcss", "tailwindcss": "tailwindcss", "gh": "github", "ci/cd": "ci/cd", "cicd": "ci/cd",
    "llms": "llm", "llm": "llm", "gen": "genai", "genai": "genai", "restful": "rest", "rest": "rest",
    "apis": "api", "api": "api", "ml": "machine learning", "ai": "ai", "sklearn": "scikit-learn",
    "scikit-learn": "scikit-learn", "pgvector": "pgvector", "rag": "rag",
}
BANNED_WORDS = (
    "senior", "lead", "leader", "leadership", "expert", "principal", "architect", "years of experience",
    "extensive", "proven track record", "seasoned", "world-class", "guru", "ninja", "rockstar", "specialist",
)
OWNERSHIP_VERBS = ("led", "lead", "leading", "managed", "owned", "architected", "spearheaded", "headed", "directed",
                   "pioneered", "founded", "drove")
FIRST_PERSON = re.compile(r"\b(I|me|my|mine|myself|we|our|ours)\b")
_NUMBER = re.compile(r"\d[\d,.]*\s?(?:k|m|%|\+)*|\d")
_YEARS = re.compile(r"\b(?:\d+\+?|several|many|multiple|numerous)\s+years?\b", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#./_-]*")
_COMMON = frozenset(
    """a an and the of for to in on with by from as at is are was were be been into over per via using use used
    built build builds shipped ship worked work working across end that this these those it its their his her which
    who whom whose while within without under about after before between during through up down out off again
    further then once here there when where why how all any both each few more most other some such no nor not only
    own same so than too very can will just should now also able make made making create created creating develop
    developed developing design designed designing implement implemented implementing deliver delivered delivering
    improve improved improving reduce reduced reducing increase increased increasing support supported supporting
    applied production systems system platform platforms application applications service services data model models
    team teams project projects feature features code quality performance tools tool software engineering engineer
    developer development full stack frontend backend front back cloud web api apis user users client clients
    solution solutions deployment deployed pipeline pipelines workflow workflows testing tests test evaluation
    analytics insights reliable scalable robust high impact real world hands experience intern interns internship
    student students university college undergraduate graduate final year looking role roles opportunity opportunities
    comfortable skilled strong passionate focused driven eager motivated learn learning grow growing ready seeking
    turn into where what whether measures measure measuring measured""".split())


def canonical(term: str) -> str:
    key = term.lower().strip(".,;:()[]")
    return ALIASES.get(key, key)


KNOWN_TECH = frozenset("""
    kubernetes docker terraform ansible jenkins kafka spark hadoop airflow snowflake databricks redis postgres
    postgresql mysql mongodb cassandra elasticsearch graphql grpc rust golang go java kotlin swift scala ruby rails
    django flask spring angular vue svelte nextjs nestjs express pytorch tensorflow keras jax onnx opencv langchain
    llamaindex openai anthropic gemini llama mistral huggingface azure gcp aws lambda ec2 s3 cloudfront sagemaker
    bedrock vertex pandas numpy scikit-learn sklearn xgboost lightgbm mlflow kubeflow grafana prometheus datadog
    splunk tableau powerbi excel sap salesforce apex agentforce servicenow workday oracle cobol fortran matlab
    unity unreal solidity ethereum blockchain react angularjs jquery bootstrap sass webpack vite graphql
    selenium cypress playwright jest junit mockito pytest kubectl helm nginx apache linux ubuntu bash powershell
    c c++ c# python javascript typescript sql nosql html css rag llm nlp cv""".split())


def _technical_tokens(text: str) -> set[str]:
    """Words that look like technologies / proper nouns: CamelCase, acronyms, digits, dots, slashes, plus signs,
    a capitalized word in the middle of a sentence, or a name from the known-technology lexicon."""
    found: set[str] = set()
    sentence_start = True
    for match in _TOKEN.finditer(text):
        token = match.group(0).strip("./-_")
        before = text[: match.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?:;"
        low = token.lower()
        if (low in KNOWN_TECH and len(token) > 1) or (
                not sentence_start and token[:1].isupper() and low not in _COMMON and len(token) > 2):
            found.add(canonical(token))
    for match in _TOKEN.finditer(text):
        token = match.group(0).strip("./-_")
        if len(token) < 2:
            continue
        looks_technical = (
            any(char.isdigit() for char in token) or any(char in token for char in ".+#/") or token.isupper()
            or (token[0].isupper() and any(c.isupper() for c in token[1:]))
        )
        if looks_technical:
            found.add(canonical(token))
    return found


def vocabulary(texts: list[str]) -> set[str]:
    """Every word and technical token appearing in the ticked facts (aliases resolved)."""
    words: set[str] = set()
    for text in texts:
        for match in _TOKEN.finditer(text):
            token = match.group(0).strip("./-_")
            if token:
                words.add(canonical(token))
                words.add(token.lower())
    return words


def numbers_in(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", m.group(0)).lower().rstrip(".") for m in _NUMBER.finditer(text)}


# --- validation -------------------------------------------------------------------------------------------------------------


@dataclass
class Violation:
    rule: str
    offending: str
    source: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "offending": self.offending[:240], "source": self.source[:240]}


@dataclass
class Validation:
    ok: bool = True
    violations: list[Violation] = field(default_factory=list)

    def add(self, rule: str, offending: str, source: str = "") -> None:
        self.ok = False
        self.violations.append(Violation(rule, offending, source))

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": [v.as_dict() for v in self.violations]}


@dataclass(frozen=True)
class Catalog:
    """What a plan may refer to: ticked facts by id, plus the base resume's structure."""

    facts: dict[str, TexFact]                 # ticked facts only
    ticked_ids: frozenset[str]
    entry_children: dict[str, tuple[str, ...]]  # entry fact id -> allowed bullet fact ids (base + ticked other)
    project_ids: frozenset[str]
    experience_ids: frozenset[str]
    skill_groups: dict[str, tuple[str, ...]]  # group name -> allowed skill fact ids
    held_titles: tuple[str, ...]              # plain "title" strings of experience entries
    base_summary: str
    vocab: frozenset[str]
    all_text: str


def validate_plan(plan: Plan, catalog: Catalog, *, allow_rephrase: bool) -> Validation:
    report = Validation()
    facts = catalog.facts

    def cited(fact_id: str, what: str) -> TexFact | None:
        fact = facts.get(fact_id)
        if fact is None:
            report.add("unknown_or_unticked_fact", fact_id, what)
        return fact

    # entries and bullets
    for group, allowed_ids in (("projects", catalog.project_ids), ("experience", catalog.experience_ids)):
        for entry in getattr(plan, group):
            if entry.id not in allowed_ids:
                report.add("unknown_entry", entry.id, group)
                continue
            allowed = set(catalog.entry_children.get(entry.id, ()))
            for bullet_id in entry.bullets:
                if bullet_id not in catalog.ticked_ids:
                    report.add("unknown_or_unticked_fact", bullet_id, f"bullet in {entry.id}")
                elif bullet_id not in allowed:
                    report.add("bullet_in_wrong_entry", bullet_id, entry.id)

    # skills: reorder / drop only
    for line in plan.skills:
        allowed = set(catalog.skill_groups.get(line.group, ()))
        if line.group not in catalog.skill_groups:
            report.add("unknown_skill_group", line.group)
            continue
        for skill_id in line.items:
            if skill_id not in catalog.ticked_ids:
                report.add("unknown_or_unticked_fact", skill_id, f"skill in {line.group}")
            elif skill_id not in allowed:
                report.add("skill_in_wrong_group", skill_id, line.group)

    # rephrased bullets
    if plan.rephrased and not allow_rephrase:
        report.add("rephrase_not_allowed", plan.rephrased[0].bullet, "mode is select/reorder only")
    kept_bullets = {b for e in (*plan.projects, *plan.experience) for b in e.bullets}
    for item in plan.rephrased if allow_rephrase else []:
        source = cited(item.bullet, "rephrased bullet")
        if source is None:
            continue
        if item.bullet not in kept_bullets:
            report.add("rephrased_bullet_not_kept", item.bullet)
        _check_rephrase(item.text, source, report)

    # summary
    if plan.summary is not None:
        refs = [cited(fact_id, "summary") for fact_id in plan.summary.facts]
        source_text = " ".join(f.text for f in refs if f) or catalog.base_summary
        _check_free_text("summary", plan.summary.text, source_text, catalog, report)
        if _YEARS.search(plan.summary.text):
            report.add("summary_states_experience_years", _YEARS.search(plan.summary.text).group(0))  # type: ignore[union-attr]
        for title in re.findall(r"\b(?:Senior|Lead|Principal|Staff|Head|Manager|Director|Architect|Engineer II)\b",
                                plan.summary.text):
            report.add("summary_claims_a_title", title, "no such title in the facts")
    return report


def _check_rephrase(new_text: str, source: TexFact, report: Validation) -> None:
    old_text = source.text
    if len(new_text) > 1.3 * len(old_text):
        report.add("rephrase_too_long", f"{len(new_text)} chars vs {len(old_text)}", source.id)
    if FIRST_PERSON.search(new_text):
        report.add("rephrase_first_person", FIRST_PERSON.search(new_text).group(0), source.id)  # type: ignore[union-attr]
    lowered = new_text.lower()
    for word in BANNED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered) and not re.search(rf"\b{re.escape(word)}\b", old_text.lower()):
            report.add("rephrase_banned_word", word, source.id)
    old_words = set(re.findall(r"[a-z]+", old_text.lower()))
    for verb in OWNERSHIP_VERBS:
        if re.search(rf"\b{verb}\b", lowered) and verb not in old_words:
            report.add("rephrase_ownership_claim", verb, source.id)
    extra_numbers = numbers_in(new_text) - numbers_in(old_text)
    for number in sorted(extra_numbers):
        report.add("rephrase_new_number", number, source.id)
    extra_terms = _technical_tokens(new_text) - _technical_tokens(old_text) - vocabulary([old_text])
    for term in sorted(extra_terms):
        report.add("rephrase_new_technology", term, source.id)


def _check_free_text(label: str, text: str, source_text: str, catalog: Catalog, report: Validation) -> None:
    for number in sorted(numbers_in(text) - numbers_in(source_text)):
        report.add(f"{label}_new_number", number, "not in the cited facts")
    allowed = set(catalog.vocab) | vocabulary([source_text])
    for term in sorted(_technical_tokens(text)):
        if term not in allowed:
            report.add(f"{label}_unknown_technology", term, "not in the ticked facts")
    lowered = text.lower()
    for word in BANNED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            report.add(f"{label}_banned_word", word)
    if FIRST_PERSON.search(text):
        report.add(f"{label}_first_person", FIRST_PERSON.search(text).group(0))  # type: ignore[union-attr]


def validate_identity(rendered_plain: dict[str, list[str]], base_plain: dict[str, list[str]]) -> Validation:
    """Employer names, titles, dates, degree, CGPA, marks, certifications and contact details must be identical to
    the base. `rendered_plain` / `base_plain` map a field group to its list of strings."""
    report = Validation()
    for group, expected in base_plain.items():
        actual = rendered_plain.get(group, [])
        if actual != expected:
            missing = [item for item in expected if item not in actual]
            extra = [item for item in actual if item not in expected]
            report.add("identity_field_changed", f"{group}: missing {missing[:3]} extra {extra[:3]}",
                       "; ".join(expected)[:200])
    return report


__all__ = ["ALIASES", "BANNED_WORDS", "Catalog", "Plan", "PlanError", "Validation", "Violation", "PLAN_SCHEMA",
           "canonical", "normalize", "numbers_in", "parse_plan", "validate_identity", "validate_plan", "vocabulary",
           "Literal"]
