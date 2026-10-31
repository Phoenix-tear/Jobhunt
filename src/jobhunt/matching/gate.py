"""The relevance gate: cheap, deterministic, explainable, reversible.

Every open job gets exactly one verdict (`target` | `maybe` | `excluded`) with a machine rule code
and a human sentence. Nothing is deleted; editing `config.yaml -> relevance` changes the rules
hash, which recomputes every verdict on the next `relevance-run` (the same retroactive behaviour
as the ingestion prefilter). The verdict is a pure function of (job fields, rules, today), so the
"current date" is injected and tests move it with a fake clock.

Spring Boot analogy: `RelevanceGate.evaluate` is a pure `@Service` method (a rules engine such as
Drools would play this role); `run_relevance` is the `@Transactional` batch job that applies it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from jobhunt.classify import compile_keywords, normalize_for_matching
from jobhunt.config import Config, RelevanceSettings, TriageSettings
from jobhunt.matching.eligibility import detect_eligibility, likely_ineligible_note, required_flags
from jobhunt.matching.company_rules import (
    COMPANY_RULES_FILE, CompanyRule, CompanyRuleSet, load_company_rules,
    signature as company_signature,
)
from jobhunt.matching.learned_rules import LearnedRule
from jobhunt.matching.triage import Verdict, build_evidence, effect_of
from jobhunt.matching.triage_apply import YEARS_PENALTY, usable_and_exempt, verdict_key
from jobhunt.matching.triage_student import Opinion, load_student, student_key
from jobhunt.matching.seniority import CAUTION, EXCLUDED_LEVELS, classify_seniority, compile_level_code_rules
from jobhunt.matching.years import required_years
from jobhunt.timeutil import Clock, to_iso, utc_now

GATE_VERSION = 7

TARGET, MAYBE, EXCLUDED = "target", "maybe", "excluded"
# strong junior markers of the seniority classifier: the years rule does not apply to these titles
_EARLY_CAREER_RULES = frozenset({"intern", "graduate / new grad", "trainee / apprentice", "early careers / campus"})
# exclusions a company grade rule may relabel (the grade rule explains the same fact better, with my note)
_SENIORITY_RULES = frozenset({"seniority_title", "seniority_level", "required_years"})
# verdict blocker -> eligibility flag kind (matching/eligibility.py)
_BLOCKER_FLAG = {"phd": "phd_required", "masters": "masters_required", "security_clearance": "us_citizen_or_clearance_required",
                 "citizenship_required": "us_citizen_or_clearance_required", "us_work_authorization": "us_work_authorization"}

_DESCRIPTION_HEAD_FOR_TERMS = 1500
_SOFT_CONTEXT_CHARS = 60
# A language named inside a list of alternatives is not "the" stack: "such as C#, Java or C++",
# "one or more of the following", "opportunities could include".
_LIST_CONTEXT_CHARS = 90
_LIST_CUES = re.compile(
    r"(?:such as|e g|eg|for example|one or more of|any of the following|one of the following|could include|"
    r"may include|might include|or similar|or equivalent|including but not limited to|a range of|variety of)",
)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")  # kana, Han, Hangul
# Pooled postings ("Talent Pool", "future opportunities") describe many possible roles at once.
_POOLED_POSTING = re.compile(r"talent (?:pool|community|network)|future opportunit|register your interest|"
                             r"expressions? of interest|general application|all grades")


# --- data -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateJob:
    """The slice of a job the gate looks at."""

    job_id: int
    title: str
    company_name: str = ""
    location_raw: str | None = None
    experience_level: str = "unknown"
    employment_type: str = "unknown"
    graduation_year_hint: int | None = None
    description: str | None = None
    degrees: tuple[str, ...] = ()           # degree list of the source payload (Simplify), if any
    triage: Verdict | None = None           # verified LLM verdict that may act on this job (None: protected, exempt or none)
    student: Opinion | None = None          # rules / classifier mined from verdicts (only for jobs without a usable verdict)

    @property
    def has_description(self) -> bool:
        return bool(self.description and self.description.strip())


@dataclass(frozen=True)
class GateDecision:
    status: str                      # target | maybe | excluded
    rule: str                        # machine code, stable (grouped by the UI and suggest-rules)
    reason: str                      # human sentence, shown next to a hidden job
    role_family: str = "other"       # ai_ml | fullstack | other
    family_detail: str | None = None
    rank_adjust: float = 0.0
    confidence: str = "high"         # high | medium | low (low: judged without a description)
    required_years: int | None = None
    seniority_level: str = "unspecified"   # junior | unspecified | mid | senior | executive
    seniority_evidence: str | None = None
    eligibility_json: str = ""              # [{kind, strength, snippet}] (matching/eligibility.py)
    learned_json: str = ""                  # learned rules that applied
    learned_penalty: float = 0.0
    triage_json: str = ""                   # student chips: probabilities, matched rules, what demoted the job


# --- phrase sets ------------------------------------------------------------------------------


class PhraseSet:
    """Whole-word phrase list on normalized text; `find` returns the phrase that matched."""

    def __init__(self, phrases: Iterable[str]) -> None:
        self._pattern = compile_keywords(phrases)

    def find(self, normalized_text: str) -> str | None:
        if not self._pattern or not normalized_text:
            return None
        match = self._pattern.search(normalized_text)
        return match.group(0) if match else None

    def find_all(self, normalized_text: str) -> list[re.Match[str]]:
        if not self._pattern or not normalized_text:
            return []
        return list(self._pattern.finditer(normalized_text))

    def __bool__(self) -> bool:
        return self._pattern is not None


# --- graduation year and internship terms -----------------------------------------------------

_GRAD_TRIGGERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"class\s+of\s+(20\d{2})",
        r"\b(?:graduat\w*|grads?)\b\D{0,40}?\b(20\d{2})\b",
        r"\b(20\d{2})\s+(?:new\s+)?(?:grads?|graduat\w*|cohort|intake|batch|start(?:s|ing)?)\b",
        r"\b(?:batch|cohort|intake)\s+(?:of\s+)?(20\d{2})\b",
        r"\b(20\d{2})\s+pass[\s-]*outs?\b",
        r"\bpass[\s-]*out\s+(?:year\s+)?(?:of\s+)?(20\d{2})\b",
    )
)
_YEAR = re.compile(r"\b(20\d{2})\b")
_MIN_GRAD_YEAR, _MAX_GRAD_YEAR = 2024, 2032


def graduation_years(*texts: str | None) -> set[int]:
    """Every class / start year the text names ("class of 2026 and 2027" -> {2026, 2027})."""
    years: set[int] = set()
    for text in texts:
        if not text:
            continue
        for pattern in _GRAD_TRIGGERS:
            for match in pattern.finditer(text):
                window = text[match.start(): match.end() + 40]
                for year_match in _YEAR.finditer(window):
                    year = int(year_match.group(1))
                    if _MIN_GRAD_YEAR <= year <= _MAX_GRAD_YEAR:
                        years.add(year)
    return years


_TERM_AFTER = re.compile(
    r"\b(?P<season>summer|fall|autumn|winter|spring)\s*(?:of\s+)?(?:internships?\s+)?['’]?\s*(?P<year>20\d{2}|\d{2})"
    r"(?:\s*[/-]\s*(?:20)?(?P<year2>\d{2}))?\b",
    re.IGNORECASE,
)
_TERM_BEFORE = re.compile(r"\b(?P<year>20\d{2})\s+(?P<season>summer|fall|autumn|winter|spring)\b", re.IGNORECASE)
_YEAR_ONLY_INTERN = re.compile(r"\b(?P<year>20\d{2})\s+(?:\w+\s+)?intern(?:ship)?s?\b", re.IGNORECASE)


def _season_end(season: str, year: int) -> date:
    season = season.lower()
    if season == "summer":
        return date(year, 8, 31)
    if season in {"fall", "autumn"}:
        return date(year, 12, 31)
    if season == "winter":      # a "Winter 2026" posting that is still open means Dec 2026 - Feb 2027
        return date(year + 1, 2, 28)
    return date(year, 5, 31)    # spring


def internship_term_ends(*texts: str | None) -> list[date]:
    """End dates of every internship term named ("Summer 2026", "2027 Winter", "2026 internship")."""
    ends: list[date] = []
    for text in texts:
        if not text:
            continue
        for match in _TERM_AFTER.finditer(text):
            year = int(match.group("year"))
            year = year + 2000 if year < 100 else year
            ends.append(_season_end(match.group("season"), year))
        for match in _TERM_BEFORE.finditer(text):
            ends.append(_season_end(match.group("season"), int(match.group("year"))))
        if not ends:   # "2026 internship": only when no season was named ("Summer 2026 Intern" is a season)
            for match in _YEAR_ONLY_INTERN.finditer(text):
                ends.append(date(int(match.group("year")), 12, 31))
    return ends


# --- the gate ---------------------------------------------------------------------------------


class RelevanceGate:
    """Compile the rules once, judge many jobs."""

    def __init__(self, settings: RelevanceSettings, learned_rules: Sequence[LearnedRule] = (),
                 company_rules: Sequence[CompanyRule] = (), triage: TriageSettings | None = None,
                 profile: Any | None = None) -> None:
        from jobhunt.profile import get_profile

        self.profile = profile or get_profile()
        self.settings = settings
        self.triage_settings = triage or TriageSettings()
        self.learned_rules = tuple(learned_rules)
        self.company_rules = CompanyRuleSet(company_rules)
        titles = settings.titles
        self._exclude = PhraseSet(titles.exclude)
        self._level_codes = compile_level_code_rules(settings.seniority.level_code_rules)
        self._ai_ml = PhraseSet(titles.ai_ml)
        self._ai_bare = PhraseSet(titles.ai_ml_bare)
        self._role_nouns = PhraseSet(titles.role_nouns)
        self._fullstack = PhraseSet(titles.fullstack)
        self._fullstack_weak = PhraseSet(titles.fullstack_weak)
        self._maybe = PhraseSet(titles.maybe)
        self._generic = PhraseSet(titles.generic)
        self._tech_signals = PhraseSet(titles.tech_signals)
        self._ai_boost = PhraseSet(titles.ai_boost)
        self._lower = {name: PhraseSet(phrases) for name, phrases in titles.lower_priority.items()}
        self._off_domain = {name: PhraseSet(phrases) for name, phrases in titles.off_domain.items()}
        self._english_role_words = (self._ai_ml, self._role_nouns, self._fullstack, self._fullstack_weak)
        self._stacks = {name: PhraseSet(words) for name, words in settings.excluded_stacks.items()}
        self._allowed = PhraseSet(settings.allowed_stacks)
        markers = sorted({normalize_for_matching(marker) for marker in settings.soft_markers if marker.strip()},
                         key=len, reverse=True)
        self._soft = re.compile(
            "(?<![a-z0-9])(?:" + "|".join(re.escape(marker).replace(r"\ ", r"\s+") for marker in markers) + ")(?![a-z0-9])"
            if markers else r"(?!x)x"
        )

    # public ------------------------------------------------------------------------------------

    def evaluate(self, job: GateJob, today: date) -> GateDecision:
        verdict = classify_seniority(
            job.title, description_head=(job.description or "")[:600], level_code_rules=self._level_codes,
        )
        if self.settings.seniority.mode == "junior_only" and verdict.level in EXCLUDED_LEVELS \
                and job.employment_type != "internship":
            decision = self._excluded(
                "seniority_title",
                f"Title reads {verdict.level} ({verdict.evidence}); only junior roles are relevant.",
                "high" if job.has_description else "medium",
            )
        else:
            decision = self._evaluate(job, today, early_career=verdict.rule in _EARLY_CAREER_RULES)
        if verdict.level == CAUTION and decision.status == TARGET:
            decision = _replace(decision, status=MAYBE, rule="seniority_caution",
                                reason=f"{decision.reason} Title may be above entry level ({verdict.evidence}).")
        decision = _replace(decision, seniority_level=verdict.level, seniority_evidence=verdict.evidence[:200])
        decision = self._apply_eligibility_and_learned(job, decision)
        decision = self._apply_company_rules(job, decision)
        decision = self._apply_triage(job, decision)
        return self._apply_student(job, decision)

    def _apply_student(self, job: GateJob, decision: GateDecision) -> GateDecision:
        """Mined rules and the classifier act only where no LLM verdict does. A senior rule excludes; the classifier only
        turns a target into a maybe and lowers its rank. Location rules act in the location step."""
        opinion = job.student
        if opinion is None or decision.status == EXCLUDED:
            return decision
        senior_rule = next((rule for rule in opinion.rules if rule.target == "senior"), None)
        if senior_rule is not None:
            return _replace(decision, status=EXCLUDED, rule="student_rule", triage_json=json.dumps(opinion.as_json("student_rule")),
                            reason=f"Mined rule ({senior_rule.support} of my labelled jobs, {senior_rule.precision:.0%} precise): "
                                   f"{senior_rule.text}"[:400])
        demoted = None
        if opinion.senior_demote or opinion.india_demote:
            demoted = "student_classifier"
            what = "senior" if opinion.senior_demote else "not open to India"
            probability = opinion.p_senior if opinion.senior_demote else opinion.p_not_india
            decision = _replace(decision, rank_adjust=decision.rank_adjust + YEARS_PENALTY)
            if decision.status == TARGET:
                decision = _replace(decision, status=MAYBE, rule="student_classifier",
                                    reason=f"Local classifier: {probability:.0%} likely {what} (precision above 90% on held-out jobs)."[:400])
        if demoted or opinion.rules:
            decision = _replace(decision, triage_json=json.dumps(opinion.as_json(demoted)))
        return decision

    def _apply_triage(self, job: GateJob, decision: GateDecision) -> GateDecision:
        """The LLM verdict as an overlay: a verified senior / lead level or required years at the limit excludes (with the
        quote as the reason); a weaker signal only caps a target at maybe and lowers its rank. It never promotes."""
        if job.triage is None or decision.status == EXCLUDED:
            return decision
        effect = effect_of(job.triage, self.triage_settings)
        if effect.seniority == "exclude":
            return _replace(decision, status=EXCLUDED, rule="llm_seniority",
                            reason=f"LLM read the posting as {effect.seniority_rule}: \u201c{effect.seniority_quote}\u201d"[:400])
        if effect.seniority == "maybe":
            note = f"LLM read the posting as {effect.seniority_rule}: \u201c{effect.seniority_quote}\u201d"
            decision = _replace(decision, rank_adjust=decision.rank_adjust + YEARS_PENALTY)
            if decision.status == TARGET:
                decision = _replace(decision, status=MAYBE, rule="llm_seniority_caution", reason=note[:400])
            else:
                decision = _replace(decision, reason=f"{decision.reason} {note}"[:400])
        return decision

    def _apply_company_rules(self, job: GateJob, decision: GateDecision) -> GateDecision:
        """My grade rules for an employer (data/profile/company_rules.yaml): deny excludes, unmatched allow rules cap at maybe."""
        if not self.company_rules:
            return decision
        verdict = self.company_rules.judge(job.company_name, job.title, job.description)
        if verdict.outcome == "deny" and (decision.status != EXCLUDED or decision.rule in _SENIORITY_RULES):
            note = f" {verdict.note}." if verdict.note else ""
            return _replace(decision, status=EXCLUDED, rule="company_grade_rule",
                            reason=f"Company grade rule for {verdict.company}: {verdict.reason}.{note}"[:400])
        if verdict.outcome == "unverified" and decision.status == TARGET:
            note = f" {verdict.note}." if verdict.note else ""
            return _replace(decision, status=MAYBE, rule="company_grade_unverified",
                            reason=f"Company grade rule for {verdict.company}: {verdict.reason}.{note}"[:400])
        return decision

    def _apply_eligibility_and_learned(self, job: GateJob, decision: GateDecision) -> GateDecision:
        """Eligibility flags and approved learned rules: reversible notes that can only lower a verdict.

        A `required` flag turns a `target` into `maybe` (never promotes, never excludes); learned rules do the same
        and may subtract score points. Excluded jobs are left alone."""
        flags = detect_eligibility(
            job.title, job.description, job.degrees or None,
            degree_list_strength="required" if self.settings.eligibility_source_lists_required else "preferred")
        if job.triage is not None and job.triage.eligibility_blockers:
            seen = {flag["kind"] for flag in flags}
            allowed_blockers = self.profile.scoring.eligibility_blockers if self.profile else _BLOCKER_FLAG.keys()
            for blocker in job.triage.eligibility_blockers:     # only blockers whose quote was verified reach the verdict
                if blocker in allowed_blockers:
                    kind = _BLOCKER_FLAG.get(blocker)
                    if kind and kind not in seen:
                        flags.append({"kind": kind, "strength": "required", "snippet": job.triage.blocker_quote[:200]})
                        seen.add(kind)
        if flags:
            decision = _replace(decision, eligibility_json=json.dumps(flags))
        if decision.status == EXCLUDED:
            return decision
        required = required_flags(flags)
        if required:
            note = likely_ineligible_note(flags)
            posting = required[0]["snippet"]
            if decision.status == TARGET:
                decision = _replace(decision, status=MAYBE, rule="likely_ineligible",
                                    reason=f"{note}. Posting says: {posting}"[:400])
            else:
                decision = _replace(decision, reason=f"{decision.reason} {note}. Posting says: {posting}"[:400])
        applied: list[dict[str, Any]] = []
        penalty = 0.0
        for rule in self.learned_rules:
            if rule.matches(job.title, job.description):
                applied.append({"id": rule.id, "phrase": rule.phrase, "demote": rule.demote, "penalty": rule.penalty,
                                "tooltip": rule.tooltip()})
                penalty += rule.penalty
                if rule.demote and decision.status == TARGET:
                    decision = _replace(decision, status=MAYBE, rule="learned_rule",
                                        reason=f"Learned rule {rule.id}: '{rule.phrase}' ({rule.evidence_count} of my decisions).")
        if applied:
            decision = _replace(decision, learned_json=json.dumps(applied), learned_penalty=min(penalty, 20.0))
        return decision

    def _evaluate(self, job: GateJob, today: date, early_career: bool = False) -> GateDecision:
        title = normalize_for_matching(job.title)
        has_text = job.has_description
        base_confidence = "high" if has_text else "low"

        hit = self._exclude.find(title)
        if hit:
            return self._excluded("hard_exclude_title", f"Title contains the excluded phrase '{hit}'.", base_confidence)

        stack = self._excluded_stack_in_title(job.title)
        if stack:
            return self._excluded("primary_stack_title", f"Title names {stack} as the stack (no Python/JS/TS in the title).",
                                  base_confidence)

        settings = self.settings
        exclude_levels = self.profile.level_policy.exclude_levels if self.profile else settings.seniority.exclude_levels
        if job.experience_level in exclude_levels:
            return self._excluded("seniority_level", f"Marked {job.experience_level} level from the title or description.",
                                  base_confidence)

        # The title family comes before the description rules so that an unrelated role is reported as
        # "no target title", not as whatever description rule happened to fire first.
        family = self._demote_other_domain(job, title, self._family_decision(job, title, base_confidence, None))
        if family.status == EXCLUDED:
            return family

        years, years_phrase = (None, None)
        if has_text:
            pooled = _POOLED_POSTING.search(normalize_for_matching(f"{job.title} {job.description[:600]}"))
            stack_reason = None if pooled else self._excluded_stack_in_description(job.description or "")
            if stack_reason:
                return self._excluded("primary_stack_description", stack_reason, "high")
            years, years_phrase = required_years(job.description, job.title)
            max_years = self.profile.level_policy.max_required_years if self.profile else settings.seniority.exclude_at_years
            if years is not None and years >= max_years and not self._exempt_from_years(job, early_career):
                return GateDecision(
                    EXCLUDED, "required_years",
                    f"Asks for {years}+ years of experience ('{years_phrase}'); my limit is "
                    f"{max_years}.",
                    confidence="high", required_years=years,
                )

        date_exclusion = self._date_exclusion(job, today, base_confidence, years)
        if date_exclusion:
            return date_exclusion

        return self._apply_caps(_replace(family, required_years=years), job, years, years_phrase, early_career)

    # rules -------------------------------------------------------------------------------------

    def _exempt_from_years(self, job: GateJob, early_career: bool = False) -> bool:
        """Interns, new grads, graduate / apprenticeship / trainee / early-careers titles skip the years rule."""
        exempt_levels = self.profile.level_policy.exempt_levels if self.profile else self.settings.seniority.exempt_levels
        return (early_career or job.experience_level in exempt_levels
                or job.employment_type == "internship")

    def _is_soft(self, normalized: str, start: int, end: int) -> bool:
        """True when "a plus" / "nice to have" style wording sits right next to the mention."""
        window = normalized[max(0, start - _SOFT_CONTEXT_CHARS): end + _SOFT_CONTEXT_CHARS]
        if self._soft.search(window) is not None:
            return True
        return _LIST_CUES.search(normalized[max(0, start - _LIST_CONTEXT_CHARS): start]) is not None

    def _excluded_stack_in_title(self, raw_title: str) -> str | None:
        normalized = normalize_for_matching(raw_title)
        if self._allowed.find(normalized):
            return None
        for name, phrases in self._stacks.items():
            if self._required_mentions(normalized, phrases) > 0:
                return name
        return None

    def _excluded_stack_in_description(self, description: str) -> str | None:
        normalized = normalize_for_matching(description)
        if self._required_mentions(normalized, self._allowed) > 0:
            return None
        best: tuple[int, str, str] | None = None
        for name, phrases in self._stacks.items():
            count = self._required_mentions(normalized, phrases)
            if count >= 2 and (best is None or count > best[0]):
                best = (count, name, phrases.find(normalized) or name)
        if best:
            return (f"Description requires {best[1]} ('{best[2]}' mentioned {best[0]} times as a requirement) "
                    f"and none of Python/JavaScript/TypeScript.")
        return None

    def _required_mentions(self, normalized: str, phrases: PhraseSet) -> int:
        """Mentions of `phrases` that are not softened by "a plus" / "nice to have" wording."""
        return sum(1 for match in phrases.find_all(normalized) if not self._is_soft(normalized, match.start(), match.end()))

    def _date_exclusion(self, job: GateJob, today: date, confidence: str, years: int | None) -> GateDecision | None:
        settings = self.settings
        head = (job.description or "")[:_DESCRIPTION_HEAD_FOR_TERMS]
        target_year = (self.profile.person.graduation_year if self.profile and self.profile.person.graduation_year
                       else settings.target_graduation_year)

        early_career = job.experience_level in {"new_grad", "intern", "entry"} or job.employment_type == "internship"
        if early_career:
            stated = graduation_years(job.title, job.description)
            if job.graduation_year_hint:
                stated.add(job.graduation_year_hint)
            if stated and target_year not in stated:
                return GateDecision(
                    EXCLUDED, "graduation_year",
                    f"Names class / start year {sorted(stated)}, not {target_year}.",
                    confidence=confidence, required_years=years,
                )

        is_internship = job.employment_type == "internship" or job.experience_level == "intern"
        if is_internship and settings.internships_must_be_current:
            ends = internship_term_ends(job.title, head)
            if ends and max(ends) < today:
                latest = max(ends).isoformat()
                return GateDecision(
                    EXCLUDED, "internship_season_over",
                    f"Internship term already ended ({latest}); today is {today.isoformat()}.",
                    confidence=confidence, required_years=years,
                )
        return None

    def _family_decision(self, job: GateJob, title: str, confidence: str, years: int | None) -> GateDecision:
        titles = self.settings.titles

        lower_hit: tuple[str, str] | None = None
        for name, phrases in self._lower.items():
            found = phrases.find(title)
            if found:
                lower_hit = (name, found)
                break
        ai_boost = self._ai_boost.find(title)

        ai_hit = self._ai_ml.find(title)
        if ai_hit:
            return GateDecision(TARGET, "title_ai_ml", f"AI/ML title ('{ai_hit}').", "ai_ml", confidence=confidence,
                                required_years=years)
        bare_hit = self._ai_bare.find(title)
        if bare_hit and not lower_hit:
            noun = self._role_nouns.find(title)
            if noun:
                return GateDecision(TARGET, "title_ai_ml", f"AI/ML title ('{bare_hit}' + '{noun}').", "ai_ml",
                                    confidence=confidence, required_years=years)
            return GateDecision(MAYBE, "title_ai_weak",
                                f"'{bare_hit}' in the title but no technical role noun: could be a non-engineering role.",
                                "ai_ml", confidence=confidence, required_years=years)

        if lower_hit and ai_boost:
            name, found = lower_hit
            return GateDecision(
                TARGET, "title_ai_side_lower_family",
                f"{name.replace('_', ' ')} role on the AI side ('{ai_boost}'): treated as an AI target.",
                "ai_ml", f"{name}_ai", rank_adjust=titles.ai_boost_adjust, confidence=confidence, required_years=years,
            )

        fullstack_hit = self._fullstack.find(title)
        if fullstack_hit and lower_hit:
            # "Software Engineer, Data Platform": still a software role, mildly ranked down.
            name, found = lower_hit
            return GateDecision(
                TARGET, "title_fullstack",
                f"Software / full-stack title ('{fullstack_hit}') with a {name.replace('_', ' ')} focus ('{found}').",
                "fullstack", name, rank_adjust=titles.lower_with_software_adjust, confidence=confidence,
                required_years=years,
            )
        if fullstack_hit:
            return GateDecision(TARGET, "title_fullstack", f"Software / full-stack title ('{fullstack_hit}').",
                                "fullstack", confidence=confidence, required_years=years)

        weak_hit = self._fullstack_weak.find(title)
        if lower_hit:
            name, found = lower_hit
            return GateDecision(
                MAYBE, "lower_priority_family",
                f"Allowed but ranked lower: {name.replace('_', ' ')} role ('{found}').",
                "other", name, rank_adjust=titles.lower_priority_adjust, confidence=confidence, required_years=years,
            )

        if weak_hit:
            return GateDecision(TARGET, "title_fullstack", f"Software title ('{weak_hit}').", "fullstack",
                                confidence=confidence, required_years=years)

        maybe_hit = self._maybe.find(title)
        if maybe_hit:
            return GateDecision(MAYBE, "title_maybe", f"Plausible title ('{maybe_hit}') that needs a second look.",
                                "other", confidence=confidence, required_years=years)

        generic_hit = self._generic.find(title)
        if generic_hit:
            if not job.has_description:
                return GateDecision(MAYBE, "generic_title_no_description",
                                    f"Generic title ('{generic_hit}') and no description yet.",
                                    "other", confidence="low", required_years=years)
            signals = {m.group(0) for m in self._tech_signals.find_all(normalize_for_matching(job.description))}
            if len(signals) >= titles.tech_signal_min:
                shown = ", ".join(sorted(signals)[:4])
                return GateDecision(MAYBE, "generic_title_with_tech_signals",
                                    f"Generic title ('{generic_hit}') but the description shows software signals ({shown}).",
                                    "other", confidence="medium", required_years=years)
            return GateDecision(EXCLUDED, "no_target_title",
                                f"Generic title ('{generic_hit}') and the description shows no software signals.",
                                "other", confidence=confidence, required_years=years)

        return GateDecision(EXCLUDED, "no_target_title",
                            "Title matches no AI/ML, software or allowed family.", "other",
                            confidence=confidence, required_years=years)

    def _demote_other_domain(self, job: GateJob, title: str, decision: GateDecision) -> GateDecision:
        """A would-be `target` becomes `maybe` when the title names another domain (games, low-code) or is
        non-English text with no English role word. Never promotes, never excludes."""
        if decision.status != TARGET:
            return decision
        titles = self.settings.titles
        for name, phrases in self._off_domain.items():
            found = phrases.find(title)
            if found:
                return _replace(
                    decision, status=MAYBE, rule="off_domain_title", role_family="other", family_detail=name,
                    rank_adjust=titles.lower_priority_adjust,
                    reason=f"Looks like software but sits in another domain: {name.replace('_', ' ')} ('{found}').",
                )
        if titles.cjk_title_is_maybe and _CJK.search(job.title):
            english = normalize_for_matching(_CJK.sub(" ", job.title))
            if not any(words.find(english) for words in self._english_role_words):
                return _replace(
                    decision, status=MAYBE, rule="cjk_title_no_english_role", role_family="other",
                    family_detail="cjk_title", rank_adjust=titles.lower_priority_adjust,
                    reason="Title is Japanese / Chinese / Korean text with no English role word: probably needs "
                           "that language.",
                )
        return decision

    def _apply_caps(self, decision: GateDecision, job: GateJob, years: int | None, years_phrase: str | None,
                    early_career: bool = False) -> GateDecision:
        """Cap a `target` at `maybe` when experience hints say it is a stretch."""
        if decision.status != TARGET:
            return decision
        settings = self.settings.seniority
        if years is not None and years == settings.maybe_at_years and not self._exempt_from_years(job, early_career):
            return _replace(decision, status=MAYBE, rule="required_years_borderline",
                            reason=f"{decision.reason} Asks for {years} years ('{years_phrase}'): a stretch.")
        maybe_levels = self.profile.level_policy.maybe_levels if self.profile else settings.maybe_levels
        if job.experience_level in maybe_levels:
            return _replace(decision, status=MAYBE, rule="mid_level_title",
                            reason=f"{decision.reason} Title reads mid-level ({job.experience_level}).")
        return decision

    @staticmethod
    def _excluded(rule: str, reason: str, confidence: str) -> GateDecision:
        return GateDecision(EXCLUDED, rule, reason, "other", confidence=confidence)


def _replace(decision: GateDecision, **changes: Any) -> GateDecision:
    values = asdict(decision)
    values.update(changes)
    return GateDecision(**values)


# --- hashing and persistence ------------------------------------------------------------------


def rules_hash(settings: RelevanceSettings, today: date, learned_signature: str = "") -> str:
    """Changes when the rules, the gate code, approved learned rules, or the month (internship seasons) change."""
    payload = json.dumps(asdict(settings), sort_keys=True, default=list)
    month = f"{today.year}-{today.month:02d}"
    return hashlib.sha256(f"gate{GATE_VERSION}|{month}|{payload}|{learned_signature}".encode("utf-8")).hexdigest()[:20]


def _degrees(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return ()
    return tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()


def input_hash(row: Mapping[str, Any], triage_key: str = "") -> str:
    parts = (
        row["title"], row["company_name"], row["location_raw"] or "", row["experience_level"], row["employment_type"],
        row["graduation_year_hint"] if row["graduation_year_hint"] is not None else "",
        row["description_hash"] or "", triage_key,
    )
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:20]


def _triage_key(verdicts: Mapping[int, Any], exempt: set[int], job_id: int, student: Any) -> str:
    """What a job's verdict depends on besides its text: the LLM verdict row, else the student model in use (never for exempt jobs)."""
    stored = verdicts.get(job_id)
    if stored is not None:
        return verdict_key(stored)
    return "" if job_id in exempt or student is None else "s" + student_key(student)


