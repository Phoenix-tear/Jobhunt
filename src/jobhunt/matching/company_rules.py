"""Company grade rules: employers (banks mostly) that hire by grade, written by me in `data/profile/company_rules.yaml`.

Semantics (the gate applies them after every other rule):

* a **deny** match -- a deny title pattern in the title, a deny level code in the title or the text, or an optional
  deny text pattern in the text -- turns the job `excluded` (rule `company_grade_rule`, the entry's note is shown);
* a company that has **allow** rules and a job where none of them match (the title, a level code, or the role words at
  the top of the description) is at best `maybe` (reviewable, never silently hidden);
* an **allow** match leaves the job unaffected.

The file is git-ignored and only ever edited by me, or by one explicit click in the UI ("Remember for this company",
"Remove") that first shows the exact text. The loader itself never writes. Entries of the same company are merged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

COMPANY_RULES_FILE = "data/profile/company_rules.yaml"
ROLE_WORDS_HEAD_CHARS = 300          # "the JD's role words": allow patterns are also looked for in the top of the description
MAX_PATTERN_CHARS = 80

TEMPLATE = '''\
# Company grade rules (git-ignored, yours). Employers that hire by grade (banks etc.): which grades are open to me.
#
# Each entry:
#   company:              name as it appears on the job (case-insensitive; "Barclays" also matches "Barclays Bank PLC")
#   aliases:              other spellings of the company            (optional)
#   allow_title_patterns: titles / role words that ARE open to me   (whole words, case-insensitive)
#   deny_title_patterns:  titles that can never be relevant -> the job is excluded (reason company_grade_rule)
#   allow_level_codes:    grade codes that are open, written without separators: BA4 also matches "BA-4" and "BA 4"
#   deny_level_codes:     grade codes that are not open -> excluded when found in the title or the posting text
#   deny_text_patterns:   phrases in the posting text that mark a closed grade -> excluded      (optional, empty by default)
#   note:                 shown next to a hidden job
#
# A company with allow rules: a job matching none of them (title, level code, or role words at the top of the posting)
# is at best "maybe" -- reviewable, never silently hidden. A deny match always wins.
# Editing this file takes effect at the next `relevance-run`. Never edited by the program without your click.

- company: Barclays
  allow_level_codes: [BA4]
  allow_title_patterns: [Analyst, Technology Developer]
  deny_title_patterns: [Assistant Vice President, Vice President, AVP, VP, Director]
  deny_level_codes: [BA3, BA2, BA1]
  note: "Only the BA-4 grade is open to me (user rule)"
'''


@dataclass(frozen=True)
class CompanyRule:
    company: str
    aliases: tuple[str, ...] = ()
    allow_title_patterns: tuple[str, ...] = ()
    deny_title_patterns: tuple[str, ...] = ()
    allow_level_codes: tuple[str, ...] = ()
    deny_level_codes: tuple[str, ...] = ()
    deny_text_patterns: tuple[str, ...] = ()
    note: str = ""
    index: int = 0                     # position of the (first) entry in the file, used by Remove
    entry_indexes: tuple[int, ...] = field(default_factory=tuple)

    @property
    def has_allow_rules(self) -> bool:
        return bool(self.allow_title_patterns or self.allow_level_codes)


@dataclass(frozen=True)
class CompanyVerdict:
    """What a rule says about one job: `deny` (excluded), `unverified` (allow rules exist, none matched), or `allowed`."""

    outcome: str                       # deny | unverified | allowed | none
    reason: str = ""
    matched: str = ""
    note: str = ""
    company: str = ""


# --- matching helpers ---------------------------------------------------------------------------------------------

_COMPANY_SUFFIX = re.compile(r"\b(?:plc|ltd|limited|inc|llc|corp|corporation|co|gmbh|ag|sa|group|holdings?)\b\.?")


def normalize_company(name: str | None) -> str:
    text = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", _COMPANY_SUFFIX.sub(" ", text)).strip()


def _normalize_phrase(value: str) -> str:
    return re.sub(r"[\s_\-–—/,.:;|()\[\]{}]+", " ", value.lower()).strip()


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    words = _normalize_phrase(phrase).split()
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(re.escape(word) for word in words) + r"(?![a-z0-9])")


def _code_regex(code: str) -> re.Pattern[str] | None:
    """BA4 -> BA4 / BA-4 / BA 4 / ba_4 (letters, then digits)."""
    match = re.fullmatch(r"\s*([A-Za-z]{1,6})[\s\-_.]*(\d{1,3})\s*", code)
    if not match:
        return None
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(match.group(1))}[\s\-_.]?{match.group(2)}(?![A-Za-z0-9])",
                      re.IGNORECASE)


def company_matches(rule: CompanyRule, company_name: str | None) -> bool:
    target = normalize_company(company_name)
    if not target:
        return False
    for name in (rule.company, *rule.aliases):
        candidate = normalize_company(name)
        if candidate and (target == candidate or target.startswith(candidate + " ")):
            return True
    return False


class _Compiled:
    def __init__(self, rule: CompanyRule) -> None:
        self.rule = rule
        self.allow_titles = [(p, _phrase_regex(p)) for p in rule.allow_title_patterns if _normalize_phrase(p)]
        self.deny_titles = [(p, _phrase_regex(p)) for p in rule.deny_title_patterns if _normalize_phrase(p)]
        self.deny_text = [(p, _phrase_regex(p)) for p in rule.deny_text_patterns if _normalize_phrase(p)]
        self.allow_codes = [(c, rx) for c in rule.allow_level_codes if (rx := _code_regex(c))]
        self.deny_codes = [(c, rx) for c in rule.deny_level_codes if (rx := _code_regex(c))]


class CompanyRuleSet:
    """Compiled rules; `judge` answers for one job."""

    def __init__(self, rules: Sequence[CompanyRule] = ()) -> None:
        self.rules = tuple(rules)
        self._compiled = [_Compiled(rule) for rule in self.rules]

    def __bool__(self) -> bool:
        return bool(self._compiled)

    def judge(self, company_name: str | None, title: str, description: str | None) -> CompanyVerdict:
        norm_title = _normalize_phrase(title)
        text = description or ""
        norm_text = _normalize_phrase(text) if (text and any(c.deny_text for c in self._compiled)) else ""
        head = _normalize_phrase(text[:ROLE_WORDS_HEAD_CHARS])
        for compiled in self._compiled:
            rule = compiled.rule
            if not company_matches(rule, company_name):
                continue
            for pattern, regex in compiled.deny_titles:
                if regex.search(norm_title):
                    return CompanyVerdict("deny", f"title says '{pattern}'", pattern, rule.note, rule.company)
            for code, regex in compiled.deny_codes:
                if regex.search(title) or regex.search(text):
                    return CompanyVerdict("deny", f"grade code {code} in the posting", code, rule.note, rule.company)
            if norm_text:
                for pattern, regex in compiled.deny_text:
                    if regex.search(norm_text):
                        return CompanyVerdict("deny", f"posting says '{pattern}'", pattern, rule.note, rule.company)
            if not rule.has_allow_rules:
                return CompanyVerdict("allowed", note=rule.note, company=rule.company)
            for pattern, regex in compiled.allow_titles:
                if regex.search(norm_title) or regex.search(head):
                    return CompanyVerdict("allowed", f"title / role says '{pattern}'", pattern, rule.note, rule.company)
            for code, regex in compiled.allow_codes:
                if regex.search(title) or regex.search(text):
                    return CompanyVerdict("allowed", f"grade code {code}", code, rule.note, rule.company)
            allowed = ", ".join([*rule.allow_title_patterns, *rule.allow_level_codes])
            return CompanyVerdict("unverified", f"none of the open roles / grades ({allowed}) is named", "",
                                  rule.note, rule.company)
        return CompanyVerdict("none")


# --- loading ------------------------------------------------------------------------------------------------------


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable) or isinstance(value, (dict, bytes)):
        return ()
    return tuple(str(item).strip()[:MAX_PATTERN_CHARS] for item in value if str(item).strip())


def parse_company_rules(text: str) -> list[CompanyRule]:
    """Entries of the YAML text, merged per company. Malformed entries are skipped (logged), never fatal."""
    try:
        data = yaml.safe_load(text) or []
    except yaml.YAMLError as error:
        logger.warning("company_rules.yaml is not valid YAML, ignoring it: %s", error)
        return []
    entries = data.get("companies", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for position, raw in enumerate(entries):
        if not isinstance(raw, dict) or not str(raw.get("company", "")).strip():
            continue
        name = str(raw["company"]).strip()
        key = normalize_company(name) or name.lower()
        if key not in merged:
            merged[key] = {"company": name, "aliases": [], "allow_title_patterns": [], "deny_title_patterns": [],
                           "allow_level_codes": [], "deny_level_codes": [], "deny_text_patterns": [], "notes": [],
                           "index": position, "entry_indexes": []}
            order.append(key)
        slot = merged[key]
        slot["entry_indexes"].append(position)
        for field_name in ("aliases", "allow_title_patterns", "deny_title_patterns", "allow_level_codes",
                           "deny_level_codes", "deny_text_patterns"):
            for item in _strings(raw.get(field_name)):
                if item not in slot[field_name]:
                    slot[field_name].append(item)
        note = str(raw.get("note", "")).strip()[:200]
        if note and note not in slot["notes"]:
            slot["notes"].append(note)
    rules = []
    for key in order:
        slot = merged[key]
        rules.append(CompanyRule(
            company=slot["company"], aliases=tuple(slot["aliases"]),
            allow_title_patterns=tuple(slot["allow_title_patterns"]), deny_title_patterns=tuple(slot["deny_title_patterns"]),
            allow_level_codes=tuple(slot["allow_level_codes"]), deny_level_codes=tuple(slot["deny_level_codes"]),
            deny_text_patterns=tuple(slot["deny_text_patterns"]), note="; ".join(slot["notes"]),
            index=slot["index"], entry_indexes=tuple(slot["entry_indexes"]),
        ))
    return rules


def load_company_rules(path: Path) -> list[CompanyRule]:
    if not path.is_file():
        return []
    try:
        return parse_company_rules(path.read_text(encoding="utf-8"))
    except OSError as error:
        logger.warning("cannot read %s: %s", path, error)
        return []


def ensure_template(path: Path) -> bool:
    """Create the commented template (with the Barclays rule) when the file is missing. Never overwrites."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return True


