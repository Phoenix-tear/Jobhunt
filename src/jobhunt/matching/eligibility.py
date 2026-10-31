"""Education-level and legal eligibility flags found in posting text (deterministic, no model).

`detect_eligibility(title, description, degrees)` returns `[{kind, strength, snippet}]`:

* `phd_required`, `masters_required`, `us_citizen_or_clearance_required` with strength `required` or `preferred`
  ("a plus / preferred / nice to have" lowers a flag to `preferred`; "Bachelor's, Master's or PhD" gives no flag);
* `us_work_authorization`, `must_be_enrolled_in_us_university` are information only (always `preferred`; the API hides
  the work-authorization chips for jobs the location policy already shows as open to India).

The wording is factual ("posting says: ..."). A `required` flag never deletes a job: it keeps it at `maybe` at best and
caps its score (see gate.py and scoring.py). Text is untrusted: sentences are length-capped and every pattern is
bounded, so hostile input cannot make this slow.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MAX_TEXT_CHARS = 60_000
MAX_SENTENCE_CHARS = 500
SNIPPET_CHARS = 120

REQUIRED, PREFERRED = "required", "preferred"
KINDS = ("phd_required", "masters_required", "us_citizen_or_clearance_required", "us_work_authorization",
         "must_be_enrolled_in_us_university")
INFO_ONLY_KINDS = ("us_work_authorization", "must_be_enrolled_in_us_university")

# sentence ends: "!?;" or a period after two letters/digits (so "U.S. citizens" and "e.g. x" are not split)
_SPLIT = re.compile(r"(?<=[!?;])\s+|(?<=[A-Za-z0-9)\"']{2}\.)\s+|\n+|\s[••*]\s")
_PHD = r"(?:ph\.?\s?d\.?|doctorate|doctoral)"
_MASTERS = r"(?:master'?s?|masters|m\.?\s?sc\.?|m\.?\s?tech|m\.?\s?s\.?(?=\W)|m\.?\s?eng\.?)"

_PHD_REQUIRED = re.compile(
    rf"(?:\b(?:currently\s+)?(?:pursuing|enrolled\s+in|working\s+(?:towards?|on)|completing|studying\s+for|in)\b[^.\n]{{0,40}}?\b{_PHD}\b)|"
    rf"(?:\b{_PHD}\s+(?:student|candidate)s?\b)|(?:\bdoctoral\s+(?:student|candidate)s?\b)|"
    rf"(?:\b{_PHD}\b[^.\n]{{0,25}}\b(?:is\s+|are\s+)?required\b)|(?:\brequires?\s+(?:a\s+|an\s+)?{_PHD}\b)|"
    rf"(?:\bmust\s+(?:be|have|hold)\b[^.\n]{{0,30}}\b{_PHD}\b)"
)
_MASTERS_REQUIRED = re.compile(
    rf"(?:\b(?:currently\s+)?(?:pursuing|enrolled\s+in|working\s+(?:towards?|on)|completing|studying\s+for)\b[^.\n]{{0,30}}?\b{_MASTERS}(?:\W|$))|"
    rf"(?:\b{_MASTERS}\s+(?:degree\s+)?(?:student|candidate)s?\b)|"
    rf"(?:\b{_MASTERS}\b[^.\n]{{0,25}}\b(?:is\s+|are\s+)?required\b)|(?:\brequires?\s+(?:a\s+|an\s+)?{_MASTERS}\b)|"
    rf"(?:\bmust\s+(?:have|hold|be\s+pursuing)\b[^.\n]{{0,30}}\b{_MASTERS}\b)"
)
_CLEARANCE = re.compile(
    r"\b(?:u\.?s\.?|united\s+states)\s+citizen(?:s|ship)?\s+(?:only|required)\b|"
    r"\bmust\s+be\s+(?:a\s+)?(?:u\.?s\.?\s+|united\s+states\s+)citizen\b|\bu\.?s\.?\s+persons?\s+only\b|"
    r"\b(?:security\s+)?clearance\s+(?:is\s+)?required\b|\brequires?\s+(?:a\s+|an\s+)?(?:active\s+|current\s+)?(?:security\s+)?clearance\b|"
    r"\b(?:active|current)\s+(?:ts/sci|top\s+secret|secret|security)\s+clearance\b|\bts/sci\b|"
    r"\bmust\s+(?:be\s+able\s+to\s+)?(?:obtain|hold|have)\s+(?:a\s+|an\s+)?(?:u\.?s\.?\s+)?(?:government\s+)?(?:security\s+)?clearance\b"
)
_US_AUTHORIZATION = re.compile(
    r"\bauthori[sz]ed\s+to\s+work\s+in\s+the\s+(?:u\.?s\.?|united\s+states)\b|"
    r"\bwork\s+authori[sz]ation\s+(?:in|for)\s+the\s+(?:u\.?s\.?|united\s+states)\b|\blegally\s+authori[sz]ed\s+to\s+work\s+in\s+the\s+u")
_US_ENROLLED = re.compile(
    r"\benrolled\s+(?:in|at)\s+(?:a|an)\s+(?:accredited\s+)?(?:u\.?s\.?|united\s+states|american)\s+(?:college|university|institution|school)\b|"
    r"\b(?:u\.?s\.?|united\s+states)\s+(?:college|university)\s+(?:student|enrollment)\b")
_PREFERRED_CUE = re.compile(
    r"\b(?:a\s+plus|is\s+a\s+plus|preferred|nice\s+to\s+have|nice-to-have|bonus|desirable|an\s+advantage|advantageous|"
    r"ideally|or\s+equivalent|welcome|encouraged|we\s+also|may\s+also|not\s+required|optional)\b")
_UNDERGRAD = re.compile(r"\b(?:bachelor|undergraduate|b\.?\s?tech|b\.?\s?sc|bs\b|ba\b|bs/ms|any\s+degree|"
                        r"or\s+equivalent\s+(?:practical\s+)?experience)")
_MASTERS_WORD = re.compile(rf"\b{_MASTERS}")
_LOWER_DEGREES = ("bachelor", "undergraduate", "associate", "diploma", "b.tech", "btech")


def _sentences(text: str) -> Iterable[str]:
    for sentence in _SPLIT.split(text[:MAX_TEXT_CHARS]):
        sentence = sentence.strip()
        if sentence:
            yield sentence[:MAX_SENTENCE_CHARS]


def _snippet(sentence: str, start: int, end: int) -> str:
    """At most 120 characters of the matching text, centred on the match."""
    if end - start >= SNIPPET_CHARS:
        return " ".join(sentence[start:start + SNIPPET_CHARS].split())
    pad = (SNIPPET_CHARS - (end - start)) // 2
    return " ".join(sentence[max(0, start - pad): end + pad].split())[:SNIPPET_CHARS]


def _strength(lowered: str) -> str:
    return PREFERRED if _PREFERRED_CUE.search(lowered) else REQUIRED


def _flags_for_sentence(sentence: str) -> list[dict[str, str]]:
    lowered = sentence.lower().replace("’", "'")
    found: list[dict[str, str]] = []
    alternatives = bool(_UNDERGRAD.search(lowered))
    mentions_masters = bool(_MASTERS_WORD.search(lowered))

    phd = _PHD_REQUIRED.search(lowered)
    masters = _MASTERS_REQUIRED.search(lowered)
    if phd and not alternatives:
        if mentions_masters:             # "Master's or PhD": a graduate degree, reported as the lower requirement
            found.append({"kind": "masters_required", "strength": _strength(lowered),
                          "snippet": _snippet(sentence, phd.start(), phd.end())})
        else:
            found.append({"kind": "phd_required", "strength": _strength(lowered),
                          "snippet": _snippet(sentence, phd.start(), phd.end())})
    elif masters and not alternatives and not phd:
        found.append({"kind": "masters_required", "strength": _strength(lowered),
                      "snippet": _snippet(sentence, masters.start(), masters.end())})

    clearance = _CLEARANCE.search(lowered)
    if clearance:
        found.append({"kind": "us_citizen_or_clearance_required", "strength": _strength(lowered),
                      "snippet": _snippet(sentence, clearance.start(), clearance.end())})
    for kind, pattern in (("us_work_authorization", _US_AUTHORIZATION), ("must_be_enrolled_in_us_university", _US_ENROLLED)):
        match = pattern.search(lowered)
        if match:
            found.append({"kind": kind, "strength": PREFERRED, "snippet": _snippet(sentence, match.start(), match.end())})
    return found


def flags_from_degrees(degrees: Iterable[Any] | None, strength: str = PREFERRED) -> list[dict[str, str]]:
    """Source lists (Simplify) name the degrees a posting accepts; no bachelor-level entry means it is graduate-only.

    Default strength is `preferred` (an information chip, no score effect): the source list is not the posting, and a
    job I labelled relevant (#4904, listed as PhD-only) shows it can disagree with what I would apply to. Set
    `relevance.eligibility_source_lists_required: true` to treat it as `required`."""
    names = [str(item).strip() for item in (degrees or []) if str(item).strip()]
    if not names:
        return []
    lowered = [n.lower().replace("’", "'") for n in names]
    if any(any(word in item for word in _LOWER_DEGREES) for item in lowered):
        return []
    has_phd = any(re.search(_PHD, item) for item in lowered)
    has_masters = any(_MASTERS_WORD.search(item) for item in lowered)
    snippet = f"source list says degrees: {', '.join(names)}"[:SNIPPET_CHARS]
    if has_phd and not has_masters:
        return [{"kind": "phd_required", "strength": strength, "snippet": snippet}]
    if has_masters:
        return [{"kind": "masters_required", "strength": strength, "snippet": snippet}]
    return []


def detect_eligibility(title: str | None, description: str | None, degrees: Iterable[Any] | None = None, *,
                       degree_list_strength: str = PREFERRED) -> list[dict[str, str]]:
    """Flags for one job, strongest strength kept per kind, at most one flag per kind."""
    text = f"{title or ''}\n{description or ''}"
    best: dict[str, dict[str, str]] = {}
    for flag in [*flags_from_degrees(degrees, degree_list_strength), *(f for s in _sentences(text) for f in _flags_for_sentence(s))]:
        current = best.get(flag["kind"])
        if current is None or (current["strength"] == PREFERRED and flag["strength"] == REQUIRED):
            best[flag["kind"]] = flag
    return list(best.values())


def required_flags(flags: Iterable[dict[str, str]] | None) -> list[dict[str, str]]:
    return [f for f in (flags or []) if f.get("strength") == REQUIRED and f.get("kind") not in INFO_ONLY_KINDS]


FLAG_LABELS = {
    "phd_required": "PhD required",
    "masters_required": "Master's required",
    "us_citizen_or_clearance_required": "US citizenship or clearance required",
    "us_work_authorization": "US work authorization mentioned",
    "must_be_enrolled_in_us_university": "US university enrollment mentioned",
}


def likely_ineligible_note(flags: Iterable[dict[str, str]]) -> str:
    """"Likely not eligible: PhD required" (factual; the snippet shows what the posting says)."""
    required = required_flags(flags)
    if not required:
        return ""
    return "Likely not eligible: " + ", ".join(FLAG_LABELS.get(f["kind"], f["kind"]) for f in required)
