"""Applies LLM verdicts to job relevance and location fit (Brief v10 & v10b).

Enforces:
1. Consensus acting rules:
   - Strong evidence: verified-and-relevant Gemini quote PLUS at least one independent agreement among:
     (a) Local LLM verified-and-relevant verdict
     (b) Deterministic signal (years parser >= 2, senior title word, explicit US-only phrase)
     (c) Classifier head above act threshold (p >= 0.90)
   - Strong senior/years >= 2 -> excluded (subject to company precedence)
   - Gemini alone -> maybe with years penalty and chip
   - Local LLM alone -> chip only (advisory)
   - Strong not-India -> location-hidden; Gemini alone -> maybe; Local alone -> chip
   - Strong blocker -> required eligibility flag; otherwise preferred / chip
2. Precedence:
   - A company rule that explicitly ALLOWS the job (e.g. Barclays BA4) overrides any LLM seniority exclusion (chip only).
3. Blast-radius guard (downgrades exclusions to maybe if > 25% of visible Relevant jobs would move).
4. Exemptions (applied, saved, tailored, manual, gold-yes, restored from auto-hidden, verdict is wrong).
5. High-stakes Gemini confirmation for top 50 jobs when verdict is not already Gemini-sourced.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.matching.budget import BudgetGuard, UsageLedger
from jobhunt.matching.company_rules import (
    COMPANY_RULES_FILE,
    CompanyRuleSet,
    level_word_in_title,
    load_company_rules,
)
from jobhunt.matching.gemini import GeminiClient
from jobhunt.matching.triage import (
    BLOCKERS,
    Effect,
    Evidence,
    Verdict,
    build_evidence,
    effect_of,
)
from jobhunt.matching.triage_apply import YEARS_PENALTY, protected_ids
from jobhunt.matching.triage_benchmark import ActingRuleStatus, load_acting_rules_from_db
from jobhunt.matching.triage_labels import _EXPLICIT_NON_INDIA
from jobhunt.matching.years import required_years
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)


@dataclass
class TriageApplicationResult:
    examined: int = 0
    excluded_seniority: int = 0
    caution_seniority: int = 0
    hidden_location: int = 0
    blockers_applied: int = 0
    advisory_only: int = 0
    exempt_protected_count: int = 0
    protected_jobs_would_move: list[dict[str, Any]] = field(default_factory=list)
    high_stakes_gemini_called: int = 0
    high_stakes_disagreements: int = 0
    blast_radius_triggered: bool = False
    blast_radius_detail: str = ""
    moved_out_count: int = 0
    total_visible_relevant: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)


def load_all_exempt_job_ids(connection: sqlite3.Connection, config: Config) -> set[int]:
    """Protected jobs (applied, saved, tailored, manual, gold-yes) + restored + wrong verdicts."""
    prot = protected_ids(connection, config)

    # Restored from Auto-hidden
    restored = {
        int(r[0])
        for r in connection.execute("SELECT job_id FROM job_auto_hidden WHERE restored_at IS NOT NULL").fetchall()
    }

    # "LLM verdict is wrong"
    wrong = {
        int(r[0])
        for r in connection.execute("SELECT job_id FROM job_llm_verdicts WHERE human_label IS NOT NULL").fetchall()
    }

    return prot | restored | wrong


def get_visible_relevant_jobs(connection: sqlite3.Connection) -> dict[int, float]:
    """(job_id -> score) of open, target, location-visible, non-hidden jobs."""
    query = """
        SELECT j.id, r.final_score
        FROM jobs j
        JOIN job_relevance r ON r.job_id = j.id
        LEFT JOIN job_auto_hidden h ON h.job_id = j.id AND h.restored_at IS NULL
        WHERE j.status = 'open'
          AND r.gate_status = 'target'
          AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
          AND h.job_id IS NULL
        ORDER BY r.final_score DESC, j.first_seen_at DESC
    """
    return {int(r[0]): float(r[1] or 0.0) for r in connection.execute(query).fetchall()}


def is_company_rule_seniority_allowed(
    connection: sqlite3.Connection,
    config: Config,
    job_id: int,
    title: str,
    company: str,
    description: str | None = None,
) -> bool:
    """Precedence check: returns True if an explicit company rule ALLOWS the job."""
    rules_path = config.resolve_path(COMPANY_RULES_FILE)
    if not Path(rules_path).is_file():
        return False
    company_rules = load_company_rules(rules_path)
    rule_set = CompanyRuleSet(company_rules)
    cv = rule_set.judge(company, title, description)
    return cv.outcome == "allowed"


def check_strong_seniority(
    gemini_v: Verdict | None,
    job_title: str,
    job_text: str,
    local_v: Verdict | None = None,
    head_p: float | None = None,
    company_rule_override: bool = False,
) -> tuple[bool, str]:
    """Check consensus strong seniority evidence.

    Returns (is_strong, strength_reason).
    If company_rule_override is True, seniority exclusion is overridden to advisory.
    """
    g_senior = False
    if gemini_v:
        is_g_lvl = (
            gemini_v.level in ("senior", "lead_or_manager")
            and bool(gemini_v.level_quote)
            and "level" not in gemini_v.irrelevant_evidence
            and "level" not in gemini_v.hallucinated
        )
        is_g_yr = (
            gemini_v.required_years_min is not None
            and gemini_v.required_years_min >= 2
            and bool(gemini_v.years_quote)
            and "required_years_min" not in gemini_v.irrelevant_evidence
            and "required_years_min" not in gemini_v.hallucinated
        )
        g_senior = is_g_lvl or is_g_yr

    l_senior = False
    if local_v:
        is_l_lvl = (
            local_v.level in ("senior", "lead_or_manager")
            and bool(local_v.level_quote)
            and "level" not in local_v.irrelevant_evidence
            and "level" not in local_v.hallucinated
        )
        is_l_yr = (
            local_v.required_years_min is not None
            and local_v.required_years_min >= 2
            and bool(local_v.years_quote)
            and "required_years_min" not in local_v.irrelevant_evidence
            and "required_years_min" not in local_v.hallucinated
        )
        l_senior = is_l_lvl or is_l_yr

    # (b) Deterministic signals
    parsed_years, _ = required_years(job_text)
    parser_years_ge_2 = bool(parsed_years is not None and parsed_years >= 2)
    senior_title_word = level_word_in_title(job_title)

    # (c) Head
    head_senior_act = bool(head_p is not None and head_p >= 0.90)

    senior_agreements: list[str] = []
    if l_senior:
        senior_agreements.append("local LLM")
    if senior_title_word:
        senior_agreements.append(f"title word '{senior_title_word}'")
    elif parser_years_ge_2:
        senior_agreements.append(f"parser years >= 2 ({parsed_years} yrs)")
    if head_senior_act:
        senior_agreements.append(f"classifier head p={head_p:.2f}")

    is_strong = g_senior and bool(senior_agreements)
    base_reason = (
        f"Gemini + {' + '.join(senior_agreements)}"
        if is_strong
        else ("Gemini alone" if g_senior else ("local LLM alone" if l_senior else "none"))
    )

    if company_rule_override:
        return False, f"{base_reason} [company rule allows this grade]"

    return is_strong, base_reason


def check_strong_location(
    gemini_v: Verdict | None,
    job_title: str,
    location_raw: str,
    job_text: str,
    local_v: Verdict | None = None,
    head_p: float | None = None,
) -> tuple[bool, str]:
    g_not_india = False
    if gemini_v:
        g_not_india = (
            gemini_v.india_eligible == "no"
            and bool(gemini_v.india_quote)
            and "india_eligible" not in gemini_v.irrelevant_evidence
            and "india_eligible" not in gemini_v.hallucinated
        )

    l_not_india = False
    if local_v:
        l_not_india = (
            local_v.india_eligible == "no"
            and bool(local_v.india_quote)
            and "india_eligible" not in local_v.irrelevant_evidence
            and "india_eligible" not in local_v.hallucinated
        )

    has_non_india_phrase = bool(_EXPLICIT_NON_INDIA.search(f"{job_title} {location_raw} {job_text}"))
    head_loc_act = bool(head_p is not None and head_p >= 0.90)

    loc_agreements: list[str] = []
    if l_not_india:
        loc_agreements.append("local LLM")
    if has_non_india_phrase:
        loc_agreements.append("explicit non-India phrase")
    if head_loc_act:
        loc_agreements.append(f"classifier head p={head_p:.2f}")

    is_strong = g_not_india and bool(loc_agreements)
    reason = (
        f"Gemini + {' + '.join(loc_agreements)}"
        if is_strong
        else ("Gemini alone" if g_not_india else ("local LLM alone" if l_not_india else "none"))
    )
    return is_strong, reason


def check_strong_blockers(
    gemini_v: Verdict | None,
    local_v: Verdict | None = None,
    head_p: float | None = None,
) -> tuple[bool, list[str], str]:
    g_blockers: list[str] = []
    if gemini_v:
        if (
            bool(gemini_v.eligibility_blockers)
            and bool(gemini_v.blocker_quote)
            and "eligibility_blockers" not in gemini_v.irrelevant_evidence
            and "eligibility_blockers" not in gemini_v.hallucinated
        ):
            g_blockers = list(gemini_v.eligibility_blockers)

    l_blockers: list[str] = []
    if local_v:
        if (
            bool(local_v.eligibility_blockers)
            and bool(local_v.blocker_quote)
            and "eligibility_blockers" not in local_v.irrelevant_evidence
            and "eligibility_blockers" not in local_v.hallucinated
        ):
            l_blockers = list(local_v.eligibility_blockers)

    head_elig_act = bool(head_p is not None and head_p >= 0.90)

    blk_agreements: list[str] = []
    if l_blockers and any(b in l_blockers for b in g_blockers):
        blk_agreements.append("local LLM")
    if head_elig_act:
        blk_agreements.append(f"classifier head p={head_p:.2f}")

    is_strong = bool(g_blockers) and bool(blk_agreements)
    active_blockers = g_blockers if g_blockers else l_blockers
    reason = (
        f"Gemini + {' + '.join(blk_agreements)}"
        if is_strong
        else ("Gemini alone" if g_blockers else ("local LLM alone" if l_blockers else "none"))
    )
    return is_strong, active_blockers, reason


def apply_local_llm_triage(
    connection: sqlite3.Connection,
    config: Config,
    verdicts: dict[int, Verdict],
    *,
    dry_run: bool = True,
    gemini_client: GeminiClient | None = None,
    clock: Clock = utc_now,
) -> TriageApplicationResult:
    """Apply LLM verdicts according to Brief v10b consensus and safety contract."""
    res = TriageApplicationResult(examined=len(verdicts))
    now_iso = to_iso(clock())

    # 1. Load acting rules and company rules
    db_rules = load_acting_rules_from_db(connection)
    can_act_map: dict[str, bool] = {f"{r.field}:{r.target_class}": r.can_act for r in db_rules}

    def _can_act_alone(field_name: str, class_name: str) -> bool:
        key = f"{field_name}:{class_name}"
        return can_act_map.get(key, False)

    gemini_alone_senior_ok = _can_act_alone("level", "senior_or_lead")
    gemini_alone_years_ok = _can_act_alone("years", "years_ge_2")
    gemini_alone_loc_ok = _can_act_alone("india_eligible", "not_india")

    # Company grade rules (Barclays BA4 etc.)
    rules_path = config.resolve_path(COMPANY_RULES_FILE)
    company_rules = load_company_rules(rules_path) if Path(rules_path).is_file() else []
    company_rule_set = CompanyRuleSet(company_rules)

    # 2. Load exemptions and current visible relevant jobs
    exempt_ids = load_all_exempt_job_ids(connection, config)
    visible_relevant = get_visible_relevant_jobs(connection)
    res.total_visible_relevant = len(visible_relevant)

    # Top 50 jobs for high-stakes check
    top_50_ids = set(list(visible_relevant.keys())[: config.triage.high_stakes_top_n])

    # 3. Pre-fetch job metadata and stored verdicts for all candidate jobs
    all_jids = list(verdicts.keys())
    job_metadata: dict[int, sqlite3.Row] = {}
    stored_verdicts: dict[int, dict[str, Verdict]] = {}  # jid -> {source: Verdict}

    if all_jids:
        placeholders = ",".join("?" for _ in all_jids)
        for r in connection.execute(
            f"SELECT id, title, company_name, location_raw, workplace_type, remote_regions, description_text FROM jobs WHERE id IN ({placeholders})",
            all_jids,
        ).fetchall():
            job_metadata[int(r["id"])] = r

        v_query = f"""
            SELECT job_id, source, model, verified_json
            FROM job_llm_verdicts
            WHERE rubric_version = ? AND job_id IN ({placeholders})
            ORDER BY id ASC
        """
        for r in connection.execute(v_query, [config.triage.rubric_version, *all_jids]).fetchall():
            jid = int(r["job_id"])
            src = r["source"] or ("gemini" if "gemini" in (r["model"] or "").lower() else "local_llm")
            try:
                v_obj = Verdict.from_json(json.loads(r["verified_json"]))
                if jid not in stored_verdicts:
                    stored_verdicts[jid] = {}
                stored_verdicts[jid][src] = v_obj
            except Exception:
                pass

    # Tier 1 classifier head confidences
    head_confidences: dict[int, dict[str, float]] = {}
    if all_jids:
        placeholders = ",".join("?" for _ in all_jids)
        for r in connection.execute(
            f"SELECT job_id, head, confidence FROM job_cascade_decisions WHERE tier = 1 AND job_id IN ({placeholders})",
            all_jids,
        ).fetchall():
            jid = int(r["job_id"])
            h = r["head"]
            c = float(r["confidence"] or 0.0)
            if jid not in head_confidences:
                head_confidences[jid] = {}
            head_confidences[jid][h] = c

    # 4. Simulate decisions to evaluate blast radius
    candidate_decisions: list[dict[str, Any]] = []
    would_move_out_count = 0

    for jid, passed_verdict in verdicts.items():
        is_exempt = jid in exempt_ids
        is_top_50 = jid in top_50_ids
        is_visible_relevant = jid in visible_relevant
        jrow = job_metadata.get(jid)
        title = (jrow["title"] or "") if jrow else ""
        comp_name = (jrow["company_name"] or "") if jrow else ""
        desc = (jrow["description_text"] or "") if jrow else ""
        loc_raw = (jrow["location_raw"] or "") if jrow else ""

        # Retrieve verdicts
        v_dict = stored_verdicts.get(jid, {})
        gemini_v = v_dict.get("gemini")
        local_v = v_dict.get("local_llm")

        if local_v is None:
            local_v = passed_verdict

        active_verdict = gemini_v if (gemini_v and config.triage.primary_judge != "local_llm") else (local_v or passed_verdict)
        verdict_source = "gemini" if (gemini_v and config.triage.primary_judge != "local_llm") else "local_llm"

        # Independent signals:
        # (b) Deterministic signals
        parsed_years, _ = required_years(desc)
        parser_years_ge_2 = bool(parsed_years is not None and parsed_years >= 2)
        senior_title_word = level_word_in_title(title)
        has_non_india_phrase = bool(_EXPLICIT_NON_INDIA.search(f"{title} {loc_raw} {desc}"))

        # (c) Classifier head above act threshold
        j_heads = head_confidences.get(jid, {})
        def _get_act_thresh(hname: str) -> float:
            if isinstance(config.triage.heads, dict):
                hobj = config.triage.heads.get(hname)
                return getattr(hobj, "act_threshold", 0.90) if hobj else 0.90
            return getattr(getattr(config.triage.heads, hname, None), "act_threshold", 0.90)

        head_senior_act = bool(j_heads.get("seniority", 0.0) >= _get_act_thresh("seniority"))
        head_loc_act = bool(j_heads.get("location", 0.0) >= _get_act_thresh("location"))
        head_elig_act = bool(j_heads.get("eligibility", 0.0) >= _get_act_thresh("eligibility"))

        # ---------------- Evaluated Seniority ----------------
        g_senior = False
        g_senior_quote = ""
        if gemini_v:
            is_g_lvl = (
                gemini_v.level in ("senior", "lead_or_manager")
                and bool(gemini_v.level_quote)
                and "level" not in gemini_v.irrelevant_evidence
                and "level" not in gemini_v.hallucinated
            )
            is_g_yr = (
                gemini_v.required_years_min is not None
                and gemini_v.required_years_min >= 2
                and bool(gemini_v.years_quote)
                and "required_years_min" not in gemini_v.irrelevant_evidence
                and "required_years_min" not in gemini_v.hallucinated
            )
            g_senior = is_g_lvl or is_g_yr
            g_senior_quote = gemini_v.level_quote if is_g_lvl else (gemini_v.years_quote if is_g_yr else "")

        l_senior = False
        l_senior_quote = ""
        if local_v:
            is_l_lvl = (
                local_v.level in ("senior", "lead_or_manager")
                and bool(local_v.level_quote)
                and "level" not in local_v.irrelevant_evidence
                and "level" not in local_v.hallucinated
            )
            is_l_yr = (
                local_v.required_years_min is not None
                and local_v.required_years_min >= 2
                and bool(local_v.years_quote)
                and "required_years_min" not in local_v.irrelevant_evidence
                and "required_years_min" not in local_v.hallucinated
            )
            l_senior = is_l_lvl or is_l_yr
            l_senior_quote = local_v.level_quote if is_l_lvl else (local_v.years_quote if is_l_yr else "")

        senior_agreements: list[str] = []
        if l_senior:
            senior_agreements.append("local LLM")
        if senior_title_word:
            senior_agreements.append(f"title word '{senior_title_word}'")
        elif parser_years_ge_2:
            senior_agreements.append(f"parser years >= 2 ({parsed_years} yrs)")
        if head_senior_act:
            senior_agreements.append(f"classifier head p={j_heads['seniority']:.2f}")

        is_strong_senior = g_senior and bool(senior_agreements)

        # ---------------- Evaluated Location ----------------
        g_not_india = False
        g_loc_quote = ""
        if gemini_v:
            g_not_india = (
                gemini_v.india_eligible == "no"
                and bool(gemini_v.india_quote)
                and "india_eligible" not in gemini_v.irrelevant_evidence
                and "india_eligible" not in gemini_v.hallucinated
            )
            g_loc_quote = gemini_v.india_quote if g_not_india else ""

        l_not_india = False
        l_loc_quote = ""
        if local_v:
            l_not_india = (
                local_v.india_eligible == "no"
                and bool(local_v.india_quote)
                and "india_eligible" not in local_v.irrelevant_evidence
                and "india_eligible" not in local_v.hallucinated
            )
            l_loc_quote = local_v.india_quote if l_not_india else ""

        loc_agreements: list[str] = []
        if l_not_india:
            loc_agreements.append("local LLM")
        if has_non_india_phrase:
            loc_agreements.append("explicit non-India phrase")
        if head_loc_act:
            loc_agreements.append(f"classifier head p={j_heads['location']:.2f}")

        is_strong_loc = g_not_india and bool(loc_agreements)

        # ---------------- Evaluated Blockers ----------------
        g_blockers = []
        g_blk_quote = ""
        if gemini_v:
            if (
                bool(gemini_v.eligibility_blockers)
                and bool(gemini_v.blocker_quote)
                and "eligibility_blockers" not in gemini_v.irrelevant_evidence
                and "eligibility_blockers" not in gemini_v.hallucinated
            ):
                g_blockers = list(gemini_v.eligibility_blockers)
                g_blk_quote = gemini_v.blocker_quote

        l_blockers = []
        l_blk_quote = ""
        if local_v:
            if (
                bool(local_v.eligibility_blockers)
                and bool(local_v.blocker_quote)
                and "eligibility_blockers" not in local_v.irrelevant_evidence
                and "eligibility_blockers" not in local_v.hallucinated
            ):
                l_blockers = list(local_v.eligibility_blockers)
                l_blk_quote = local_v.blocker_quote

        blk_agreements: list[str] = []
        if l_blockers:
            blk_agreements.append("local LLM")
        if head_elig_act:
            blk_agreements.append(f"classifier head p={j_heads['eligibility']:.2f}")

        is_strong_blk = bool(g_blockers and blk_agreements)

        # Determine Proposed Action, Precedence, and Strength Reason
        proposed_action = "none"
        reason = ""
        evidence = ""
        strength_reason = ""
        company_precedence_applied = False

        is_local_mode = (config.triage.primary_judge == "local_llm")

        # Priority 1: Seniority
        if is_strong_senior or (g_senior and (gemini_alone_senior_ok or gemini_alone_years_ok)):
            # Check company rule precedence
            comp_verdict = company_rule_set.judge(comp_name, title, desc)
            if comp_verdict.outcome == "allowed":
                # Precedence: company rule explicitly allows job -> override exclusion to chip only!
                proposed_action = "advisory"
                company_precedence_applied = True
                reason = "company rule allows this grade"
                strength_reason = f"Gemini + {', '.join(senior_agreements)} [company rule allows this grade]"
                evidence = g_senior_quote
            else:
                proposed_action = "exclude"
                reason = f"Strong senior evidence (Gemini + {', '.join(senior_agreements)}): \u201c{g_senior_quote}\u201d"
                strength_reason = f"Gemini + {', '.join(senior_agreements)}"
                evidence = g_senior_quote
        elif g_senior:
            # Gemini alone (verified and relevant) -> maybe with years penalty and chip
            proposed_action = "maybe"
            comp_verdict = company_rule_set.judge(comp_name, title, desc)
            if comp_verdict.outcome == "allowed":
                company_precedence_applied = True
                strength_reason = "Gemini alone [company rule allows this grade]"
                reason = "company rule allows this grade"
            else:
                strength_reason = "Gemini alone"
                reason = f"Gemini seniority verdict: \u201c{g_senior_quote}\u201d"
            evidence = g_senior_quote
        elif l_senior:
            local_senior_can_act = bool(
                is_local_mode
                or can_act_map.get("level:senior_or_lead")
                or can_act_map.get("years:years_ge_2")
            )
            if local_senior_can_act and local_v.confidence == "high":
                comp_verdict = company_rule_set.judge(comp_name, title, desc)
                if comp_verdict.outcome == "allowed":
                    proposed_action = "advisory"
                    company_precedence_applied = True
                    reason = "company rule allows this grade"
                    strength_reason = "Local LLM [company rule allows this grade]"
                    evidence = l_senior_quote
                else:
                    proposed_action = "exclude"
                    reason = f"Local LLM seniority: \u201c{l_senior_quote}\u201d"
                    strength_reason = "Local LLM"
                    evidence = l_senior_quote
            elif local_senior_can_act:
                proposed_action = "maybe"
                reason = f"Local LLM seniority caution: \u201c{l_senior_quote}\u201d"
                strength_reason = "Local LLM caution"
                evidence = l_senior_quote
            else:
                proposed_action = "advisory"
                strength_reason = "Local LLM alone"
                reason = f"Local LLM seniority second opinion: \u201c{l_senior_quote}\u201d"
                evidence = l_senior_quote
        elif is_local_mode:
            is_l_caution = bool(
                local_v and (
                    local_v.required_years_min == 1
                    or local_v.confidence == "medium"
                    or local_v.suitable_for_new_grad == "no"
                ) and "required_years_min" not in local_v.hallucinated and "level" not in local_v.hallucinated
            )
            if is_l_caution:
                proposed_action = "maybe"
                reason = f"Local LLM seniority caution: \u201c{local_v.years_quote or local_v.level_quote}\u201d"
                strength_reason = "Local LLM caution"
                evidence = local_v.years_quote or local_v.level_quote

        # Priority 2: Location (if not already excluded)
        if proposed_action in ("none", "advisory"):
            if is_strong_loc or (g_not_india and gemini_alone_loc_ok):
                proposed_action = "hidden_location"
                reason = f"Strong not-India evidence (Gemini + {', '.join(loc_agreements)}): \u201c{g_loc_quote}\u201d"
                strength_reason = f"Gemini + {', '.join(loc_agreements)}"
                evidence = g_loc_quote
            elif g_not_india:
                proposed_action = "maybe"
                strength_reason = "Gemini alone"
                reason = f"Gemini location caution: \u201c{g_loc_quote}\u201d"
                evidence = g_loc_quote
            elif l_not_india:
                local_loc_can_act = bool(is_local_mode or can_act_map.get("india_eligible:not_india"))
                if local_loc_can_act and local_v.confidence == "high":
                    proposed_action = "hidden_location"
                    reason = f"Local LLM location: \u201c{l_loc_quote}\u201d"
                    strength_reason = "Local LLM"
                    evidence = l_loc_quote
                elif local_loc_can_act:
                    proposed_action = "maybe"
                    reason = f"Local LLM location caution: \u201c{l_loc_quote}\u201d"
                    strength_reason = "Local LLM caution"
                    evidence = l_loc_quote
                elif proposed_action == "none":
                    proposed_action = "advisory"
                    strength_reason = "Local LLM alone"
                    reason = f"Local LLM location second opinion: \u201c{l_loc_quote}\u201d"
                    evidence = l_loc_quote

        # Priority 3: Blockers
        if is_strong_blk:
            # Strong blocker -> required flag
            if proposed_action in ("none", "advisory"):
                proposed_action = "blocker"
                reason = f"Strong blocker evidence (Gemini + {', '.join(blk_agreements)}): {', '.join(g_blockers)}"
                strength_reason = f"Gemini + {', '.join(blk_agreements)}"
                evidence = g_blk_quote
        elif l_blockers and (is_local_mode or can_act_map.get("blockers:any_blocker")):
            if proposed_action in ("none", "advisory"):
                proposed_action = "blocker"
                reason = f"Blocker: {', '.join(l_blockers)}"
                strength_reason = "Local LLM"
                evidence = l_blk_quote
        elif g_blockers or l_blockers:
            # Advisory blocker flag
            if proposed_action == "none":
                proposed_action = "blocker_preferred"
                blks = g_blockers or l_blockers
                reason = f"Blocker chip: {', '.join(blks)}"
                strength_reason = "Gemini alone" if g_blockers else "Local LLM alone"
                evidence = g_blk_quote or l_blk_quote

        would_remove = proposed_action in ("exclude", "hidden_location")

        if is_exempt:
            res.exempt_protected_count += 1
            if would_remove or proposed_action == "maybe":
                res.protected_jobs_would_move.append({
                    "job_id": jid,
                    "action": proposed_action,
                    "reason": reason,
                    "evidence": evidence,
                    "strength_reason": strength_reason,
                })
            # Skip moving exempt jobs
            continue

        if is_visible_relevant and would_remove:
            would_move_out_count += 1

        candidate_decisions.append({
            "job_id": jid,
            "verdict": active_verdict,
            "verdict_source": verdict_source,
            "action": proposed_action,
            "reason": reason,
            "evidence": evidence,
            "strength_reason": strength_reason,
            "company_precedence": company_precedence_applied,
            "blockers": g_blockers or l_blockers,
            "is_top_50": is_top_50,
            "would_remove": would_remove,
        })

    # 5. Check blast-radius guard (> 25% of visible Relevant jobs would move)
    res.moved_out_count = would_move_out_count
    max_allowed_moved = int(res.total_visible_relevant * config.triage.blast_radius_max_ratio)

    if res.total_visible_relevant > 0 and would_move_out_count > max_allowed_moved:
        res.blast_radius_triggered = True
        res.blast_radius_detail = (
            f"Blast-radius guard triggered: {would_move_out_count} of {res.total_visible_relevant} "
            f"({would_move_out_count / res.total_visible_relevant:.1%}) would move out of Relevant "
            f"(cap is {config.triage.blast_radius_max_ratio:.0%}). Exclusions downgraded to maybe."
        )
        logger.warning(res.blast_radius_detail)

    # 6. High-stakes check for top 50 non-Gemini removals and persistence
    ledger = UsageLedger(connection, config.pricing)
    guard = BudgetGuard(ledger, config.budget)

    for item in candidate_decisions:
        jid = item["job_id"]
        action = item["action"]
        verdict = item["verdict"]
        v_src = item["verdict_source"]
        reason = item["reason"]
        evidence = item["evidence"]
        strength_reason = item["strength_reason"]
        comp_precedence = item["company_precedence"]
        is_top_50 = item["is_top_50"]

        if action == "none":
            continue

        # High-Stakes Confirmation for top 50 jobs ONLY when verdict is not already Gemini-sourced
        if is_top_50 and item["would_remove"] and v_src != "gemini":
            summary = ledger.summary()
            monthly_remaining = config.budget.monthly_usd - summary.usd_month
            can_call_gemini = (
                gemini_client is not None
                and guard.check_triage(0.01, enforce_daily=True).allowed
                and (monthly_remaining >= 1.00)
            )
            if can_call_gemini:
                try:
                    res.high_stakes_gemini_called += 1
                    from jobhunt.matching.triage import TriageJudge

                    judge = TriageJudge(connection, config, gemini_client, ledger, guard, clock=clock)
                    judge.judge([jid], max_calls=1, max_usd=0.05, enforce_daily=True)
                    g_row = connection.execute(
                        "SELECT verified_json FROM job_llm_verdicts WHERE job_id = ? AND source = 'gemini' ORDER BY id DESC LIMIT 1",
                        (jid,),
                    ).fetchone()
                    if g_row:
                        g_verdict = Verdict.from_json(json.loads(g_row["verified_json"]))
                        g_eff = effect_of(g_verdict, config.triage)
                        agrees = (action == "exclude" and g_eff.seniority == "exclude") or (
                            action == "hidden_location" and g_eff.location == "hidden"
                        )
                        if not agrees:
                            res.high_stakes_disagreements += 1
                            if not dry_run:
                                try:
                                    connection.execute(
                                        """INSERT INTO job_llm_disagreements (job_id, local_verdict, gemini_verdict, difference, resolved_by, created_at)
                                           VALUES (?, ?, ?, ?, 'gemini', ?)""",
                                        (
                                            jid,
                                            json.dumps(verdict.as_json() if hasattr(verdict, "as_json") else {}),
                                            json.dumps(g_verdict.as_json() if hasattr(g_verdict, "as_json") else {}),
                                            f"local proposed {item['action']} vs gemini {g_eff.seniority}/{g_eff.location}",
                                            now_iso,
                                        ),
                                    )
                                except Exception as e_dis:
                                    logger.warning("Could not insert disagreement for job %d: %s", jid, e_dis)
                            if g_eff.seniority == "none" and g_eff.location == "none":
                                action = "none"
                                reason = "Gemini high-stakes verdict disagreed: Gemini wins and keeps job in target"
                            else:
                                action = "maybe"
                                reason = f"Gemini high-stakes verdict disagreed: demoted to maybe"
                except Exception as err:
                    logger.warning("High-stakes confirmation failed for job %d: %s", jid, err)
                    action = "maybe"
                    reason += " (high-stakes check skipped: demoted to maybe)"
            else:
                action = "maybe"
                reason += " (demoted to maybe: high-stakes check skipped due to budget)"

        # Check blast-radius downgrade
        if res.blast_radius_triggered and action == "exclude":
            action = "maybe"
            reason += " [downgraded to maybe by blast-radius guard]"

        if res.blast_radius_triggered and action == "hidden_location":
            action = "advisory"
            reason += " [advisory chip only: blast-radius guard active]"

        # Counting actions
        if action == "exclude":
            res.excluded_seniority += 1
        elif action == "maybe":
            res.caution_seniority += 1
        elif action == "hidden_location":
            res.hidden_location += 1
        elif action == "blocker":
            res.blockers_applied += 1
        elif action in ("advisory", "blocker_preferred"):
            res.advisory_only += 1

        decision_rec = {
            "job_id": jid,
            "action": action,
            "reason": reason,
            "evidence": evidence,
            "strength_reason": strength_reason,
            "verdict_source": v_src,
            "company_rule_precedence": comp_precedence,
            "confidence": getattr(verdict, "confidence", "medium"),
        }
        res.decisions.append(decision_rec)

        # Apply persistence if not dry_run
        if not dry_run:
            if action == "exclude":
                connection.execute(
                    "UPDATE job_relevance SET gate_status = 'excluded', gate_rule = 'llm_seniority', gate_reason = ? WHERE job_id = ?",
                    (reason[:400], jid),
                )
            elif action == "maybe":
                connection.execute(
                    """
                    UPDATE job_relevance
                    SET gate_status = CASE WHEN gate_status = 'target' THEN 'maybe' ELSE gate_status END,
                        rank_adjust = rank_adjust + ?,
                        gate_rule = CASE WHEN gate_status = 'target' THEN 'llm_seniority_caution' ELSE gate_rule END,
                        gate_reason = CASE WHEN gate_status = 'target' THEN ? ELSE gate_reason END
                    WHERE job_id = ?
                    """,
                    (YEARS_PENALTY, reason[:400], jid),
                )
            elif action == "hidden_location":
                connection.execute(
                    "UPDATE job_relevance SET location_fit = 'wrong_location', location_reason = 'llm_location' WHERE job_id = ?",
                    (jid,),
                )
            elif action in ("blocker", "blocker_preferred"):
                blks = item.get("blockers") or []
                strength = "required" if action == "blocker" else "preferred"
                existing_json = connection.execute("SELECT eligibility_json FROM job_relevance WHERE job_id = ?", (jid,)).fetchone()
                cur_flags: list[dict[str, Any]] = []
                if existing_json and existing_json[0]:
                    try:
                        cur_flags = json.loads(existing_json[0])
                    except Exception:
                        pass
                seen_kinds = {f["kind"] for f in cur_flags}
                for bl in blks:
                    if bl not in seen_kinds:
                        cur_flags.append({"kind": bl, "strength": strength, "snippet": evidence[:200]})
                        seen_kinds.add(bl)
                connection.execute(
                    "UPDATE job_relevance SET eligibility_json = ? WHERE job_id = ?",
                    (json.dumps(cur_flags), jid),
                )

            # Record in job_cascade_decisions
            tier_val = 3 if v_src == "gemini" else 2
            model_val = "gemini-3.8-flash" if v_src == "gemini" else config.triage.local_llm_model
            mode_val = "active" if action in ("exclude", "maybe", "hidden_location", "blocker") else "advisory"
            connection.execute(
                """
                INSERT INTO job_cascade_decisions (
                    job_id, head, tier, action, confidence, evidence, mode, model, decided_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    jid,
                    "seniority" if action in ("exclude", "maybe") else ("location" if action == "hidden_location" else "eligibility"),
                    tier_val,
                    action,
                    0.95 if action in ("exclude", "hidden_location") else 0.75,
                    f"[{strength_reason}] {evidence}"[:200],
                    mode_val,
                    model_val,
                    now_iso,
                ),
            )

    if not dry_run:
        connection.commit()

    return res


__all__ = [
    "TriageApplicationResult",
    "apply_local_llm_triage",
    "check_strong_blockers",
    "check_strong_location",
    "check_strong_seniority",
    "get_visible_relevant_jobs",
    "is_company_rule_seniority_allowed",
    "load_all_exempt_job_ids",
]
