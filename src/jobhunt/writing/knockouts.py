"""Knock-out scanner: finds eligibility screens in form questions and posting text, with no model.

Pure regex and a small lexicon (English only). Question text is untrusted data: it is length-capped, never
executed, never sent to a model (this module imports no model client, which a test asserts), and only ever
shown back as plain text. Candidate values come solely from `data/profile/application_answers.yaml`, which the
user writes; a null value means "needs your confirmation" and nothing is inferred or invented.

Wording is "possible knock-out: asks X vs your value Y", never "safe". Legal, demographic, authorization, visa,
salary, relocation-of-person and self-identification answers are never drafted: only the stored value is shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MAX_QUESTION_CHARS = 1500
MAX_QUESTIONS = 30
MAX_POSTING_CHARS = 20000

ANSWERS_FILE = "data/profile/application_answers.yaml"

RISK_POSSIBLE = "possible_knockout"
RISK_OK = "ok_by_your_value"
RISK_CONFIRM = "needs_confirmation"

KINDS = (
    "min_experience", "degree_branch", "cgpa_percentage", "backlogs", "tenth_twelfth", "graduation_year",
    "notice_period", "relocation_location", "bond", "work_authorization", "salary_ctc",
)

# Kinds that make up the one-line "eligibility screens in the posting" summary (detection, not comparison).
SCREEN_KINDS = ("cgpa_percentage", "backlogs", "tenth_twelfth", "graduation_year", "bond", "notice_period")

TEMPLATE = """\
# Your own answers to common application-form screens. YOU write this file; jobhunt never fills it in.
# `null` means "needs your confirmation": the knock-out scanner will say so instead of guessing.
# Nothing here is sent to any model. Edit values, keep the keys.

graduation_year: 2027            # from your resume
degree: null                     # e.g. "B.Tech"
branch: null                     # e.g. "Computer Science Engineering"
university: null
cgpa: null                       # your current CGPA, e.g. 8.1
cgpa_scale: null                 # the scale it is out of, e.g. 10 (or 4)
percentage_equivalent: null      # your university's CGPA-to-percentage figure, e.g. 76.9
tenth_percent: null              # class 10 percentage
twelfth_percent: null            # class 12 percentage
active_backlogs: null            # number of current backlogs / arrears, e.g. 0
history_of_backlogs: null        # true / false: any backlog ever, even if cleared
notice_period_days: null         # 0 for a student; set it if you are employed
willing_to_relocate: null        # true / false
work_authorization_india: null   # e.g. "Indian citizen"
needs_sponsorship_elsewhere: null  # true / false
expected_ctc_policy: null        # how you answer CTC questions, e.g. "negotiable, open to company range"
bond_acceptable: null            # true / false: would you accept a service bond?
"""

NUMERIC_KEYS = ("graduation_year", "cgpa", "cgpa_scale", "percentage_equivalent", "tenth_percent", "twelfth_percent",
                "active_backlogs", "notice_period_days")
BOOLEAN_KEYS = ("history_of_backlogs", "willing_to_relocate", "needs_sponsorship_elsewhere", "bond_acceptable")
TEXT_KEYS = ("degree", "branch", "university", "work_authorization_india", "expected_ctc_policy")
ALL_KEYS = NUMERIC_KEYS + BOOLEAN_KEYS + TEXT_KEYS


# --- the answers file ---------------------------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"yes", "true", "y"}:
            return True
        if lowered in {"no", "false", "n"}:
            return False
    return None


def parse_answers(data: Any) -> dict[str, Any]:
    """Typed view of the YAML mapping: every known key present, unusable values become None."""
    source = data if isinstance(data, dict) else {}
    out: dict[str, Any] = {}
    for key in NUMERIC_KEYS:
        out[key] = _number(source.get(key))
    for key in BOOLEAN_KEYS:
        out[key] = _flag(source.get(key))
    for key in TEXT_KEYS:
        value = source.get(key)
        out[key] = str(value).strip() if value not in (None, "") and not isinstance(value, (dict, list)) else None
    # "history of backlogs" may also be a number (count): treat > 0 as true
    raw_history = source.get("history_of_backlogs")
    if out["history_of_backlogs"] is None and _number(raw_history) is not None:
        out["history_of_backlogs"] = _number(raw_history) > 0  # type: ignore[operator]
    return out


def ensure_template(path: Path) -> bool:
    """Create the answers template when missing (never overwrites). Returns True when it was created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return True


