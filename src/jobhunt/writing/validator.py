"""The grounding validator for cover letters and application answers. Pure Python, no model.

It reuses the resume tailoring vocabulary (numbers, technology terms, banned seniority words) and adds the
rules a free-text letter needs: every claim must come from a cited, ticked fact; no employer, title or date that
is not in those facts; no "years of experience"; no company flattery beyond what the posting itself says; no
placeholders; plain text; and the length limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jobhunt.tailoring.plan import (
    BANNED_WORDS, OWNERSHIP_VERBS, _YEARS, _technical_tokens, canonical, numbers_in, vocabulary,
)

_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{1,60}\]|\{[^}\n]{1,60}\}|<[^>\n]{1,60}>|\bTODO\b|\bTBD\b|\bX{3,}\b|\blorem ipsum\b|"
    r"\b(?:your|insert|company|candidate|hiring manager) name\b|\[?\bcompany\b\]?\s*here", re.IGNORECASE)
_MARKDOWN = re.compile(r"(\*\*|__|`|^\s{0,3}#{1,6}\s|^\s*[-*•]\s+|^\s*\d+[.)]\s+|\|)", re.MULTILINE)
_YEARS_PHRASE = re.compile(r"\byears?\s+of\s+(?:\w+\s+){0,2}experience\b|\bdecade\b|\bseasoned\b", re.IGNORECASE)
_MONTH = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|"
                    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b")
_ORG_AFTER = re.compile(
    r"\b(?:at|with|for|joined|from|interned at|worked at|worked for|employed at|employed by)\s+"
    r"((?:(?!I\b)[A-Z][\w&.\-']*)(?:\s+(?:(?!I\b)[A-Z][\w&.\-']*|&|of|and))*)")
_ROLE_AS = re.compile(r"\bas\s+(?:an?|the)\s+([A-Za-z][A-Za-z/\- ]{2,40}?)(?=\s+(?:at|with|for|in|on)\b|[,.;]|$)")
_FLATTERY = (
    "world-class", "world class", "industry-leading", "leading company", "market leader", "innovative", "cutting-edge",
    "cutting edge", "renowned", "prestigious", "admire", "admired", "impressed", "dream company", "dream job",
    "best-in-class", "top-tier", "visionary", "groundbreaking", "pioneering", "revolutionary", "inspiring",
    "passionate about your", "love your", "fan of", "thrilled", "honored", "honoured", "excited by your",
)
_SAFE_WORDS = frozenset(
    "hiring team dear sincerely regards thank thanks hello hi best kind warm please i".split())
# technology names that are also ordinary English words: only flagged when written as a name (capitalised mid-sentence)
_AMBIGUOUS_TECH = frozenset({"express", "go", "swift", "rust", "spring", "unity", "ruby", "rails", "apache", "helm",
                             "bash", "c", "r", "ant", "maven", "kotlin", "spark", "flask", "node", "next", "react"})
_TITLE_WORDS = ("Senior", "Lead", "Principal", "Staff", "Head", "Manager", "Director", "Architect", "Chief", "VP")


@dataclass
class WritingVerdict:
    ok: bool = True
    violations: list[dict[str, str]] = field(default_factory=list)
    words: int = 0

    def add(self, rule: str, offending: str = "", source: str = "") -> None:
        self.ok = False
        self.violations.append({"rule": rule, "offending": offending[:200], "source": source[:200]})

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": self.violations, "words": self.words}


_WORD_VALUES = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100, "dozen": 12,
}
_SCALES = {"thousand": 1_000, "million": 1_000_000, "lakh": 100_000, "crore": 10_000_000}
_SPELLED = re.compile(
    r"\b((?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[- ])?(?:two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|dozen)(?:[- ](?:hundred|thousand|million|lakh|crore))?)\b", re.IGNORECASE)
_DIGIT_VALUE = re.compile(r"(\d[\d,]*(?:\.\d+)?)((?:k|m)(?![A-Za-z])|\s?%|\+)?", re.IGNORECASE)


def number_values(text: str) -> set[float]:
    """Numeric values of the digits in a text ("~60k" -> 60000, "5k+" -> 5000, "99.9%" -> 99.9, "1,200" -> 1200)."""
    values: set[float] = set()
    for match in _DIGIT_VALUE.finditer(text):
        raw = match.group(1).replace(",", "").rstrip(".")
        try:
            value = float(raw)
        except ValueError:
            continue
        suffix = (match.group(2) or "").lower()
        values.add(value * 1000 if suffix == "k" else value * 1_000_000 if suffix == "m" else value)
    return values


def spelled_numbers(text: str) -> list[tuple[float, str]]:
    """(value, phrase) for each number written in words ("sixty thousand" -> 60000, "forty" -> 40). "one" is skipped."""
    found: list[tuple[float, str]] = []
    for match in _SPELLED.finditer(text):
        phrase = match.group(1)
        total = 1.0
        parts = re.split(r"[- ]", phrase.lower())
        base = 0.0
        for part in parts:
            if part in _WORD_VALUES:
                base += _WORD_VALUES[part] if base == 0 or _WORD_VALUES[part] < 100 else 0
                if part == "hundred" and len(parts) > 1 and parts[0] in _WORD_VALUES and parts[0] != "hundred":
                    base = _WORD_VALUES[parts[0]] * 100 if parts.index(part) == 1 else base
            elif part in _SCALES:
                total *= _SCALES[part]
        found.append((base * total if base else total, phrase))
    return found


def values_in_order(text: str) -> tuple[float, ...]:
    """Every number in a text as a value, in the order written, digits and words alike: "40" and "forty" are the same
    claim, "from 65% to 85%" and "from 85% to 65%" are not."""
    found: list[tuple[int, float]] = []
    for match in _DIGIT_VALUE.finditer(text):
        raw = match.group(1).replace(",", "").rstrip(".")
        try:
            value = float(raw)
        except ValueError:
            continue
        suffix = (match.group(2) or "").lower()
        found.append((match.start(), value * 1000 if suffix == "k" else value * 1_000_000 if suffix == "m" else value))
    for match in _SPELLED.finditer(text):
        for value, phrase in spelled_numbers(match.group(0)):
            found.append((match.start(), value))
            break
    return tuple(value for _, value in sorted(found))


def spelled_values(text: str) -> set[float]:
    """Facts that themselves spell a number out count as known values too."""
    return {value for value, _ in spelled_numbers(text)}


def word_count(text: str) -> int:
    return len(text.split())


def parse_word_limit(question: str) -> int | None:
    """A limit the question states ("in 100 words", "max 150 words", "up to 200 words"); characters are converted."""
    text = question.lower()
    patterns = (
        r"(?:maximum|max\.?|at most|up to|under|within|no more than|limit(?:ed)? to|not exceed(?:ing)?|<=?)\s*(?:of\s*)?"
        r"(\d{2,4})\s*words?",
        r"(\d{2,4})\s*[- ]?words?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    match = re.search(r"(\d{3,5})\s*(?:characters?|chars)\b", text)
    if match:
        return max(10, int(match.group(1)) // 6)          # ~6 characters per word incl. the space
    return None


def validate_text(
    text: str, *, kind: str, cited_ids: list[str], facts: dict[str, Any], job: dict[str, str],
    min_words: int | None = None, max_words: int | None = None,
) -> WritingVerdict:
    """`facts`: ticked fact id -> object with `.text` and `.entry` (parent fact id). `job`: title, company,
    location, description (already injection-stripped)."""
    verdict = WritingVerdict(words=word_count(text))
    body = text.strip()
    if not body:
        verdict.add("empty_text")
        return verdict

    # citations
    known = [fid for fid in cited_ids if fid in facts]
    for fid in cited_ids:
        if fid not in facts:
            verdict.add("unknown_or_unticked_fact", fid, "cited id is not one of your ticked facts")
    if not known:
        verdict.add("no_citations", "", "the text must cite the facts it uses")
        return verdict
    cited_text = " ".join(facts[fid].text for fid in known)
    parent_text = " ".join(facts[facts[fid].entry].text for fid in known
                           if getattr(facts[fid], "entry", "") in facts)
    source_text = f"{cited_text} {parent_text}".strip()

    # length
    if min_words is not None and verdict.words < min_words:
        verdict.add("too_short", f"{verdict.words} words", f"minimum {min_words}")
    if max_words is not None and verdict.words > max_words:
        verdict.add("too_long", f"{verdict.words} words", f"maximum {max_words}")

    # form
    if _PLACEHOLDER.search(body):
        verdict.add("placeholder_left_in", _PLACEHOLDER.search(body).group(0))      # type: ignore[union-attr]
    if _MARKDOWN.search(body):
        verdict.add("not_plain_text", _MARKDOWN.search(body).group(0).strip())      # type: ignore[union-attr]

    # numbers and dates
    allowed_numbers = numbers_in(source_text) | numbers_in(f"{job.get('company', '')} {job.get('title', '')}")
    for number in sorted(numbers_in(body) - allowed_numbers):
        verdict.add("number_not_in_cited_facts", number, "numbers must come from the cited facts")
    known_values = number_values(f"{source_text} {job.get('company', '')} {job.get('title', '')}") | \
        {value for word_value in spelled_values(source_text) for value in (word_value,)}
    for value, phrase in spelled_numbers(body):
        if value not in known_values:
            verdict.add("spelled_number_not_in_cited_facts", phrase, "a number written in words must match a cited fact")
    for month in sorted({m.group(0) for m in _MONTH.finditer(body)}):
        if not re.search(rf"\b{re.escape(month)}\b", source_text):
            verdict.add("date_not_in_cited_facts", month)

    # years of experience, seniority, titles
    phrase = _YEARS_PHRASE.search(body) or _YEARS.search(body)
    if phrase:
        verdict.add("states_years_of_experience", phrase.group(0))
    job_title_words = set(re.findall(r"[A-Za-z]+", job.get("title", "")))
    lowered = body.lower()
    for word in BANNED_WORDS:
        if word in {"specialist"} and re.search(rf"\b{word}\b", source_text.lower()):
            continue
        if re.search(rf"\b{re.escape(word)}\b", lowered) and not re.search(rf"\b{re.escape(word)}\b", source_text.lower()) \
                and word.capitalize() not in job_title_words:
            verdict.add("banned_or_seniority_word", word)
    for title in _TITLE_WORDS:
        if re.search(rf"\b{title}\b", body) and not re.search(rf"\b{title}\b", source_text) and title not in job_title_words:
            verdict.add("claims_a_title", title, "no such title in the cited facts")
    source_words = set(re.findall(r"[a-z]+", source_text.lower()))
    for verb in OWNERSHIP_VERBS:
        if re.search(rf"\b{verb}\b", lowered) and verb not in source_words:
            verdict.add("ownership_claim_not_in_facts", verb)

    # technologies and proper nouns
    company_tokens = set(re.findall(r"[A-Za-z0-9+#.]+", job.get("company", "")))
    allowed_vocab = vocabulary([source_text]) | {w.lower() for w in company_tokens} | \
        {canonical(w) for w in company_tokens} | {w.lower() for w in job_title_words} | \
        {w.lower() for w in re.findall(r"[A-Za-z]+", job.get("location", ""))} | _SAFE_WORDS | \
        {w.lower() for w in re.findall(r"[A-Za-z]+", job.get("question", ""))}
    allowed_vocab |= {part for word in list(allowed_vocab) for part in re.split(r"[/]", word) if part}   # "PostgreSQL/pgvector"
    for term in sorted(_technical_tokens(body)):
        if term in _AMBIGUOUS_TECH and not re.search(rf"(?<=[a-z,;] ){re.escape(term.capitalize())}\b", body):
            continue                                  # "I want to express my interest": an English word, not Express.js
        if term not in allowed_vocab and term.lower() not in allowed_vocab:
            verdict.add("unknown_technology_or_name", term, "not in the cited facts")

    # employers
    company = job.get("company", "").lower()
    for match in _ORG_AFTER.finditer(body):
        organisation = match.group(1).strip(" .,")
        key = organisation.lower()
        if not key or key in {"the", "you", "your", "a", "an"}:
            continue
        in_facts = key in source_text.lower()
        is_company = bool(company) and (key in company or company in key)
        words = set(re.findall(r"[A-Za-z0-9+#.]+", organisation))
        generic = all(w.lower() in allowed_vocab for w in words)
        if not (in_facts or is_company or generic):
            verdict.add("employer_not_in_facts", organisation)
    for match in _ROLE_AS.finditer(body):
        role = match.group(1).strip()
        if role.lower() in source_text.lower():
            continue
        if not all(w.lower() in allowed_vocab or w.lower() in {"a", "an", "and", "the", "student", "undergraduate",
                                                                "candidate", "engineer", "developer", "intern"}
                   for w in re.findall(r"[A-Za-z]+", role)):
            verdict.add("role_not_in_facts", role)

    # flattery beyond what the posting itself says
    description = job.get("description", "").lower()
    for phrase_text in _FLATTERY:
        if phrase_text in lowered and phrase_text not in description:
            verdict.add("company_flattery", phrase_text, "only restate what the posting says")
    return verdict


_SENIORITY_CLAIMS = ("senior", "principal", "architect", "guru", "ninja", "rockstar", "seasoned", "proven track record", "veteran")


def validate_letter(
    text: str, *, cited_ids: list[str], facts: dict[str, Any], job: dict[str, str], profile_text: str = "",
    min_words: int | None = None, max_words: int | None = None,
) -> WritingVerdict:
    """The cover-letter validator (v8): fact safety without punishing paraphrase.

    Allowed evidence = the cited ticked facts (and their entry headers) + the job posting + my profile basics (degree, school,
    expected graduation). Rejected: a number, technology, tool, company or product name outside that evidence; years of
    experience; flattery and cliches; a role or title claim the facts do not support. Dropping numbers, rewording and merging
    facts are fine. Length is counted on the body (without greeting and sign-off). `[Your Name]` is the one allowed placeholder."""
    from jobhunt.writing.letter import split_letter

    verdict = WritingVerdict(words=word_count(text))
    greeting, body, _ = split_letter(text)
    verdict.words = len(body.split())
    if not body.strip():
        verdict.add("empty_text")
        return verdict
    known = [fid for fid in cited_ids if fid in facts]
    for fid in cited_ids:
        if fid not in facts:
            verdict.add("unknown_or_unticked_fact", fid, "cited id is not one of your ticked facts")
    if not known:
        verdict.add("no_citations", "", "the text must cite the facts it uses")
        return verdict
    facts_text = " ".join(facts[fid].text for fid in known) + " " + " ".join(
        facts[facts[fid].entry].text for fid in known if getattr(facts[fid], "entry", "") in facts)
    posting = " ".join([job.get("description", ""), job.get("title", ""), job.get("company", ""), job.get("location", "")])
    evidence = f"{facts_text} {posting} {profile_text}"
    if min_words is not None and verdict.words < min_words:
        verdict.add("too_short", f"{verdict.words} words", f"minimum {min_words}")
    if max_words is not None and verdict.words > max_words:
        verdict.add("too_long", f"{verdict.words} words", f"maximum {max_words}")
    whole = f"{greeting}\n{body}".replace("[Your Name]", "")
    if _PLACEHOLDER.search(whole):
        verdict.add("placeholder_left_in", _PLACEHOLDER.search(whole).group(0))      # type: ignore[union-attr]
    if _MARKDOWN.search(whole):
        verdict.add("not_plain_text", _MARKDOWN.search(whole).group(0).strip())     # type: ignore[union-attr]

    # numbers: by value ("60,000" equals "~60k"); spelled numbers must be known too, or equal the number of cited entries
    known_values = number_values(evidence) | spelled_values(evidence)
    cited_entries = {facts[fid].entry or fid for fid in known if facts[fid].kind in {"project_bullet", "experience_bullet", "project",
                                                                                    "experience"}}
    entry_count = float(len(cited_entries))
    for match in _DIGIT_VALUE.finditer(body):
        raw = match.group(1).replace(",", "").rstrip(".")
        try:
            value = float(raw)
        except ValueError:
            continue
        suffix = (match.group(2) or "").lower()
        value = value * 1000 if suffix == "k" else value * 1_000_000 if suffix == "m" else value
        if value not in known_values and value > 1:
            verdict.add("number_not_in_evidence", match.group(0).strip(), "numbers must come from the cited facts or the posting")
    for value, phrase in spelled_numbers(body):
        if value not in known_values and value != entry_count:
            verdict.add("spelled_number_not_in_evidence", phrase, "a number written in words must match the facts or the posting")
    for month in sorted({m.group(0) for m in _MONTH.finditer(body)}):
        if not re.search(rf"\b{re.escape(month)}\b", evidence):
            verdict.add("date_not_in_evidence", month)

    # years of experience, seniority and title claims are judged against the facts (the posting asking for them proves nothing)
    phrase = _YEARS_PHRASE.search(body) or _YEARS.search(body)
    if phrase:
        verdict.add("states_years_of_experience", phrase.group(0))
    job_title_words = set(re.findall(r"[A-Za-z]+", job.get("title", "")))
    lowered = body.lower()
    for word in _SENIORITY_CLAIMS:
        if re.search(rf"\b{re.escape(word)}\b", lowered) and not re.search(rf"\b{re.escape(word)}\b", facts_text.lower()) \
                and word.capitalize() not in job_title_words and word not in job.get("description", "").lower():
            verdict.add("banned_or_seniority_word", word)
    for title in _TITLE_WORDS:
        if re.search(rf"\b{title}\b", body) and not re.search(rf"\b{title}\b", facts_text) and title not in job_title_words \
                and not re.search(rf"\b{title}\b", job.get("description", "")):
            verdict.add("claims_a_title", title, "no such title in the cited facts")

    # technologies, tools and proper names must be somewhere in the evidence (the posting counts)
    company_tokens = set(re.findall(r"[A-Za-z0-9+#.]+", job.get("company", "")))
    allowed_vocab = vocabulary([evidence]) | {w.lower() for w in company_tokens} | {canonical(w) for w in company_tokens} | \
        {w.lower() for w in job_title_words} | _SAFE_WORDS
    allowed_vocab |= {part for word in list(allowed_vocab) for part in re.split(r"[/]", word) if part}
    for term in sorted(_technical_tokens(body)):
        if term in _AMBIGUOUS_TECH and not re.search(rf"(?<=[a-z,;] ){re.escape(term.capitalize())}\b", body):
            continue
        if term not in allowed_vocab and term.lower() not in allowed_vocab:
            verdict.add("unknown_technology_or_name", term, "not in the cited facts or the posting")
    company = job.get("company", "").lower()
    for match in _ORG_AFTER.finditer(body):
        organisation = match.group(1).strip(" .,")
        key = organisation.lower()
        if not key or key in {"the", "you", "your", "a", "an"}:
            continue
        words = set(re.findall(r"[A-Za-z0-9+#.]+", organisation))
        generic = all(w.lower() in allowed_vocab for w in words)
        if not (key in evidence.lower() or (company and (key in company or company in key)) or generic):
            verdict.add("employer_not_in_facts", organisation)
    for match in _ROLE_AS.finditer(body):
        role = match.group(1).strip()
        if role.lower() in evidence.lower():
            continue
        if not all(w.lower() in allowed_vocab or w.lower() in {"a", "an", "and", "the", "student", "undergraduate", "candidate",
                                                                "engineer", "developer", "intern"}
                   for w in re.findall(r"[A-Za-z]+", role)):
            verdict.add("role_not_in_facts", role)

    description = job.get("description", "").lower()
    for phrase_text in _FLATTERY:
        if phrase_text in lowered and phrase_text not in description:
            verdict.add("company_flattery", phrase_text, "only restate what the posting says")
    return verdict


__all__ = ["WritingVerdict", "number_values", "parse_word_limit", "spelled_numbers", "validate_letter", "validate_text", "values_in_order",
           "word_count"]