def signature(rules: Sequence[CompanyRule]) -> str:
    """Stable fingerprint of the rules (part of the gate's rules hash, so an edit recomputes every verdict)."""
    if not rules:
        return ""
    payload = repr([(r.company, r.aliases, r.allow_title_patterns, r.deny_title_patterns, r.allow_level_codes,
                     r.deny_level_codes, r.deny_text_patterns, r.note) for r in rules])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# --- the two edits the UI may make (each only after an explicit click that was shown the exact text) -------------------

LEVEL_WORDS = (       # longest first: the word to offer in "Remember for this company"
    "assistant vice president", "associate vice president", "senior vice president", "executive vice president",
    "vice president", "managing director", "associate director", "director", "head of", "principal", "staff",
    "avp", "svp", "evp", "vp", "lead", "manager", "senior", "sr",
)


def level_word_in_title(title: str) -> str | None:
    """The level word to remember for a company ('Vice President' from 'Vice President, Technology'), or None."""
    normalized = _normalize_phrase(title)
    for word in LEVEL_WORDS:
        match = _phrase_regex(word).search(normalized)
        if match:
            raw = re.search(r"(?i)" + r"[\s\-]+".join(re.escape(part) for part in word.split()), title)
            shown = raw.group(0) if raw else match.group(0)
            return shown.upper() if len(word) <= 3 else shown.title()
    return None