def load_answers(path: Path | None = None, *, create_template: bool = False) -> dict[str, Any]:
    target = Path(path) if path is not None else Path(ANSWERS_FILE)
    if create_template:
        ensure_template(target)
    if not target.is_file():
        return parse_answers(None)
    try:
        return parse_answers(yaml.safe_load(target.read_text(encoding="utf-8")))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return parse_answers(None)


# --- text normalisation -------------------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACES = re.compile(r"\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+|\s[•\-–*]\s")


def clean(text: str, limit: int = MAX_QUESTION_CHARS) -> str:
    return _SPACES.sub(" ", _CONTROL.sub(" ", str(text or "")[:limit])).strip()


def _norm(text: str) -> str:
    lowered = text.lower()
    for source, target in (("％", "%"), ("≥", ">="), ("–", "-"), ("—", "-"), ("’", "'")):
        lowered = lowered.replace(source, target)
    return lowered


# --- patterns -----------------------------------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:\.\d{1,2})?"
_PERCENT = re.compile(rf"(?<![\d.])({_NUM})\s*(?:%|per\s?cent\b|percent\b)")
_PERCENT_WORD = re.compile(
    rf"(?:percentage|aggregate|marks)\W{{0,12}}(?:of\s+|at\s+least\s+|minimum\s+|min\.?\s+|above\s+)?({_NUM})(?![\d%.])")
_CGPA_AFTER = re.compile(
    r"\b(?:cgpa|sgpa|gpa|cpi|cgp)\b[^\d]{0,40}?(\d{1,2}(?:\.\d{1,2})?)(?!\d)(?:\s*/\s*(\d{1,2}(?:\.\d{1,2})?))?")
_CGPA_BEFORE = re.compile(
    r"(?<![\d.])(\d{1,2}(?:\.\d{1,2})?)(?!\d)(?:\s*/\s*(\d{1,2}(?:\.\d{1,2})?))?\s*\+?\s*"
    r"(?:and\s+above\s+|or\s+above\s+|or\s+more\s+|and\s+more\s+)?(?:cgpa|sgpa|gpa|cpi|cgp)\b")
_THROUGHOUT = re.compile(r"\bthroughout\b|\bin\s+all\s+(?:exams|semesters|academics)|\bconsistent\b|"
                         r"\bacross\s+(?:all\s+)?academics|\bin\s+all\s+(?:the\s+)?(?:examinations|levels)")
_TENTH = re.compile(r"\b(?:10th|tenth|xth|x\s+(?:std|standard|class|grade|marks)|ssc|sslc|"
                    r"matriculation|class\s*(?:10|x)\b|std\.?\s*(?:10|x)\b)")
_TWELFTH = re.compile(r"\b(?:12th|twelfth|xii(?:th)?\b|hsc|class\s*(?:12|xii)\b|std\.?\s*(?:12|xii)\b|"
                      r"intermediate|plus\s*two|10\s*\+\s*2|senior\s+secondary)")
_BACKLOG = re.compile(r"\b(?:backlogs?|arrears?|atkt|year[\s-]?downs?|supplementary\s+exams?|"
                      r"(?:pending|standing|active|dead)\s+papers?)\b")
_NEGATION = re.compile(r"\b(?:no|nil|zero|without|not\s+(?:have|having|be)|free\s+(?:of|from)|absence\s+of|"
                       r"should\s+not|must\s+not|cleared|clear)\b")
