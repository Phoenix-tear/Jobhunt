"""Rule-based classification and the title prefilter (no LLM, all keyword lists from config).

Matching is whole-word and case-insensitive on text where hyphens, slashes and underscores
have been turned into spaces, so "Entry-Level", "entry level" and "ENTRY_LEVEL" all match
the single keyword "entry level".
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from jobhunt.config import ClassificationSettings, Config, PrefilterSettings
from jobhunt.models import EmploymentType, ExperienceLevel

_SEPARATORS = re.compile(r"[\-_/\\]+")
_WHITESPACE = re.compile(r"\s+")
_DESCRIPTION_HEAD_CHARS = 600  # employment-type hints are only trusted near the top

_GRADUATION_YEAR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"class\s+of\s+(20\d{2})",
        r"\b(?:graduat\w*|grads?)\b\D{0,40}?\b(20\d{2})\b",
        r"\b(20\d{2})\s+(?:new\s+)?(?:grads?|graduat\w*|start(?:s|ing)?|cohort|intake|batch)\b",
        r"\b(?:batch|cohort|intake)\s+(?:of\s+)?(20\d{2})\b",
        r"\b(20\d{2})\s+pass[\s-]*outs?\b",
        r"\bpass[\s-]*out\s+(?:year\s+)?(?:of\s+)?(20\d{2})\b",
    )
)
_MIN_GRADUATION_YEAR, _MAX_GRADUATION_YEAR = 2024, 2032

_YEARS_OF_EXPERIENCE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?(?:\s*(?:-|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE
)


def normalize_for_matching(text: str | None) -> str:
    """Lower-case, separators -> spaces, collapse whitespace."""
    if not text:
        return ""
    return _WHITESPACE.sub(" ", _SEPARATORS.sub(" ", text.lower())).strip()


def compile_keywords(keywords: Iterable[str]) -> re.Pattern[str] | None:
    """One whole-word alternation over normalized keywords; None when the list is empty."""
    normalized = sorted(
        {normalize_for_matching(keyword) for keyword in keywords if normalize_for_matching(keyword)},
        key=len,
        reverse=True,
    )
    if not normalized:
        return None
    alternation = "|".join(re.escape(keyword).replace(r"\ ", r"\s+") for keyword in normalized)
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])")


def _matches(pattern: re.Pattern[str] | None, normalized_text: str) -> bool:
    return bool(pattern and normalized_text and pattern.search(normalized_text))


def _remove_phrases(text: str, phrases: Iterable[str]) -> str:
    for phrase in phrases:
        text = text.replace(normalize_for_matching(phrase), " ")
    return text


class TitlePrefilter:
    """Keep a job iff the title hits an include keyword and no exclude keyword."""

    def __init__(self, include_keywords: Iterable[str], settings: PrefilterSettings) -> None:
        self._include = compile_keywords(include_keywords)
        self._has_include_list = self._include is not None
        self._exclude = compile_keywords(settings.exclude_title_keywords)
        self._protected = tuple(settings.protected_title_phrases)

    @classmethod
    def from_config(cls, config: Config) -> "TitlePrefilter":
        return cls(config.prefilter_include_keywords, config.prefilter)

    def keeps(self, title: str | None) -> bool:
        normalized = normalize_for_matching(title)
        if not normalized:
            return False
        if self._has_include_list and not _matches(self._include, normalized):
            return False
        # Include was judged on the full title; exclude ignores protected phrases.
        return not _matches(self._exclude, _remove_phrases(normalized, self._protected))


class Classifier:
    """employment_type, experience_level and graduation_year_hint from title/description."""

    def __init__(self, settings: ClassificationSettings, protected_title_phrases: Iterable[str] = ()) -> None:
        self._intern = compile_keywords(settings.intern_keywords)
        self._new_grad = compile_keywords(settings.new_grad_keywords)
        self._entry = compile_keywords(settings.entry_keywords)
        self._mid = compile_keywords(settings.mid_keywords)
        self._senior = compile_keywords(settings.senior_keywords)
        self._part_time = compile_keywords(settings.part_time_keywords)
        self._contract = compile_keywords(settings.contract_keywords)
        self._full_time = compile_keywords(settings.full_time_keywords)
        self._protected = tuple(protected_title_phrases)
        # Strong description phrases that justify a level when the title is silent.
        self._description_new_grad = compile_keywords(
            ["new grad", "new graduate", "recent graduate", "recent graduates", "university graduate",
             "university grad", "fresh graduate", "fresh graduates", "freshers"]
        )
        self._description_entry = compile_keywords(["entry level", "no prior experience", "0 1 years", "0 2 years"])

    @classmethod
    def from_config(cls, config: Config) -> "Classifier":
        return cls(config.classification, config.prefilter.protected_title_phrases)

    # -- experience level ------------------------------------------------------------------

    def experience_level(self, title: str | None, description: str | None = None) -> ExperienceLevel:
        normalized_title = _remove_phrases(normalize_for_matching(title), self._protected)
        # Order matters: "Senior Associate" is senior, "Intern - Lead Gen" is an intern.
        if _matches(self._intern, normalized_title):
            return ExperienceLevel.INTERN
        if _matches(self._senior, normalized_title):
            return ExperienceLevel.SENIOR
        if _matches(self._mid, normalized_title):
            return ExperienceLevel.MID
        if _matches(self._new_grad, normalized_title):
            return ExperienceLevel.NEW_GRAD
        if _matches(self._entry, normalized_title):
            return ExperienceLevel.ENTRY
        return self._level_from_description(description)

    def _level_from_description(self, description: str | None) -> ExperienceLevel:
        normalized = normalize_for_matching(description)
        if not normalized:
            return ExperienceLevel.UNKNOWN
        if _matches(self._description_new_grad, normalized):
            return ExperienceLevel.NEW_GRAD
        if _matches(self._description_entry, normalized):
            return ExperienceLevel.ENTRY
        for years in _YEARS_OF_EXPERIENCE.finditer(description or ""):
            # Only count "N years" that is actually about experience, not "founded 10 years ago".
            window = (description or "")[max(0, years.start() - 60): years.end() + 60]
            if "experience" not in window.lower():
                continue
            minimum_years = int(years.group(1))
            if minimum_years <= 1:
                return ExperienceLevel.ENTRY
            if minimum_years >= 5:
                return ExperienceLevel.SENIOR
            if minimum_years >= 3:
                return ExperienceLevel.MID
            break  # 2 years: ambiguous, leave unknown
        return ExperienceLevel.UNKNOWN

    # -- employment type -------------------------------------------------------------------

    def employment_type(
        self, title: str | None, description: str | None = None, hint: str | None = None
    ) -> EmploymentType:
        normalized_title = normalize_for_matching(title)
        if _matches(self._intern, normalized_title):
            return EmploymentType.INTERNSHIP
        from_hint = self._type_from_text(normalize_for_matching(hint))
        if from_hint is not EmploymentType.UNKNOWN:
            return from_hint
        from_title = self._type_from_text(normalized_title, include_intern=False)
        if from_title is not EmploymentType.UNKNOWN:
            return from_title
        head = normalize_for_matching((description or "")[:_DESCRIPTION_HEAD_CHARS])
        return self._type_from_text(head)

    def _type_from_text(self, normalized_text: str, *, include_intern: bool = True) -> EmploymentType:
        if not normalized_text:
            return EmploymentType.UNKNOWN
        if include_intern and _matches(self._intern, normalized_text):
            return EmploymentType.INTERNSHIP
        if _matches(self._part_time, normalized_text):
            return EmploymentType.PART_TIME
        if _matches(self._contract, normalized_text):
            return EmploymentType.CONTRACT
        if _matches(self._full_time, normalized_text):
            return EmploymentType.FULL_TIME
        return EmploymentType.UNKNOWN

    # -- graduation year -------------------------------------------------------------------

    @staticmethod
    def graduation_year_hint(*texts: str | None) -> int | None:
        """First plausible graduation year mentioned ("class of 2027", "2027 new grad", ...)."""
        for text in texts:
            if not text:
                continue
            for pattern in _GRADUATION_YEAR_PATTERNS:
                for match in pattern.finditer(text):
                    year = int(match.group(1))
                    if _MIN_GRADUATION_YEAR <= year <= _MAX_GRADUATION_YEAR:
                        return year
        return None
