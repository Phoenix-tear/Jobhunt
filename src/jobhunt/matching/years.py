"""Required years of experience: the lower bound a posting asks for (deterministic, no model).

Policy lives in the gate (`relevance.seniority.exclude_at_years` / `maybe_at_years`); this module only answers
"how many years does this text require, at least?". It reads one sentence or bullet at a time and counts a phrase
only when it is a work-experience requirement:

* ranges count their lower bound ("2-4 years" -> 2, "2 to 3 years" -> 2, "0-2 years" -> 0);
* "2+", "2 plus", "2 or more", "minimum / at least / minimum of 2 years", "two years of experience", "3 yrs",
  "three (3) years", "24 months of experience" (-> 2) all count;
* upper bounds mean "none required" ("up to 2 years", "less than 2 years", "no more than 2 years" -> 0);
* non-work uses are ignored: "within 2 years of graduating", "in the last 2 years", "2 years of coursework",
  "a 2-year degree / contract / programme", "2 years ago", "10 years in business", "years old";
* a requirement softened by "preferred", "a plus", "nice to have", "ideally", "bonus" does not count;
* the sentence must talk about experience, background, hands-on work, "minimum", "required", ... (conservative:
  "Python for 2 years" with no requirement wording does not count). A title like "Engineer (3+ years)" counts.
"""

from __future__ import annotations

import re

_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15,
}
_NUM = r"(?:\d{1,2}(?:\.\d)?|" + "|".join(_WORDS) + r")"
_UNIT = r"(?:years?|yrs?)"
# "three (3) years", "3 (three) years": the parenthesised repeat is skipped
_REPEAT = r"(?:\s*\(\s*(?:\d{1,2}|[a-z]+)\s*\))?"

_RANGE = re.compile(
    rf"(?<![\w.\-])(?P<low>{_NUM}){_REPEAT}\s*(?:\+\s*)?(?:-|–|—|to|or)\s*(?P<high>{_NUM}){_REPEAT}\s*\+?\s*{_UNIT}\b",
    re.IGNORECASE,
)
_SINGLE = re.compile(
    rf"(?<![\w.\-])(?P<low>{_NUM}){_REPEAT}\s*(?P<plus>\+|plus\b|or\s+more\b|and\s+(?:above|up|more|over)\b|or\s+above\b)?"
    rf"\s*{_UNIT}\b",
    re.IGNORECASE,
)
_MONTHS = re.compile(r"(?<![\w.])(?P<low>\d{2})\s*(?:\+\s*)?months?\b", re.IGNORECASE)
_A_YEAR = re.compile(r"\b(?:a|one)\s+(?:full\s+)?year\b", re.IGNORECASE)

# sentence ends, bullets, and " - Capitalised" bullets of flattened descriptions (a range "2 - 4 years" has a digit after)
_SENTENCE_SPLIT = re.compile(r"[\n\r•●▪*;|]+|(?<!\d)\.(?=\s+[A-Z(\d]|\s*$)|[!?]+|\s[-–—]\s+(?=[A-Z])")

# the sentence is about work experience / a requirement
_CONTEXT = re.compile(
    r"experience|experienced|\bexp\b|background|track record|hands[\s-]?on|professional|industry|expertise|proficien|"
    r"work(?:ed|ing)?\s+(?:with|in|as|on|experience)|\bworked\b|employment|in a (?:similar|related|relevant|comparable) role|"
    r"in the field|in (?:a )?(?:software|engineering|development|programming|technical|data|ml|ai)\b|"
    r"minimum|at least|min\.|required|requires?|must have|should have|you have|you(?:.ll|\s+will)?\s+(?:bring|need|have)|we.re looking for|"
    r"qualif",
    re.IGNORECASE,
)
_SOFT = re.compile(
    r"\b(?:preferred|prefer|a plus|plus point|nice to have|nice-to-have|bonus|ideally|desirable|desired|an advantage|"
    r"would be (?:great|a plus|an advantage|helpful)|is a plus|good to have)\b",
    re.IGNORECASE,
)
# what follows the number means it is not a work requirement
_NOT_WORK_AFTER = re.compile(
    r"^\W*(?:of\s+age|old\b|ago|in\s+business|of\s+history|since|of\s+(?:coursework|study|studies|schooling|education|"
    r"university|college|school|academic|formal)|(?:bachelor|degree|diploma|course|program|programme|contract|term|fixed|bond|"
    r"rotation|cycle|visa|lease|warranty|apprenticeship|placement|curriculum|horizon|roadmap|plan|period|residen\w+|citizen\w*|tenure|notice|probation|lock[\s-]?in|service|"
    r"post\W?doc)\b|from\s+(?:now|the\s+date)|after\s+(?:graduat|completion|the\s+start))",
    re.IGNORECASE,
)
# what precedes the number means it is not a work requirement (or it is an upper bound)
_NOT_WORK_BEFORE = re.compile(
    r"(?:\b(?:last|past|over\s+the|within|every|next|first|previous|following|during\s+the|for\s+the\s+(?:past|last)|"
    r"in\s+the\s+(?:last|past|next)|after)\s+(?:the\s+)?|\b(?:graduat\w*|completed?|finish\w*)\b.{0,40}"
    r"\bin\s+)$",
    re.IGNORECASE,
)
_UPPER_BOUND_BEFORE = re.compile(
    r"\b(?:up\s+to|upto|less\s+than|fewer\s+than|under|below|maximum(?:\s+of)?|max\.?(?:\s+of)?|no\s+more\s+than|"
    r"not\s+more\s+than|at\s+most|a\s+maximum\s+of|upper\s+limit\s+of)\s*$",
    re.IGNORECASE,
)
_UPPER_BOUND_AFTER = re.compile(r"^\s*(?:or\s+less|or\s+fewer|and\s+under|maximum|max\b)", re.IGNORECASE)