_BACKLOG_HISTORY = re.compile(r"\b(?:history|ever|previous|past|prior|dead|cleared|any\s+time)\b")
_GRAD_TRIGGER = re.compile(r"\b(?:batch|class\s+of|pass(?:ing)?[\s-]*out|passout|graduat\w*|year\s+of\s+passing|"
                           r"passing\s+year|completing|final[\s-]year|freshers?)\b")
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_NOTICE = re.compile(
    r"\bnotice(?:\s+period)?\b[^\d]{0,30}?(\d{1,3})\s*(days?|weeks?|months?)|"
    r"(\d{1,3})\s*(days?|weeks?|months?)\s*(?:of\s+)?(?:notice|serving)")
_IMMEDIATE = re.compile(r"\bimmediate(?:ly)?\s+(?:joiners?|join|joining|availab\w+)|\bavailable\s+immediately\b")
_RELOCATION = re.compile(
    r"\brelocat\w*|\bwilling(?:ness)?\s+to\s+(?:move|travel|work\s+from)|\bwork\s+from\s+(?:the\s+)?office\b|"
    r"\bon[\s-]?site\b|\bmust\s+be\s+(?:located|based)\b|\bbased\s+(?:out\s+of|in)\b|\bwork\s+location\b|"
    r"\boffice\s+in\b|\bhybrid\b|\bpreferred\s+location\b|\bcurrent\s+location\b|\blocated\s+in\b")
_BOND = re.compile(
    r"\bbond\b|\bservice\s+(?:agreement|contract|bond)\b|\btraining\s+(?:bond|agreement)\b|"
    r"\bminimum\s+(?:commitment|tenure|service)\b|\bcommit(?:ment)?\s+(?:of|for)\s+\d+\s*(?:years?|months?)\b|"
    r"\block[\s-]?in\b")
_AUTHORIZATION = re.compile(
    r"\bwork(?:ing)?\s+(?:authori[sz]ation|permit|visa|status)\b|\bauthori[sz]ed\s+to\s+work\b|"
    r"\blegally\s+(?:authori[sz]ed|permitted|eligible|entitled)\b|\bright\s+to\s+work\b|\bvisa\b|\bsponsorship\b|"
    r"\bcitizen(?:ship)?\b|\bpermanent\s+resident\b|\bh-?1b\b|\bgreen\s+card\b|\bemployment\s+eligibility\b|"
    r"\bwork\s+eligibility\b|\bstem\s+opt\b")
_SALARY = re.compile(r"\bctc\b|\bsalary\b|\bcompensation\b|\bremuneration\b|\bpay\s+expectation\b|"
                     r"\bexpected\s+(?:pay|package|stipend)\b|\blpa\b|\bstipend\s+expectation\b")
