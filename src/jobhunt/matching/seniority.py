"""Rule-based seniority level of a job: junior | unspecified | mid | senior | executive.

Purely deterministic (no model): ordered regular expressions over the normalized title (and, for
configured level codes, the start of the description). The first matching rule decides and its
evidence is kept so a verdict can always be explained.

Order matters and is the whole design:
  1. compound senior titles first ("Associate Vice President", "Associate Director", "Associate
     Principal" must never be read as the junior "Associate");
  2. executive words, then senior words, then level numbers (II or higher);
  3. configured level codes (employer grade codes seen in real data, e.g. Citi "C11");
  4. explicit junior markers;
  5. otherwise `unspecified`.
A *strong* junior marker (intern, trainee, graduate, apprentice, ...) beats the weak rank words
"lead", "manager", "staff", "architect", so "Management Trainee" and "Graduate Engineer - Staff
Services" stay junior.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

JUNIOR = "junior"
UNSPECIFIED = "unspecified"
CAUTION = "caution"      # specialist / L4: not excluded, but a target is capped at maybe
MID = "mid"
SENIOR = "senior"
EXECUTIVE = "executive"
LEVELS = (JUNIOR, UNSPECIFIED, CAUTION, MID, SENIOR, EXECUTIVE)
EXCLUDED_LEVELS = frozenset({MID, SENIOR, EXECUTIVE})

_ROLE = (r"(?:engineer|engineers|engineering|developer|developers|programmer|scientist|analyst|architect|consultant|"
         r"specialist|researcher|administrator|designer|tester|technologist)")
_NOT_LEAD_RANK = r"(?!\s*(?:gen\b|generation|gen\s|management|capture|qualification|scoring|nurturing|to\b|time))"

# (level, rule name, pattern) in priority order. Patterns run on lower-case text with separators
# turned into spaces; "." and "-" are kept as word boundaries.
_COMPOUND_SENIOR = (
    (EXECUTIVE, "associate vice president", r"\bassociate\s+vice[\s-]+president\b"),
    (SENIOR, "associate director", r"\bassociate\s+director\b"),
    (SENIOR, "associate principal", r"\bassociate\s+principal\b"),
    (SENIOR, "associate manager", r"\bassociate\s+(?:project\s+|program\s+|product\s+|engineering\s+)?manager\b"),
)
_EXECUTIVE = (
    (EXECUTIVE, "assistant vice president", r"\b(?:assistant|asst\.?)\s+vice[\s-]+president\b"),
    (EXECUTIVE, "vice president", r"\b(?:senior\s+|executive\s+)?vice[\s-]+president\b"),
    (EXECUTIVE, "VP/AVP/SVP/EVP", r"\b(?:vp|avp|svp|evp)\b"),
    (EXECUTIVE, "managing director", r"\bmanaging\s+director\b"),
    (EXECUTIVE, "director", r"\bdirector\b"),
    (EXECUTIVE, "chief", r"\bchief\b"),
    (EXECUTIVE, "head of", r"\bhead\s+of\b"),
    # A bare "Fellow" is usually a fellowship programme (early career); only rank forms are executive.
    (EXECUTIVE, "distinguished / technical fellow",
     r"\b(?:distinguished|(?:technical|senior|principal)\s+fellow)\b"),
)
_SENIOR_STRONG = (
    (SENIOR, "principal", r"\bprincipal\b"),
    (SENIOR, "senior", r"\b(?:senior|sr|snr)\b"),
)
_SENIOR_WEAK = (        # lose against a strong junior marker
    (SENIOR, "staff", r"\bstaff\s+(?:" + _ROLE + r"|software|data|machine|ml|ai)\b"),
    (SENIOR, "lead (rank)", r"\b(?:tech(?:nical)?\s+lead|team\s+lead|lead)\b" + _NOT_LEAD_RANK),
    (SENIOR, "team leader", r"\b(?:team|tech|technical|project|delivery|squad|engineering)\s+leader\b"),
    (SENIOR, "manager", r"\bmanager\b"),
    (SENIOR, "architect", r"\barchitect\b"),
)
_LEVEL_NUMBERS = (
    (MID, "level II+", r"\b(?:" + _ROLE + r"|sde|swe|sdet|engineer|developer|associate|consultant|officer)"
                       r"\s*[-–:]?\s*(?:ii|iii|iv|v|vi|2|3|4|5|6|7)\b(?!\s*(?:\+|years|yrs|months))"),
    (MID, "level N", r"\blevel\s*(?:[2-9]|ii|iii|iv|v)\b"),
    (MID, "L5+", r"\bl\s?[5-9]\b"),            # Amazon: SDE I is L4, SDE II is L5
    (MID, "MTS II+", r"\bmember\s+of\s+technical\s+team\s*[-–:]?\s*(?:ii|iii|iv|2|3|4)\b"),
    (MID, "intermediate / mid-level", r"\b(?:intermediate|mid\s*[- ]?level|mid\s*[- ]?senior)\b"),
)
_CAUTION = (            # not excluded: the gate caps a target at maybe and the years rule decides
    (CAUTION, "specialist", r"\bspecialists?\b"),
    (CAUTION, "L4", r"\bl\s?4\b"),
)
_STRONG_JUNIOR = (
    ("intern", r"\b(?:intern|interns|internship|internships|co\s*-?\s*op|industrial\s+placement|working\s+student|werkstudent)\b"),
    ("graduate / new grad", r"\b(?:graduate|graduates|new\s+grad|new\s+graduate|recent\s+graduate|grad)\b"),
    ("trainee / apprentice", r"\b(?:trainee|trainees|apprentice|apprenticeship|apprentices)\b"),
    ("early careers / campus", r"\b(?:early\s+careers?|early\s+talent|campus|university|college\s+hire|student)\b"),
)
_JUNIOR = (
    ("junior", r"\b(?:junior|jr)\b"),
    ("entry level", r"\b(?:entry\s*[- ]?level|fresher|freshers)\b"),
    ("level I", r"\b(?:" + _ROLE + r"|sde|swe|sdet|engineer|developer|analyst)\s*[-–:]?\s*(?:i|1)\b"),
    ("level 1", r"\blevel\s*(?:1|i)\b"),
    ("associate <role>", r"\bassociate\s+(?:[a-z/&]+\s+){0,3}?" + _ROLE + r"\b"),
    ("associate (AI)", r"\bassociate\s*\(\s*[a-z/ ]+\s*\)"),
    ("analyst", r"\banalyst\b"),
    ("programmer analyst", r"\bprogrammer\b"),
)

_NORMALIZE = re.compile(r"[\s_/,;:|()\[\]{}–—]+")


def normalize_title(title: str | None) -> str:
    return _NORMALIZE.sub(" ", (title or "").lower().replace("&", " and ")).strip()


@dataclass(frozen=True)
class SeniorityVerdict:
    level: str
    evidence: str            # "title: 'assistant vice president' (rule: assistant vice president)"
    rule: str


@dataclass(frozen=True)
class LevelCodeRule:
    """An employer-specific grade code, configurable in config.yaml (`seniority.level_code_rules`)."""

    name: str
    pattern: re.Pattern[str]
    level: str
    scope: str = "any"       # "title" or "any" (title + start of description)
    note: str = ""


def compile_level_code_rules(raw_rules: Iterable[Mapping[str, Any]]) -> tuple[LevelCodeRule, ...]:
    rules: list[LevelCodeRule] = []
    for raw in raw_rules:
        level = str(raw.get("level", ""))
        if level not in LEVELS or not raw.get("pattern"):
            continue
        rules.append(LevelCodeRule(
            name=str(raw.get("name") or raw["pattern"]), pattern=re.compile(str(raw["pattern"]), re.IGNORECASE),
            level=level, scope=str(raw.get("scope", "any")), note=str(raw.get("note", "")),
        ))
    return tuple(rules)


_COMPILED = {
    name: tuple((level, rule, re.compile(pattern)) for level, rule, pattern in group)
    for name, group in {
        "compound": _COMPOUND_SENIOR, "executive": _EXECUTIVE, "senior": _SENIOR_STRONG, "weak": _SENIOR_WEAK,
        "numbers": _LEVEL_NUMBERS,
    }.items()
}
_STRONG = tuple((rule, re.compile(pattern)) for rule, pattern in _STRONG_JUNIOR)
_WEAK_JUNIOR = tuple((rule, re.compile(pattern)) for rule, pattern in _JUNIOR)
_CAUTION_COMPILED = tuple((level, rule, re.compile(pattern)) for level, rule, pattern in _CAUTION)


def classify_seniority(
    title: str | None, *, description_head: str | None = None,
    level_code_rules: Iterable[LevelCodeRule] = (),
) -> SeniorityVerdict:
    text = normalize_title(title).replace("member of technical staff", "member of technical team")
    if not text:
        return SeniorityVerdict(UNSPECIFIED, "no title", "none")

    strong_junior = next(((rule, match.group(0)) for rule, pattern in _STRONG
                          if (match := pattern.search(text))), None)

    for group in ("compound", "executive", "senior"):
        for level, rule, pattern in _COMPILED[group]:
            match = pattern.search(text)
            if match:
                # "Intern - Director's Office" style titles are rare; a strong junior marker only wins
                # against the weak words below, never against VP / director / principal / senior.
                return SeniorityVerdict(level, f"title: '{match.group(0)}'", rule)
    if strong_junior is None:
        for level, rule, pattern in _COMPILED["weak"]:
            match = pattern.search(text)
            if match:
                return SeniorityVerdict(level, f"title: '{match.group(0)}'", rule)
    for level, rule, pattern in _COMPILED["numbers"]:
        match = pattern.search(text)
        if match and strong_junior is None:
            return SeniorityVerdict(level, f"title: '{match.group(0).strip()}'", rule)

    head = (description_head or "")[:600]
    for code_rule in level_code_rules:
        haystack = (title or "") if code_rule.scope == "title" else f"{title or ''} {head}"
        match = code_rule.pattern.search(haystack)
        if match:
            if strong_junior is not None and code_rule.level in EXCLUDED_LEVELS:
                continue
            return SeniorityVerdict(code_rule.level, f"level code: '{match.group(0)}' ({code_rule.name})",
                                    f"code:{code_rule.name}")

    if strong_junior is not None:
        return SeniorityVerdict(JUNIOR, f"title: '{strong_junior[1]}'", strong_junior[0])
    for rule, pattern in _WEAK_JUNIOR:
        match = pattern.search(text)
        if match:
            return SeniorityVerdict(JUNIOR, f"title: '{match.group(0)}'", rule)
    for level, rule, pattern in _CAUTION_COMPILED:
        match = pattern.search(text)
        if match:
            return SeniorityVerdict(level, f"title: '{match.group(0)}'", rule)
    return SeniorityVerdict(UNSPECIFIED, "no seniority signal in the title", "none")
