"""Alias-aware keyword matching: which skills a job asks for, and which of them my ticked facts cover.

Everything here is deterministic. Keywords the resume does not support are only ever *reported* ("missing"),
never inserted into a resume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# canonical concept -> surface forms (lower-case). Longest forms are matched first.
ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "rag": ("rag", "retrieval augmented generation", "retrieval-augmented generation", "rag pipeline", "rag pipelines"),
    "llm": ("llm", "llms", "large language model", "large language models"),
    "genai": ("genai", "gen ai", "generative ai", "generative artificial intelligence"),
    "machine learning": ("machine learning", "ml"),
    "deep learning": ("deep learning", "dl"),
    "nlp": ("nlp", "natural language processing"),
    "computer vision": ("computer vision", "cv"),
    "agents": ("agent", "agents", "agentic", "ai agent", "ai agents", "tool-using agent", "tool-using agents",
               "tool using agents", "multi-agent", "tool use", "tool-use", "tool calling", "function calling"),
    "postgresql": ("postgres", "postgresql"),
    "javascript": ("javascript", "js", "es6", "ecmascript"),
    "typescript": ("typescript", "ts"),
    "node.js": ("node.js", "nodejs", "node js", "node"),
    "next.js": ("next.js", "nextjs", "next js"),
    "react": ("react", "react.js", "reactjs", "react js"),
    "ci/cd": ("ci/cd", "cicd", "ci cd", "continuous integration", "continuous delivery", "github actions"),
    "aws": ("aws", "amazon web services", "ec2", "s3", "cloudfront", "lambda"),
    "gcp": ("gcp", "google cloud"),
    "kubernetes": ("kubernetes", "k8s"),
    "rest": ("rest", "restful", "rest api", "rest apis", "restful apis", "restful api"),
    "api": ("api", "apis"),
    "sql": ("sql", "text-to-sql", "t-sql"),
    "pgvector": ("pgvector",),
    "vector database": ("vector database", "vector databases", "vector store", "vector db", "chromadb", "pgvector",
                        "pinecone", "faiss"),
    "embeddings": ("embedding", "embeddings", "sentence-transformers"),
    "prompt engineering": ("prompt engineering", "prompting", "prompt design"),
    "evaluation": ("evaluation", "evals", "llm evaluation", "evaluation suite"),
    "guardrails": ("guardrail", "guardrails", "prompt injection", "pii", "pii redaction", "safety"),
    "testing": ("testing", "pytest", "unit tests", "unit testing", "test automation", "jest", "cypress"),
    "docker": ("docker", "containers", "containerization"),
    "salesforce": ("salesforce", "agentforce", "apex", "lightning", "sfdc"),
    "microservices": ("microservices", "microservice"),
    "agile": ("agile", "scrum", "agile/scrum", "sprint", "sprints"),
    "scikit-learn": ("scikit-learn", "sklearn", "scikit learn"),
    "xgboost": ("xgboost",),
    "pandas": ("pandas",),
    "python": ("python",),
    "java": ("java",),
    "c++": ("c++",),
    "fastapi": ("fastapi",),
    "flask": ("flask",),
    "django": ("django",),
    "mongodb": ("mongodb", "mongo"),
    "nosql": ("nosql",),
    "redis": ("redis",),
    "git": ("git", "github"),
    "pytorch": ("pytorch",),
    "tensorflow": ("tensorflow",),
    "langchain": ("langchain", "langgraph", "llamaindex"),
    "openai": ("openai", "gpt"),
    "gemini": ("gemini",),
    "hugging face": ("hugging face", "huggingface", "transformers"),
    "mlops": ("mlops", "model deployment", "model serving"),
    "data science": ("data science", "data scientist", "analytics"),
    "statistics": ("statistics", "statistical"),
    "tailwind": ("tailwind", "tailwindcss"),
    "html/css": ("html", "css", "html5", "css3"),
    "linux": ("linux",),
    "monitoring": ("monitoring", "observability", "logging"),
    "security": ("security", "authentication", "authorization"),
    "graphql": ("graphql",),
    "kafka": ("kafka",),
    "spark": ("spark", "pyspark"),
    "airflow": ("airflow",),
    "terraform": ("terraform",),
    "tableau": ("tableau", "power bi", "powerbi"),
    "devops": ("devops",),
    "cloud": ("cloud",),
    # v7: the Java / enterprise vocabulary a Spring Boot job asks for, and the finance domain
    "spring boot": ("spring boot", "springboot", "spring mvc", "spring security", "spring data", "spring cloud",
                    "spring framework"),
    "jpa/hibernate": ("jpa", "hibernate", "jpa/hibernate", "orm"),
    "junit": ("junit", "mockito", "testng"),
    "oracle": ("oracle",),
    "mysql": ("mysql",),
    "maven": ("maven", "gradle"),
    "soap": ("soap",),
    "unix": ("unix", "shell scripting", "bash"),
    "multithreading": ("multi-threading", "multithreading", "concurrency"),
    "event-driven": ("event-driven", "event driven"),
    "infrastructure as code": ("infrastructure-as-code", "infrastructure as code", "iac"),
    "angular": ("angular", "angularjs"),
    "bedrock": ("bedrock", "aws bedrock"),
    "jira": ("jira",),
    "data pipelines": ("data pipeline", "data pipelines", "etl"),
    "oop": ("oop", "object-oriented", "object oriented"),
    "dsa": ("dsa", "data structures", "algorithms"),
    "finance": ("banking", "bank", "financial services", "fintech", "finance", "financial", "trading", "payments"),
}
_FORM_TO_CANON: dict[str, str] = {}
for canon, forms in ALIAS_GROUPS.items():
    for form in forms:
        _FORM_TO_CANON.setdefault(form, canon)
    _FORM_TO_CANON.setdefault(canon, canon)
_FORMS_BY_LENGTH = sorted(_FORM_TO_CANON, key=len, reverse=True)
_PATTERN = re.compile(
    r"(?<![a-z0-9+#.])(" + "|".join(re.escape(f) for f in _FORMS_BY_LENGTH) + r")(?![a-z0-9+#])", re.IGNORECASE)

# concepts that mean "this is a skill the job asks for" (a deliberately closed vocabulary)
SKILL_CONCEPTS = frozenset(ALIAS_GROUPS) - {"api", "cloud"} | {"api", "cloud"}


def concepts(text: str) -> list[str]:
    """Canonical concepts in `text`, in order of appearance (with repeats, so frequency can be counted)."""
    return [_FORM_TO_CANON[m.group(1).lower()] for m in _PATTERN.finditer(text.replace("’", "'"))]


@dataclass
class KeywordReport:
    keywords: list[str] = field(default_factory=list)          # canonical, most important first
    covered: dict[str, str] = field(default_factory=dict)      # keyword -> fact id that supports it
    missing: list[str] = field(default_factory=list)           # not on the resume: shown, never added


_REQUIRED_HEADER = re.compile(
    r"(?i)should have|must have|you have|you will have|you(?:'ll| will) need|to be successful|requirements?\b|"
    r"qualifications?\b|required|essential|minimum|we(?:'re| are) looking for|key skills|skills required|what we(?:'re| are) looking for")
_OPTIONAL_HEADER = re.compile(
    r"(?i)nice to have|highly valued|preferred|bonus|desirable|a plus\b|good to have|would be an advantage|optional|"
    r"valued skills")
_RESPONSIBILITY_HEADER = re.compile(
    r"(?i)responsibilit|accountabilit|what you(?:'ll| will) (?:do|work)|analyst expectations|about the role|"
    r"purpose of the role|expectations\b|about us|who we are|benefits|perks")
_BULLET = re.compile(r"^\s*(?:[-*•●]|\d+[.)])\s+")
LINE_WEIGHTS = {"required": 2.4, "optional": 1.2, "responsibility": 0.8, "base": 1.0}
TITLE_WEIGHT = 3.0
REPEAT_BONUS = 0.25


def job_keyword_weights(title: str, description: str) -> dict[str, float]:
    """Every concept the posting mentions with a weight, best first (ties: first mention wins).

    A concept's weight is the weight of the line it is mentioned on at its best (a requirements list weighs 2.4,
    a "nice to have" list 1.2, a responsibilities list 0.8, anything else 1.0), plus a small bonus per repeat, plus 3.0
    for the title. Counting every repeat made "unit testing" outweigh "Java" in a posting that lists Java once."""
    best: dict[str, float] = {}
    repeats: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for concept in concepts(title):
        best[concept] = max(best.get(concept, 0.0), TITLE_WEIGHT)
        first_seen.setdefault(concept, order)
        order += 1
    state = "base"
    for raw in re.split(r"\n+", description):
        line = raw.strip()
        if not line:
            continue
        bullet = bool(_BULLET.match(line))
        if _OPTIONAL_HEADER.search(line) and (not bullet or len(line) < 80):
            state = "optional" if not bullet else state
        elif _REQUIRED_HEADER.search(line) and not bullet:
            state = "required"
        elif _RESPONSIBILITY_HEADER.search(line) and not bullet:
            state = "responsibility"
        elif not bullet and len(line) > 140:
            state = "base"                  # a long prose paragraph ends a list
        weight = LINE_WEIGHTS[state]
        if _OPTIONAL_HEADER.search(line):
            weight = min(weight, LINE_WEIGHTS["optional"])
        elif not bullet and _REQUIRED_HEADER.search(line):
            weight = max(weight, 2.0)
        for concept in concepts(line):
            if concept in best:
                repeats[concept] = repeats.get(concept, 0) + 1
            best[concept] = max(best.get(concept, 0.0), weight)
            first_seen.setdefault(concept, order)
            order += 1
    score = {c: best[c] + REPEAT_BONUS * min(repeats.get(c, 0), 3) for c in best}
    return {c: round(score[c], 3) for c in sorted(score, key=lambda c: (-score[c], first_seen[c]))}


def job_keywords(title: str, description: str, limit: int = 16) -> list[str]:
    """Top concepts the posting asks for, required skills before nice-to-haves (see `job_keyword_weights`)."""
    return list(job_keyword_weights(title, description))[:limit]


def weighted_coverage(text: str, weights: dict[str, float], top: int = 16) -> float:
    """Share (0..1) of the posting's weighted top concepts that `text` mentions."""
    wanted = dict(list(weights.items())[:top])
    total = sum(wanted.values())
    if not total:
        return 0.0
    present = set(concepts(text))
    return sum(w for c, w in wanted.items() if c in present) / total


def coverage(keywords: list[str], facts: dict[str, str]) -> KeywordReport:
    """For each keyword: the first ticked fact (id -> text) containing the concept, else missing."""
    report = KeywordReport(keywords=list(keywords))
    fact_concepts = {fact_id: set(concepts(text)) for fact_id, text in facts.items()}
    for keyword in keywords:
        owner = next((fid for fid, found in fact_concepts.items() if keyword in found), None)
        if owner:
            report.covered[keyword] = owner
        else:
            report.missing.append(keyword)
    return report


def text_coverage(keywords: list[str], text: str) -> set[str]:
    present = set(concepts(text))
    return {k for k in keywords if k in present}


def relevance_score(text: str, keywords: list[str], title_concepts: set[str] | None = None) -> float:
    """Deterministic relevance of one bullet / skill to the job (alias-aware keyword match, rank-weighted)."""
    present = set(concepts(text))
    if not present:
        return 0.0
    score = 0.0
    for rank, keyword in enumerate(keywords):
        if keyword in present:
            score += 1.0 + (len(keywords) - rank) / max(1, len(keywords))     # earlier keywords weigh more
    if title_concepts:
        score += 0.5 * len(present & title_concepts)
    return score