_EXPERIENCE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*\+?\s*(?:(?:-|to)\s*\d{1,2}\s*)?(?:\+\s*)?(?:years?|yrs?)\b")
_EXPERIENCE_CONTEXT = re.compile(r"\bexperience\b|\bexp\b|\bworked\b|\bworking\b|\bprofessional\b|\bin\s+the\s+industry\b")
_DEGREE_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("btech", re.compile(r"\bb\.?\s?tech\b|\bbachelor(?:s|'s)?\s+of\s+technology\b")),
    ("be", re.compile(r"\bb\.?\s?e\b\.?|\bbachelor(?:s|'s)?\s+of\s+engineering\b")),
    ("bsc", re.compile(r"\bb\.?\s?sc\b|\bbachelor(?:s|'s)?\s+of\s+science\b")),
    ("bca", re.compile(r"\bbca\b|\bbachelor(?:s|'s)?\s+of\s+computer\s+applications?\b")),
    ("mtech", re.compile(r"\bm\.?\s?tech\b|\bmaster(?:s|'s)?\s+of\s+technology\b")),
    ("me", re.compile(r"\bm\.?\s?e\b\.?(?=\W|$)|\bmaster(?:s|'s)?\s+of\s+engineering\b")),
    ("mca", re.compile(r"\bmca\b|\bmaster(?:s|'s)?\s+of\s+computer\s+applications?\b")),
    ("msc", re.compile(r"\bm\.?\s?sc\b|\bmaster(?:s|'s)?\s+of\s+science\b")),
    ("mba", re.compile(r"\bmba\b")),
    ("phd", re.compile(r"\bph\.?\s?d\b|\bdoctorate\b")),
)
_ANY_DEGREE = re.compile(r"\b(?:any|all)\s+(?:graduate|degree|discipline|branch(?:es)?|stream)s?\b|\bany\s+bachelor")
_DEGREE_CONTEXT = re.compile(r"\bdegree\b|\bbachelor\w*\b|\bmaster\w*\b|\bgraduat(?:e|ion)\s+in\b|\bbranch(?:es)?\b|"
                             r"\bdiscipline\b|\bstream\b|\bqualification\b|\bcomputer\s+science\b|\bengineering\b")
_BRANCH_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("computer science", ("computer science", "cse", "cs", "computer engineering", "computer science engineering")),
    ("information technology", ("information technology", "it branch", "it", "information science")),
    ("electronics", ("electronics", "ece", "electronics and communication", "electronics & communication", "etc")),
    ("electrical", ("electrical", "eee", "electrical and electronics")),
    ("mechanical", ("mechanical", "mech")),
    ("civil", ("civil",)),
    ("data science", ("data science", "artificial intelligence", "machine learning", "ai & ml", "ai/ml", "aiml")),
)


# --- findings -----------------------------------------------------------------------------------------------------


@dataclass
class Finding:
    kind: str
    risk: str
    your_value: str | float | bool | None
    note: str
    label: str
    thresholds: list[float] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return self.risk == RISK_CONFIRM or self.your_value is None


def _fmt(value: float | None) -> str:
    if value is None:
        return "not set"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _worst(risks: list[str]) -> str:
    if RISK_POSSIBLE in risks:
        return RISK_POSSIBLE
    if RISK_CONFIRM in risks or not risks:
        return RISK_CONFIRM
    return RISK_OK


def _compare_at_least(asked: float, mine: float | None, unit: str, what: str) -> tuple[str, str]:
    """`asked` is a minimum: below it is a possible knock-out."""
    if mine is None:
        return RISK_CONFIRM, f"asks {_fmt(asked)}{unit} {what}; your value is not set in application_answers.yaml"
    if mine < asked:
        return RISK_POSSIBLE, f"possible knock-out: asks {_fmt(asked)}{unit} {what} vs your value {_fmt(mine)}{unit}"
    return RISK_OK, f"asks {_fmt(asked)}{unit} {what}; your value {_fmt(mine)}{unit} meets it (confirm on the form)"


# --- per-kind detectors -------------------------------------------------------------------------------------------


def _percent_values(text: str) -> list[float]:
    values = [float(m.group(1)) for m in _PERCENT.finditer(text)]
    values += [float(m.group(1)) for m in _PERCENT_WORD.finditer(text)]
    return [v for v in values if 0 < v <= 100]


