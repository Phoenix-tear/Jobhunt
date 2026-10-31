"""Turn messy source fields into the clean values stored in `jobs`.

HTML -> text, salary parsing/annualizing, slug prettifying and `enrich_job`, which fills the
derived fields (country/city/remote flag, employment type, experience level, grad year).
"""

from __future__ import annotations

import html
import re
import warnings
from typing import Any

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from jobhunt.classify import Classifier
from jobhunt.locations import LocationParser
from jobhunt.models import EmploymentType, ExperienceLevel, NormalizedJob

MAX_DESCRIPTION_CHARS = 60_000

_BLOCK_TAGS = ["p", "div", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
               "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"]
_ENCODED_MARKUP = re.compile(r"&lt;\s*/?\s*[a-zA-Z]")
_HAS_ENTITY_OR_TAG = re.compile(r"[<&]")
_SPACES_AND_TABS = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_EMPTY_LINES = re.compile(r"\n{2,}")


# --- text ---------------------------------------------------------------------------------

def html_to_text(raw: Any) -> str:
    """HTML (possibly entity-encoded, e.g. Greenhouse) -> readable plain text.

    Block elements become line breaks, list items become "- " bullets, scripts and styles
    are dropped, entities are decoded. Never raises: non-string input yields "".
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = raw
    # Greenhouse sends the whole fragment entity-encoded ("&lt;p&gt;..."): decode that first.
    for _ in range(2):
        if "<" not in text and _ENCODED_MARKUP.search(text):
            text = html.unescape(text)
        else:
            break

    from_markup = bool(_HAS_ENTITY_OR_TAG.search(text))
    if from_markup:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
            soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        for line_break in soup.find_all("br"):
            line_break.replace_with("\n")
        for item in soup.find_all("li"):
            item.insert_before("\n- ")
        for block in soup.find_all(_BLOCK_TAGS):
            block.append("\n")
        text = soup.get_text()
        text = html.unescape(text)  # double-encoded leftovers such as "&amp;amp;"

    lines = (_SPACES_AND_TABS.sub(" ", line).strip() for line in text.split("\n"))
    joined = "\n".join(lines)
    # Markup yields many empty lines between blocks: keep one line per block. Plain text may
    # carry deliberate paragraph breaks: keep at most one blank line.
    cleaned = (_EMPTY_LINES if from_markup else _BLANK_LINES).sub("\n" if from_markup else "\n\n", joined).strip()
    return cleaned[:MAX_DESCRIPTION_CHARS]


def clean_text(value: Any) -> str | None:
    """Single-line trimmed string, or None for empty / non-string values."""
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def prettify_slug(slug: str) -> str:
    """'acme-corp_labs' -> 'Acme Corp Labs' (placeholder company name until the ATS says better)."""
    words = re.split(r"[-_.\s]+", slug.strip())
    return " ".join(word.capitalize() for word in words if word) or slug


# --- salary -------------------------------------------------------------------------------

_ANNUALIZATION_FACTORS = {
    "year": 1, "yearly": 1, "annual": 1, "annually": 1, "annum": 1, "pa": 1,
    "month": 12, "monthly": 12, "week": 52, "weekly": 52,
    "day": 260, "daily": 260, "hour": 2080, "hourly": 2080,
}
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
_CURRENCY_CODES = {"USD", "EUR", "GBP", "INR", "CAD", "AUD", "SGD", "CHF", "SEK", "NOK", "DKK", "PLN", "JPY"}
_SALARY_RANGE = re.compile(
    r"(?P<cur1>[$€£₹¥]|\b[A-Z]{3}\b)?\s*(?P<low>\d[\d,]*(?:\.\d+)?)\s*(?P<suf1>k|lpa|lakhs?|m)?\b"
    r"(?:\s*(?:-|–|—|to)\s*(?P<cur2>[$€£₹¥]|\b[A-Z]{3}\b)?\s*(?P<high>\d[\d,]*(?:\.\d+)?)\s*(?P<suf2>k|lpa|lakhs?|m)?\b)?",
    re.IGNORECASE,
)


def normalize_interval(value: Any) -> str | None:
    """'YEAR', '1 YEAR', 'PER_HOUR', 'yearly' -> canonical key of _ANNUALIZATION_FACTORS."""
    if not isinstance(value, str):
        return None
    token = re.sub(r"[^a-z]", " ", value.lower()).split()
    for word in token:
        if word in _ANNUALIZATION_FACTORS:
            return word
    return None


def annualize_salary(
    minimum: float | None, maximum: float | None, interval: str | None
) -> tuple[float | None, float | None]:
    """Convert a pay range to a per-year figure; unknown or missing interval = already annual."""
    key = normalize_interval(interval) if interval else None
    factor = _ANNUALIZATION_FACTORS.get(key, 1) if key else 1
    return (
        round(minimum * factor, 2) if minimum is not None else None,
        round(maximum * factor, 2) if maximum is not None else None,
    )


def to_number(value: Any) -> float | None:
    """Tolerant numeric coercion for salary fields ("120,000", 120000, None, "n/a")."""
    if value is None or isinstance(value, bool):
        return None
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
        if match:
            try:
                number = float(match.group(0).replace(",", ""))
            except ValueError:
                number = None
    # NaN and zero are "no figure" (a 0 minimum is a placeholder, not a salary).
    return number if number is not None and number == number and number > 0 else None


def parse_salary_text(text: Any) -> tuple[float, float, str | None] | None:
    """Best-effort "$120,000 - $150,000", "£45k", "₹12-18 LPA" -> (min, max, currency).

    A match needs a currency marker or a k/LPA suffix, so years and phone numbers are ignored.
    """
    if not isinstance(text, str):
        return None
    for match in _SALARY_RANGE.finditer(text):
        currency_marker = match["cur1"] or match["cur2"]
        suffix_low, suffix_high = (match["suf1"] or "").lower(), (match["suf2"] or "").lower()
        currency = _currency_code(currency_marker)
        has_scale = bool(suffix_low or suffix_high)
        if currency is None and not has_scale:
            continue
        low = _scaled(match["low"], suffix_low or suffix_high)
        high = _scaled(match["high"], suffix_high or suffix_low) if match["high"] else low
        if low is None or high is None or low < 1000:
            continue
        if suffix_low in {"lpa", "lakh", "lakhs"} or suffix_high in {"lpa", "lakh", "lakhs"}:
            currency = currency or "INR"
        return (min(low, high), max(low, high), currency)
    return None


def _currency_code(marker: str | None) -> str | None:
    if not marker:
        return None
    if marker in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[marker]
    upper = marker.upper()
    return upper if upper in _CURRENCY_CODES else None


def _scaled(number_text: str | None, suffix: str) -> float | None:
    number = to_number(number_text)
    if number is None:
        return None
    if suffix == "k":
        return number * 1_000
    if suffix in {"lpa", "lakh", "lakhs"}:
        return number * 100_000
    if suffix == "m":
        return number * 1_000_000
    return number


# --- enrichment ---------------------------------------------------------------------------

def enrich_job(
    job: NormalizedJob,
    locations: LocationParser,
    classifier: Classifier,
    *,
    employment_hint: str | None = None,
) -> NormalizedJob:
    """Fill every derived field that the adapter left unknown. Mutates and returns `job`."""
    if job.country is None or job.city is None or job.is_remote is None:
        parsed = locations.parse(job.location_raw)
        job.country = job.country or parsed.country
        job.city = job.city or parsed.city
        if job.is_remote is None:
            job.is_remote = parsed.is_remote
    job.city = locations.canonical_city(job.city)

    if job.experience_level is ExperienceLevel.UNKNOWN:
        job.experience_level = classifier.experience_level(job.title, job.description_text)
    if job.employment_type is EmploymentType.UNKNOWN:
        job.employment_type = classifier.employment_type(job.title, job.description_text, employment_hint)
    # Two-way link: an intern-level role is an internship even if the source calls it
    # "permanent/full-time", and an internship without a stated level is intern-level.
    if job.experience_level is ExperienceLevel.INTERN:
        job.employment_type = EmploymentType.INTERNSHIP
    elif job.employment_type is EmploymentType.INTERNSHIP and job.experience_level is ExperienceLevel.UNKNOWN:
        job.experience_level = ExperienceLevel.INTERN
    if job.graduation_year_hint is None:
        job.graduation_year_hint = classifier.graduation_year_hint(job.title, job.description_text)
    return job