def render_deny_entry(company: str, pattern: str, *, today: date) -> str:
    """The exact text appended to the file for 'Remember for this company'."""
    safe_company = company.replace('"', "'")
    safe_pattern = pattern.replace('"', "'")
    return (f'\n- company: "{safe_company}"\n'
            f'  deny_title_patterns: ["{safe_pattern}"]\n'
            f'  note: "Added from Not applying (ineligible_level) on {today.isoformat()}"\n')


def already_denied(rules: Sequence[CompanyRule], company: str, pattern: str) -> bool:
    wanted = _normalize_phrase(pattern)
    return any(company_matches(rule, company) and wanted in {_normalize_phrase(p) for p in rule.deny_title_patterns}
               for rule in rules)


def append_deny_entry(path: Path, entry_text: str) -> None:
    ensure_template(path)
    existing = path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + entry_text, encoding="utf-8")


def _last_line(node: yaml.Node) -> int:
    """Last line (0-based) that holds content of `node` (comments after it do not count)."""
    if isinstance(node, yaml.ScalarNode):
        end = node.end_mark
        return end.line - 1 if end.column == 0 and end.line > node.start_mark.line else end.line
    children = node.value if isinstance(node, yaml.SequenceNode) else [part for pair in node.value for part in pair]
    return max((_last_line(child) for child in children), default=node.start_mark.line)


def remove_entry(path: Path, entry_index: int) -> bool:
    """Delete one list entry (its own lines only; comments and other entries stay). False when the index is unknown."""
    text = path.read_text(encoding="utf-8")
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return False
    sequence = root
    if isinstance(root, yaml.MappingNode):
        sequence = next((value for key, value in root.value if getattr(key, "value", None) == "companies"), None)
    if not isinstance(sequence, yaml.SequenceNode) or not 0 <= entry_index < len(sequence.value):
        return False
    node = sequence.value[entry_index]
    lines = text.splitlines(keepends=True)
    del lines[node.start_mark.line: _last_line(node) + 1]
    path.write_text("".join(lines), encoding="utf-8")
    return True