def _academic_marks(text: str, answers: dict[str, Any]) -> list[Finding]:
    has_tenth, has_twelfth = bool(_TENTH.search(text)), bool(_TWELFTH.search(text))
    has_cgpa = bool(_CGPA_AFTER.search(text) or _CGPA_BEFORE.search(text))
    degree_words = bool(re.search(r"\b(?:graduation|graduate|degree|b\.?\s?tech|b\.?\s?e\b|aggregate|"
                                  r"overall|academic|engineering|semester|ug|pg|bachelor)\b", text))
    percents = _percent_values(text)
    out: list[Finding] = []

    # 10th / 12th
    if (has_tenth or has_twelfth) and percents:
        asked = percents[0]
        parts: list[tuple[str, str, float | None]] = []
        if has_tenth:
            parts.append(("10th", "tenth_percent", answers.get("tenth_percent")))
        if has_twelfth:
            parts.append(("12th", "twelfth_percent", answers.get("twelfth_percent")))
        risks, notes, shown = [], [], []
        for label, _key, mine in parts:
            risk, note = _compare_at_least(asked, mine, "%", f"in {label}")
            risks.append(risk)
            notes.append(note)
            shown.append(f"{label} {_fmt(mine)}" + ("%" if mine is not None else ""))
        out.append(Finding("tenth_twelfth", _worst(risks), "; ".join(shown), "; ".join(notes),
                           f"{'/'.join(p[0] for p in parts)} {_fmt(asked)}%", [asked]))
    elif has_tenth or has_twelfth:
        which = "/".join(name for name, present in (("10th", has_tenth), ("12th", has_twelfth)) if present)
        out.append(Finding("tenth_twelfth", RISK_CONFIRM, None,
                           f"asks about your {which} marks; the minimum is not stated in this text",
                           f"{which} marks"))

    # CGPA / percentage for the degree (also "throughout")
    wants_degree_marks = has_cgpa or ((percents and not (has_tenth or has_twelfth)) or
                                      (percents and (degree_words or _THROUGHOUT.search(text))))
    if wants_degree_marks and (has_cgpa or percents):
        risks, notes, thresholds, shown = [], [], [], []
        label_parts: list[str] = []
        throughout = bool(_THROUGHOUT.search(text))
        cgpa_match = _CGPA_AFTER.search(text) or _CGPA_BEFORE.search(text)
        if cgpa_match:
            asked = float(cgpa_match.group(1))
            scale = float(cgpa_match.group(2)) if cgpa_match.group(2) else (4.0 if asked <= 4.5 else 10.0)
            assumed = cgpa_match.group(2) is None and asked <= 4.5
            mine, my_scale = answers.get("cgpa"), answers.get("cgpa_scale")
            if mine is None or my_scale is None:
                risks.append(RISK_CONFIRM)
                missing = "cgpa" if mine is None else "cgpa_scale"
                notes.append(f"asks CGPA {_fmt(asked)}/{_fmt(scale)}; your {missing} is not set in application_answers.yaml")
            else:
                mine_on_scale = mine * scale / my_scale
                risk, note = _compare_at_least(asked, mine_on_scale, "", f"CGPA (out of {_fmt(scale)})")
                if my_scale != scale:
                    note += f" [your {_fmt(mine)}/{_fmt(my_scale)} converted to the {_fmt(scale)} scale]"
                if assumed:
                    note += " [scale not stated: assumed 4.0]"
                risks.append(risk)
                notes.append(note)
                shown.append(f"{_fmt(mine)}/{_fmt(my_scale)}")
            thresholds.append(asked)
            label_parts.append(f"CGPA {_fmt(asked)}")
        if percents and not (has_tenth or has_twelfth) or (percents and degree_words and not cgpa_match):
            asked_pct = percents[0]
            mine_pct = answers.get("percentage_equivalent")
            risk, note = _compare_at_least(asked_pct, mine_pct, "%", "overall")
            risks.append(risk)
            notes.append(note)
            if mine_pct is not None:
                shown.append(f"{_fmt(mine_pct)}%")
            thresholds.append(asked_pct)
            label_parts.append(f"{_fmt(asked_pct)}%" + (" throughout" if throughout else ""))
            if throughout:
                for key, name in (("tenth_percent", "10th"), ("twelfth_percent", "12th")):
                    mine_other = answers.get(key)
                    r2, n2 = _compare_at_least(asked_pct, mine_other, "%", f"throughout (checked against your {name})")
                    risks.append(r2)
                    notes.append(n2)
        if risks:
            out.append(Finding("cgpa_percentage", _worst(risks), "; ".join(dict.fromkeys(shown)) or None,
                               "; ".join(dict.fromkeys(notes)), " / ".join(label_parts), thresholds))
    return out