def _number(text: str) -> int | None:
    lowered = text.lower()
    if lowered in _WORDS:
        return _WORDS[lowered]
    try:
        value = int(float(lowered))
    except ValueError:
        return None
    return value if 0 <= value <= 40 else None


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part and part.strip()]


# headings that open / close the part of a posting that states requirements (flattened descriptions keep them inline)
_REQUIREMENTS_HEADING = re.compile(
    r"requirements?|qualifications?|what you.?(?:ll|\s+will)?\s*(?:bring|need|have)|what we.?re\s+looking\s+for|who you are|"
    r"about you|you.?(?:ll|\s+will)?\s*(?:have|bring)|must[\s-]?haves?|skills\s+(?:and|&)\s+experience|your\s+background|"
    r"experience we (?:value|need|expect)|education\s*(?:and|/|&)\s*(?:or\s*)?experience|required\s+(?:experience|skills)|"
    r"minimum\s+qualifications?|basic\s+qualifications?|what we require|we require|required|"
    r"knowledge,?\s+(?:and\s+)?experience|skills,?\s+knowledge",
    re.IGNORECASE)
_SOFT_HEADING = re.compile(
    r"preferred|nice[\s-]to[\s-]haves?|bonus|good to have|a plus|desirable|extra credit|even better|great to have",
    re.IGNORECASE)
_CLOSED_HEADING = re.compile(          # parts of a posting that are never about the candidate's experience
    r"benefits|perks|what we offer|why (?:join|you.ll love)|about (?:us|the company)|what you.?ll love|"
    r"our (?:mission|story|values|culture)|how to apply|compensation|salary|equal opportunity",
    re.IGNORECASE)
_OTHER_HEADING = re.compile(
    r"about (?:the team|the role|\w+)|responsibilities|what you.?(?:ll|\s+will)\s*do|the role",
    re.IGNORECASE)

HEADING_START_CHARS = 25    # a heading opens its line ("Requirements", "What you bring - 3+ years ..."), it is not mid-sentence prose
SOFT_BEFORE_CHARS = 40      # "ideally" / "nice to have" in the same clause, this close before the years phrase softens it ...
SOFT_AFTER_CHARS = 45       # ... and "preferred" / "is a plus" in the same clause after it; across a comma it describes something else
COMPANY_AGE_YEARS = 20      # this many years or more needs the word "experience" (anything else is a company's age)
LARGE_YEARS = 8             # this many years or more is ignored when the sentence talks about the company, not about experience
_EXPERIENCE_WORD = re.compile(r"experience|experienced", re.IGNORECASE)
MAX_UNCONTEXTED_YEARS = 11  # an "N+ years" line with no experience wording counts only below this (25+ years = company history)
_HISTORY_CUES = re.compile(r"\b(?:we|our|us|company|founded|since|history|customers?|clients?|serving|served|business|decades?|partners?|firm|provider|organi[sz]ation|team|model)\b",
                           re.IGNORECASE)
_CLAUSE_END = re.compile(r"[,;:()]")


def _clause_before(sentence: str, start: int) -> str:
    text = sentence[max(0, start - SOFT_BEFORE_CHARS): start]
    parts = re.split(r"[,;()]", text)         # a label ("Nice to have:") still applies to what follows it
    return parts[-1] if parts else text


def _clause_after(sentence: str, end: int) -> str:
    return _CLAUSE_END.split(sentence[end: end + SOFT_AFTER_CHARS])[0]
SOFT_HEADING_CHARS = 30     # a line that OPENS with "Preferred ..." is soft as a whole


SOFT_SECTION_SENTENCES = 8


def _is_new_soft_heading(sentence: str) -> bool:
    return any(m.start() <= 4 for m in _SOFT_HEADING.finditer(sentence[:HEADING_START_CHARS + 30]))