def _opinion(student: Any, verdicts: Mapping[int, Any], exempt: set[int], row: Mapping[str, Any]) -> Opinion | None:
    job_id = int(row["id"])
    if student is None or job_id in verdicts or job_id in exempt:
        return None
    evidence = build_evidence(row["title"] or "", row["company_name"] or "", row["location_raw"], row["workplace_type"],
                              row["remote_regions"], row["description_text"])
    return student.opinion(evidence)


@dataclass
class RelevanceStats:
    examined: int = 0
    computed: int = 0
    unchanged: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    rules_hash: str = ""


_LIGHT_COLUMNS = (
    "j.id, j.title, j.company_name, j.location_raw, j.experience_level, j.employment_type, "
    "j.graduation_year_hint, j.description_hash, r.rules_hash AS stored_rules, r.input_hash AS stored_input"
)


def run_relevance(
    connection: sqlite3.Connection,
    config: Config,
    *,
    force: bool = False,
    clock: Clock = utc_now,
    batch_size: int = 500,
    profile: Any | None = None,
) -> RelevanceStats:
    """Judge every open job whose verdict is missing or stale (every open job with `force`)."""
    now = clock()
    today = now.date()
    from jobhunt.matching import learned_rules

    from jobhunt.profile import get_profile

    profile = profile or get_profile(config)
    learned = learned_rules.load_rules(config.resolve_path(profile.paths.learned_rules if profile else learned_rules.LEARNED_FILE))
    company_path = config.resolve_path(profile.paths.company_rules if profile else COMPANY_RULES_FILE)
    company = load_company_rules(company_path)
    gate = RelevanceGate(config.relevance, learned, company, config.triage, profile=profile)
    student = load_student(config)
    triage_rules = (f"triage|{config.triage.enabled}|{config.triage.rubric_version}|{config.triage.exclude_years_min}|"
                    f"{student_key(student)}")
    # A different profile (graduation year, level policy, ...) must never reuse another profile's stored decisions.
    triage_rules += f"|profile|{profile.content_hash if profile else ''}"
    current_rules = rules_hash(config.relevance, today,
                               learned_rules.signature(learned) + "|" + company_signature(company) + "|" + triage_rules)
    verdicts, exempt = usable_and_exempt(connection, config)
    stats = RelevanceStats(rules_hash=current_rules)
    now_iso = to_iso(now)

    light = connection.execute(
        f"""SELECT {_LIGHT_COLUMNS} FROM jobs j LEFT JOIN job_relevance r ON r.job_id = j.id
            WHERE j.status = 'open' ORDER BY j.id"""
    ).fetchall()

    stale: list[int] = []
    for row in light:
        stats.examined += 1
        key = _triage_key(verdicts, exempt, int(row["id"]), student)
        fresh = (not force and row["stored_rules"] == current_rules and row["stored_input"] == input_hash(row, key))
        if fresh:
            stats.unchanged += 1
        else:
            stale.append(int(row["id"]))

    for start in range(0, len(stale), batch_size):
        chunk = stale[start:start + batch_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""SELECT id, title, company_name, location_raw, experience_level, employment_type, workplace_type, remote_regions,
                       graduation_year_hint, description_text, description_hash,
                       CASE WHEN json_valid(raw_json) THEN json_extract(raw_json, '$.degrees') END AS degrees_json
                FROM jobs WHERE id IN ({placeholders})""", chunk,
        ).fetchall()
        values: list[tuple[Any, ...]] = []
        for row in rows:
            decision = gate.evaluate(
                GateJob(
                    job_id=int(row["id"]), title=row["title"], company_name=row["company_name"],
                    location_raw=row["location_raw"], experience_level=row["experience_level"],
                    employment_type=row["employment_type"], graduation_year_hint=row["graduation_year_hint"],
                    description=row["description_text"], degrees=_degrees(row["degrees_json"]),
                    triage=verdicts[int(row["id"])].verdict if int(row["id"]) in verdicts else None,
                    student=_opinion(student, verdicts, exempt, row),
                ),
                today,
            )
            stats.by_status[decision.status] = stats.by_status.get(decision.status, 0) + 1
            stats.by_rule[decision.rule] = stats.by_rule.get(decision.rule, 0) + 1
            values.append((
                int(row["id"]), now_iso, current_rules, input_hash(row, _triage_key(verdicts, exempt, int(row["id"]), student)),
                decision.status, decision.rule,
                decision.reason[:400], decision.role_family, decision.family_detail, decision.rank_adjust,
                decision.confidence, decision.required_years, decision.seniority_level,
                decision.seniority_evidence, decision.eligibility_json or None, decision.learned_json or None,
                decision.triage_json or None,
            ))
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """INSERT INTO job_relevance (job_id, computed_at, rules_hash, input_hash, gate_status, gate_rule,
                                              gate_reason, role_family, family_detail, rank_adjust, confidence,
                                              required_years, seniority_level, seniority_evidence,
                                              eligibility_json, learned_json, triage_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                       computed_at = excluded.computed_at, rules_hash = excluded.rules_hash,
                       input_hash = excluded.input_hash, gate_status = excluded.gate_status,
                       gate_rule = excluded.gate_rule, gate_reason = excluded.gate_reason,
                       role_family = excluded.role_family, family_detail = excluded.family_detail,
                       rank_adjust = excluded.rank_adjust, confidence = excluded.confidence,
                       required_years = excluded.required_years, seniority_level = excluded.seniority_level,
                       seniority_evidence = excluded.seniority_evidence,
                       eligibility_json = excluded.eligibility_json, learned_json = excluded.learned_json,
                       triage_json = excluded.triage_json""",
                values,
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        stats.computed += len(values)
    return stats