def _backlogs(text: str, answers: dict[str, Any]) -> Finding | None:
    match = _BACKLOG.search(text)
    if not match:
        return None
    window = text[max(0, match.start() - 60): match.end() + 30]
    wants_none = bool(_NEGATION.search(window))
    history = bool(_BACKLOG_HISTORY.search(window))
    if history:
        mine = answers.get("history_of_backlogs")
        shown: str | float | bool | None = mine
        if mine is None:
            risk, note = RISK_CONFIRM, "asks about your history of backlogs; history_of_backlogs is not set"
        elif wants_none and mine:
            risk, note = RISK_POSSIBLE, "possible knock-out: asks for no backlog history vs your value: you have one"
        elif wants_none:
            risk, note = RISK_OK, "asks for no backlog history; your value says none (confirm on the form)"
        else:
            risk, note = RISK_CONFIRM, "asks about your backlog history; answer it from your own record"
        return Finding("backlogs", risk, shown, note, "no backlog history" if wants_none else "backlog history")
    mine_active = answers.get("active_backlogs")
    if mine_active is None:
        risk, note = RISK_CONFIRM, "asks about active backlogs; active_backlogs is not set in application_answers.yaml"
    elif wants_none and mine_active > 0:
        risk, note = RISK_POSSIBLE, f"possible knock-out: asks for no active backlogs vs your value {_fmt(mine_active)}"
    elif wants_none:
        risk, note = RISK_OK, "asks for no active backlogs; your value 0 meets it (confirm on the form)"
    else:
        risk, note = RISK_CONFIRM, "asks about your backlogs; answer it from your own record"
    return Finding("backlogs", risk, mine_active, note, "no backlogs" if wants_none else "backlogs")


def _graduation_year(text: str, answers: dict[str, Any]) -> Finding | None:
    if not _GRAD_TRIGGER.search(text):
        return None
    years = sorted({int(y) for y in _YEAR.findall(text) if 2020 <= int(y) <= 2035})
    if not years:
        return None
    mine = answers.get("graduation_year")
    label = "batch " + "/".join(str(y) for y in years)
    if mine is None:
        return Finding("graduation_year", RISK_CONFIRM, None,
                       f"names batch {years}; graduation_year is not set in application_answers.yaml", label)
    if int(mine) in years:
        return Finding("graduation_year", RISK_OK, int(mine),
                       f"names batch {years}; your graduation year {int(mine)} is included (confirm on the form)", label)
    return Finding("graduation_year", RISK_POSSIBLE, int(mine),
                   f"possible knock-out: names batch {years} vs your graduation year {int(mine)}", label)


def _notice(text: str, answers: dict[str, Any]) -> Finding | None:
    asked: float | None = None
    match = _NOTICE.search(text)
    if match:
        number = float(match.group(1) or match.group(3))
        unit = (match.group(2) or match.group(4) or "days").lower()
        asked = number * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
    elif _IMMEDIATE.search(text):
        asked = 0.0
    if asked is None:
        return None
    mine = answers.get("notice_period_days")
    label = "immediate joiner" if asked == 0 else f"notice up to {_fmt(asked)} days"
    if mine is None:
        return Finding("notice_period", RISK_CONFIRM, None,
                       f"asks for notice of {_fmt(asked)} days or less; notice_period_days is not set", label, [asked])
    if mine > asked:
        return Finding("notice_period", RISK_POSSIBLE, mine,
                       f"possible knock-out: asks notice of {_fmt(asked)} days or less vs your value {_fmt(mine)}",
                       label, [asked])
    return Finding("notice_period", RISK_OK, mine,
                   f"asks notice of {_fmt(asked)} days or less; your value {_fmt(mine)} meets it (confirm on the form)",
                   label, [asked])