def _in_requirements(sentence: str, current: str) -> str:
    """Section of this sentence: "req" (requirements), "soft" (preferred / nice to have), "closed" (benefits, about us),
    "other" (responsibilities, the role), or the current one.

    The heading at the START of the line decides ("What you bring - ...", "Preferred qualifications"); ordinary prose that
    merely uses the word "requirements" does not open a section."""
    head = sentence[:HEADING_START_CHARS + 30]
    hits = [(m.start(), "req") for m in _REQUIREMENTS_HEADING.finditer(head)]
    hits += [(m.start(), "soft") for m in _SOFT_HEADING.finditer(head) if m.start() <= 4]   # the line OPENS with it
    hits += [(m.start(), "other") for m in _OTHER_HEADING.finditer(head)]
    hits += [(m.start(), "closed") for m in _CLOSED_HEADING.finditer(head)]
    hits = [hit for hit in hits if hit[0] <= HEADING_START_CHARS]
    if not hits:
        return current
    return min(hits, key=lambda hit: hit[0])[1]


def _sentence_requirement(sentence: str, *, force_context: bool = False, section: str = "") -> tuple[int, str] | None:
    """(lower bound, phrase) for one sentence, or None when it states no work-experience requirement."""
    has_context = force_context or section == "req" or bool(_CONTEXT.search(sentence))
    if section == "soft" or _SOFT.search(sentence[:SOFT_HEADING_CHARS]):
        return None
    best: tuple[int, str] | None = None

    def consider(low: int | None, phrase: str, start: int, end: int, *, explicit: bool = False,
                 upper_checked: bool = False) -> None:
        nonlocal best
        if low is None:
            return
        if low >= COMPANY_AGE_YEARS and not _EXPERIENCE_WORD.search(sentence):
            return                  # "a 30+ year track record": a company's age, not a candidate's experience
        if low >= LARGE_YEARS and _HISTORY_CUES.search(sentence) and not _EXPERIENCE_WORD.search(sentence):
            return                  # "for a minimum of ten years" about a partnership, "more than 15 years of expertise" about us
        if not has_context and not (explicit and section != "closed" and low <= MAX_UNCONTEXTED_YEARS
                                    and not _HISTORY_CUES.search(sentence)):
            return                  # "26 years in the making", "for 12+ years we served banks": not a job requirement
        before = sentence[max(0, start - 45): start]
        after = sentence[end: end + 40]
        if _NOT_WORK_AFTER.search(after) or _NOT_WORK_BEFORE.search(before):
            return
        if _SOFT.search(_clause_before(sentence, start)) or _SOFT.search(_clause_after(sentence, end)):
            return                  # "ideally 3 years", "3 years of experience is a plus", "nice to have: 3 years"
        if not upper_checked and (_UPPER_BOUND_BEFORE.search(before) or _UPPER_BOUND_AFTER.search(after)):
            low = 0
        if best is None or low > best[0]:
            best = (low, phrase)

    covered: list[tuple[int, int]] = []
    for match in _RANGE.finditer(sentence):
        covered.append(match.span())
        consider(_number(match.group("low")), match.group(0), match.start(), match.end(), explicit=True)
    for match in _SINGLE.finditer(sentence):
        if any(start <= match.start() < end for start, end in covered):
            continue
        low = _number(match.group("low"))
        # "5+" after "up to" is still an upper bound; "over 5 years" is a lower bound of 5
        consider(low, match.group(0), match.start(), match.end(), explicit=bool(match.group("plus")))
    for match in _MONTHS.finditer(sentence):
        months = int(match.group("low"))
        if months >= 12:
            consider(months // 12, match.group(0), match.start(), match.end())
    if best is None and re.search(r"\b(?:a|one)\s+(?:full\s+)?year\s+(?:of|'s|’s)\s+(?:\w+\s+){0,3}experience", sentence,
                                  re.IGNORECASE):
        best = (1, "a year of experience")
    return best


def required_years(description: str | None, title: str | None = None) -> tuple[int | None, str | None]:
    """(years, matched phrase): the largest lower bound asked for as work experience; (None, None) when none is stated.

    0 is returned for an explicit "0-2 years" / "up to 2 years" so callers can tell "asks for none" from "silent"."""
    best: tuple[int, str] | None = None
    if title:
        found = _sentence_requirement(title, force_context=True)
        if found:
            best = found
    section, soft_left = "", 0
    for sentence in _sentences(description or ""):
        before = section
        section = _in_requirements(sentence, section)
        if section == "soft":
            # a "Preferred qualifications" list is a few bullets long; do not let it swallow the rest of the posting
            soft_left = SOFT_SECTION_SENTENCES if before != "soft" or _is_new_soft_heading(sentence) else soft_left - 1
            if soft_left < 0:
                section = ""
        found = _sentence_requirement(sentence, section=section)
        if found and (best is None or found[0] > best[0]):
            best = found
    return (best[0], best[1]) if best else (None, None)