def _experience(text: str, answers: dict[str, Any]) -> Finding | None:
    for match in _EXPERIENCE.finditer(text):
        window = text[max(0, match.start() - 40): match.end() + 40]
        if _EXPERIENCE_CONTEXT.search(window):
            years = float(match.group(1))
            note = f"asks {_fmt(years)}+ years of experience"
            if years == 0:
                note += " (entry level); confirm what you can claim"
            return Finding("min_experience", RISK_CONFIRM, None,
                           note + "; no experience figure is stored, answer from your own record",
                           f"{_fmt(years)}+ years experience", [years])
    return None


def _degree_branch(text: str, answers: dict[str, Any]) -> Finding | None:
    named = [code for code, pattern in _DEGREE_TOKENS if pattern.search(text)]
    if not named and not _DEGREE_CONTEXT.search(text):
        return None
    branches = [name for name, aliases in _BRANCH_ALIASES
                if any(re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text) for alias in aliases if len(alias) > 2)]
    if not named and not branches and not re.search(r"\bbranch(?:es)?\b|\bdiscipline\b|\bstream\b", text):
        return None
    any_ok = bool(_ANY_DEGREE.search(text))
    my_degree, my_branch = answers.get("degree"), answers.get("branch")
    risks, notes = [], []
    label_parts: list[str] = []
    if named:
        label_parts.append("/".join(named))
        if my_degree is None:
            risks.append(RISK_CONFIRM)
            notes.append(f"names degree {named}; degree is not set in application_answers.yaml")
        else:
            mine = re.sub(r"[^a-z]", "", my_degree.lower())
            mine_code = next((code for code, pattern in _DEGREE_TOKENS if pattern.search(my_degree.lower())), mine)
            if mine_code in named or any_ok:
                risks.append(RISK_OK)
                notes.append(f"names degree {named}; your degree {my_degree!r} is among them (confirm on the form)")
            else:
                risks.append(RISK_POSSIBLE)
                notes.append(f"possible knock-out: names degree {named} vs your value {my_degree!r}")
    if branches:
        label_parts.append("/".join(branches))
        if my_branch is None:
            risks.append(RISK_CONFIRM)
            notes.append(f"names branch {branches}; branch is not set in application_answers.yaml")
        else:
            mine_text = my_branch.lower()
            ok = any_ok or any(name in mine_text or any(a in mine_text for a in aliases if len(a) > 3)
                               for name, aliases in _BRANCH_ALIASES if name in branches)
            risks.append(RISK_OK if ok else RISK_POSSIBLE)
            notes.append(f"names branch {branches}; your branch {my_branch!r} " + (
                "is among them (confirm on the form)" if ok else "is not named: possible knock-out"))
    if not risks:
        risks.append(RISK_CONFIRM)
        notes.append("asks about degree or branch; the accepted list is not stated in this text")
        label_parts.append("degree/branch")
    shown = ", ".join(v for v in (my_degree, my_branch) if v) or None
    return Finding("degree_branch", _worst(risks), shown, "; ".join(notes), " ".join(label_parts))


def _bond(text: str, answers: dict[str, Any]) -> Finding | None:
    if not _BOND.search(text):
        return None
    mine = answers.get("bond_acceptable")
    if mine is None:
        return Finding("bond", RISK_CONFIRM, None,
                       "mentions a bond or service agreement; bond_acceptable is not set", "service bond")
    if mine is False:
        return Finding("bond", RISK_POSSIBLE, False,
                       "possible knock-out: mentions a bond or service agreement vs your value: not acceptable",
                       "service bond")
    return Finding("bond", RISK_OK, True, "mentions a bond or service agreement; you marked a bond acceptable "
                   "(read the terms before agreeing)", "service bond")


def _relocation(text: str, answers: dict[str, Any]) -> Finding | None:
    match = _RELOCATION.search(text)
    if not match:
        return None
    mine = answers.get("willing_to_relocate")
    explicit = bool(re.search(r"\brelocat\w*|\bwilling(?:ness)?\s+to\s+(?:move|travel)", text))
    if mine is None:
        return Finding("relocation_location", RISK_CONFIRM, None,
                       "asks about relocation or work location; willing_to_relocate is not set. Answer it yourself",
                       "relocation / location")
    if mine is False and explicit:
        return Finding("relocation_location", RISK_POSSIBLE, False,
                       "possible knock-out: asks about relocation vs your value: not willing. Answer it yourself",
                       "relocation / location")
    return Finding("relocation_location", RISK_CONFIRM, mine,
                   "asks about relocation or work location; your stored value is shown, answer it yourself",
                   "relocation / location")


def _authorization(text: str, answers: dict[str, Any]) -> Finding | None:
    if not _AUTHORIZATION.search(text):
        return None
    shown = answers.get("work_authorization_india")
    sponsor = answers.get("needs_sponsorship_elsewhere")
    detail = []
    if shown:
        detail.append(f"work authorization (India): {shown}")
    if sponsor is not None:
        detail.append(f"needs sponsorship elsewhere: {'yes' if sponsor else 'no'}")
    return Finding("work_authorization", RISK_CONFIRM, "; ".join(detail) or None,
                   "work authorization / visa question: never drafted. Answer it yourself from your own status",
                   "work authorization")


def _salary(text: str, answers: dict[str, Any]) -> Finding | None:
    if not _SALARY.search(text):
        return None
    policy = answers.get("expected_ctc_policy")
    return Finding("salary_ctc", RISK_CONFIRM, policy,
                   "salary / CTC question: never drafted. Answer it yourself" +
                   ("" if policy else "; expected_ctc_policy is not set"), "salary / CTC")


# --- public API ---------------------------------------------------------------------------------------------------


def scan_text(text: str, answers: dict[str, Any]) -> list[Finding]:
    """Every screen detected in one question or sentence (at most one finding per kind)."""
    normalized = _norm(clean(text))
    if not normalized:
        return []
    findings: list[Finding] = []
    findings += _academic_marks(normalized, answers)
    for detector in (_backlogs, _graduation_year, _notice, _bond, _relocation, _authorization, _salary,
                     _experience, _degree_branch):
        found = detector(normalized, answers)
        if found:
            findings.append(found)
    return findings


def scan_questions(questions: list[str], answers: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per (question, kind). Questions are capped in count and length; nothing is executed."""
    out: list[dict[str, Any]] = []
    for question in questions[:MAX_QUESTIONS]:
        shown = clean(question)
        for finding in scan_text(shown, answers):
            out.append({
                "question": shown, "kind": finding.kind, "risk": finding.risk,
                "your_value": finding.your_value, "needs_confirmation": finding.needs_confirmation,
                "note": finding.note,
            })
    return out


def eligibility_screens(posting_text: str | None, answers: dict[str, Any]) -> list[dict[str, Any]]:
    """Screens named in a posting: `{kind, label, risk}`. Risk is None when the answers file has no value."""
    if not posting_text:
        return []
    sentences = [s for s in _SENTENCE_SPLIT.split(_norm(clean(posting_text, MAX_POSTING_CHARS)))
                 if s and len(s) <= 400]
    found: dict[str, dict[str, Any]] = {}
    for sentence in sentences:
        for finding in scan_text(sentence, answers):
            if finding.kind not in SCREEN_KINDS or finding.label in found:
                continue
            found[finding.label] = {
                "kind": finding.kind, "label": finding.label,
                "risk": None if finding.your_value is None and finding.risk == RISK_CONFIRM else finding.risk,
            }
    return list(found.values())[:8]
