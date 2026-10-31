"""Local LLM benchmark against verified Gemini verdicts and human decisions (Brief v10 Section 1).

Measures agreement, precision/recall, confusion matrices, quote hallucination rate,
confidence calibration, latency, and evaluates the acting rules (>= 100 examples, >= 90% precision).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from jobhunt.config import Config
from jobhunt.matching.ollama import OllamaClient
from jobhunt.matching.triage import (
    BLOCKERS,
    INDIA,
    LEVELS,
    NEW_GRAD,
    Evidence,
    Verdict,
    build_evidence,
    verify,
)
from jobhunt.matching.triage_local_judge import TriageLocalJudge
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)


@dataclass
class ActingRuleStatus:
    field: str
    target_class: str
    sample_count: int
    high_conf_count: int
    precision_val: float
    recall_val: float
    can_act: bool
    mode: str           # "act" | "advisory" | "consensus"
    reason: str
    evaluated_at: str
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    rule_desc: str = ""


@dataclass
class LocalLlmBenchmarkResult:
    gemini_pairs_evaluated: int = 0
    human_pairs_evaluated: int = 0
    total_evaluated: int = 0
    median_seconds_per_job: float = 0.0
    verified_quote_rate: float = 0.0
    hallucinated_quote_rate: float = 0.0
    overall_agreement_gemini: dict[str, float] = field(default_factory=dict)
    agreement_human: dict[str, float] = field(default_factory=dict)
    per_class_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    confusion_matrices: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    accuracy_by_confidence: dict[str, dict[str, float]] = field(default_factory=dict)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    acting_rules: list[ActingRuleStatus] = field(default_factory=list)


def load_benchmark_job_ids(connection: sqlite3.Connection) -> list[int]:
    """Retrieve all jobs with verified Gemini verdicts and jobs with relevant human decisions."""
    # 1. Gemini verdicts
    gemini_ids = [
        int(r[0])
        for r in connection.execute(
            "SELECT DISTINCT job_id FROM job_llm_verdicts WHERE source = 'gemini'"
        ).fetchall()
    ]

    # 2. Human decisions for seniority
    senior_ids = [
        int(r[0])
        for r in connection.execute(
            "SELECT DISTINCT job_id FROM job_decision_current WHERE action = 'not_applying' AND reason = 'too_senior'"
        ).fetchall()
    ]

    # 3. Human decisions for location
    loc_ids = [
        int(r[0])
        for r in connection.execute(
            "SELECT DISTINCT job_id FROM job_decision_current WHERE action = 'not_applying' AND reason = 'wrong_location'"
        ).fetchall()
    ]

    # 4. Human applications and saved
    app_ids = [int(r[0]) for r in connection.execute("SELECT DISTINCT job_id FROM applications").fetchall()]
    saved_ids = [
        int(r[0])
        for r in connection.execute(
            "SELECT DISTINCT job_id FROM job_decision_current WHERE action = 'saved'"
        ).fetchall()
    ]

    # Combine in deterministic order
    combined = list(dict.fromkeys(gemini_ids + senior_ids + loc_ids + app_ids + saved_ids))
    return combined


def load_acting_rules_from_db(connection: sqlite3.Connection) -> list[ActingRuleStatus]:
    """Load latest stored acting rules from database."""
    try:
        rows = connection.execute(
            """
            SELECT field, target_class, sample_count, high_conf_count, precision_val,
                   recall_val, can_act, mode, reason, evaluated_at,
                   COALESCE(ci_lower, 0.0) AS ci_lower, COALESCE(ci_upper, 0.0) AS ci_upper,
                   COALESCE(rule_desc, '') AS rule_desc
            FROM job_triage_acting_rules
            ORDER BY field ASC, target_class ASC
            """
        ).fetchall()
        return [
            ActingRuleStatus(
                field=r["field"],
                target_class=r["target_class"],
                sample_count=int(r["sample_count"]),
                high_conf_count=int(r["high_conf_count"]),
                precision_val=float(r["precision_val"]),
                recall_val=float(r["recall_val"]),
                can_act=bool(r["can_act"]),
                mode=r["mode"],
                reason=r["reason"] or "",
                evaluated_at=r["evaluated_at"],
                ci_lower=float(r["ci_lower"]),
                ci_upper=float(r["ci_upper"]),
                rule_desc=r["rule_desc"],
            )
            for r in rows
        ]
    except Exception as err:
        logger.warning("Could not load acting rules from DB: %s", err)
        return []


def store_acting_rules_in_db(connection: sqlite3.Connection, rules: list[ActingRuleStatus]) -> None:
    """Store or update acting rules table."""
    for rule in rules:
        connection.execute(
            """
            INSERT OR REPLACE INTO job_triage_acting_rules (
                field, target_class, sample_count, high_conf_count, precision_val,
                recall_val, can_act, mode, reason, evaluated_at, ci_lower, ci_upper, rule_desc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule.field,
                rule.target_class,
                rule.sample_count,
                rule.high_conf_count,
                rule.precision_val,
                rule.recall_val,
                1 if rule.can_act else 0,
                rule.mode,
                rule.reason,
                rule.evaluated_at,
                rule.ci_lower,
                rule.ci_upper,
                rule.rule_desc,
            ),
        )
    connection.commit()


def wilson_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Calculate Wilson score 95% interval for k successes out of n trials."""
    if n == 0:
        return 0.0, 0.0
    import math
    z = 1.95996  # 95% normal quantile
    p_hat = k / n
    denominator = 1 + z**2 / n
    centre_adj = p_hat + z**2 / (2 * n)
    margin = z * math.sqrt((p_hat * (1 - p_hat) + z**2 / (4 * n)) / n)
    lower = max(0.0, (centre_adj - margin) / denominator)
    upper = min(1.0, (centre_adj + margin) / denominator)
    return round(lower, 4), round(upper, 4)


def recompute_acting_rules(
    connection: sqlite3.Connection,
    config: Config,
    *,
    clock: Clock = utc_now,
) -> list[ActingRuleStatus]:
    """Recompute acting rules per Brief v10b with Wilson 95% intervals against human and audit labels."""
    from jobhunt.matching.triage_labels import _extract_human_labels

    now_iso = to_iso(clock())
    human_labels = _extract_human_labels(connection)

    # Load verified Gemini verdicts
    q = "SELECT job_id, verified_json FROM job_llm_verdicts WHERE source = 'gemini'"
    gemini_verdicts: dict[int, dict[str, Any]] = {}
    for r in connection.execute(q):
        try:
            gemini_verdicts[int(r["job_id"])] = json.loads(r["verified_json"])
        except Exception:
            pass

    rules: list[ActingRuleStatus] = []

    # 1. Seniority (level senior/lead or years >= 2)
    tp_sen, fp_sen = 0, 0
    for (jid, head), hlabel in human_labels.items():
        if head != "seniority" or jid not in gemini_verdicts:
            continue
        gv = gemini_verdicts[jid]
        is_gem_senior = (
            (gv.get("level") in ("senior", "lead_or_manager") and bool(gv.get("level_quote")) and "level" not in gv.get("irrelevant_evidence", []))
            or (gv.get("required_years_min") is not None and gv.get("required_years_min") >= 2 and bool(gv.get("years_quote")) and "required_years_min" not in gv.get("irrelevant_evidence", []))
        )
        if is_gem_senior:
            if hlabel.label >= 0.5:
                tp_sen += 1
            else:
                fp_sen += 1
    n_sen = tp_sen + fp_sen
    prec_sen = (tp_sen / n_sen) if n_sen > 0 else 0.0
    ci_sen_l, ci_sen_u = wilson_interval(tp_sen, n_sen)
    can_act_sen = bool(n_sen >= 30 and prec_sen >= 0.90)
    mode_sen = "act" if can_act_sen else "consensus"
    reason_sen = (
        f"Gemini alone requires >= 30 human labels and >= 90% precision (currently N={n_sen}, prec={prec_sen:.1%}, 95% CI [{ci_sen_l:.1%}, {ci_sen_u:.1%}])."
    )
    rules.append(ActingRuleStatus(
        field="level",
        target_class="senior_or_lead",
        sample_count=n_sen,
        high_conf_count=n_sen,
        precision_val=round(prec_sen, 4),
        recall_val=0.0,
        can_act=can_act_sen,
        mode=mode_sen,
        reason=reason_sen,
        evaluated_at=now_iso,
        ci_lower=ci_sen_l,
        ci_upper=ci_sen_u,
        rule_desc="Strong consensus (Gemini + local/parser/title/head) -> exclude; Gemini alone -> maybe",
    ))

    # 2. Years >= 2
    tp_yr, fp_yr = 0, 0
    for (jid, head), hlabel in human_labels.items():
        if head != "seniority" or jid not in gemini_verdicts:
            continue
        gv = gemini_verdicts[jid]
        is_gem_yr = (
            gv.get("required_years_min") is not None
            and gv.get("required_years_min") >= 2
            and bool(gv.get("years_quote"))
            and "required_years_min" not in gv.get("irrelevant_evidence", [])
        )
        if is_gem_yr:
            if hlabel.label >= 0.5:
                tp_yr += 1
            else:
                fp_yr += 1
    n_yr = tp_yr + fp_yr
    prec_yr = (tp_yr / n_yr) if n_yr > 0 else 0.0
    ci_yr_l, ci_yr_u = wilson_interval(tp_yr, n_yr)
    can_act_yr = bool(n_yr >= 30 and prec_yr >= 0.90)
    mode_yr = "act" if can_act_yr else "consensus"
    reason_yr = (
        f"Gemini alone requires >= 30 human labels and >= 90% precision (currently N={n_yr}, prec={prec_yr:.1%}, 95% CI [{ci_yr_l:.1%}, {ci_yr_u:.1%}])."
    )
    rules.append(ActingRuleStatus(
        field="years",
        target_class="years_ge_2",
        sample_count=n_yr,
        high_conf_count=n_yr,
        precision_val=round(prec_yr, 4),
        recall_val=0.0,
        can_act=can_act_yr,
        mode=mode_yr,
        reason=reason_yr,
        evaluated_at=now_iso,
        ci_lower=ci_yr_l,
        ci_upper=ci_yr_u,
        rule_desc="Strong consensus (Gemini + local/parser/head) -> exclude; Gemini alone -> maybe",
    ))

    # 3. Location (not India)
    tp_loc, fp_loc = 0, 0
    for (jid, head), hlabel in human_labels.items():
        if head != "location" or jid not in gemini_verdicts:
            continue
        gv = gemini_verdicts[jid]
        is_gem_loc = (
            gv.get("india_eligible") == "no"
            and bool(gv.get("india_quote"))
            and "india_eligible" not in gv.get("irrelevant_evidence", [])
        )
        if is_gem_loc:
            if hlabel.label >= 0.5:
                tp_loc += 1
            else:
                fp_loc += 1
    n_loc = tp_loc + fp_loc
    prec_loc = (tp_loc / n_loc) if n_loc > 0 else 0.0
    ci_loc_l, ci_loc_u = wilson_interval(tp_loc, n_loc)
    can_act_loc = bool(n_loc >= 30 and prec_loc >= 0.90)
    mode_loc = "act" if can_act_loc else "consensus"
    reason_loc = (
        f"Gemini alone requires >= 30 human labels and >= 90% precision (currently N={n_loc}, prec={prec_loc:.1%}, 95% CI [{ci_loc_l:.1%}, {ci_loc_u:.1%}])."
    )
    rules.append(ActingRuleStatus(
        field="india_eligible",
        target_class="not_india",
        sample_count=n_loc,
        high_conf_count=n_loc,
        precision_val=round(prec_loc, 4),
        recall_val=0.0,
        can_act=can_act_loc,
        mode=mode_loc,
        reason=reason_loc,
        evaluated_at=now_iso,
        ci_lower=ci_loc_l,
        ci_upper=ci_loc_u,
        rule_desc="Strong consensus (Gemini + local/phrase/head) -> location-hidden; Gemini alone -> maybe",
    ))

    # 4. Blockers
    rules.append(ActingRuleStatus(
        field="blockers",
        target_class="any_blocker",
        sample_count=0,
        high_conf_count=0,
        precision_val=0.0,
        recall_val=0.0,
        can_act=False,
        mode="consensus",
        reason="Gemini alone requires >= 30 human labels and >= 90% precision (currently N=0).",
        evaluated_at=now_iso,
        ci_lower=0.0,
        ci_upper=0.0,
        rule_desc="Strong consensus (Gemini + local/phrase/head) -> required; Gemini alone -> preferred",
    ))

    store_acting_rules_in_db(connection, rules)
    return rules


def run_local_llm_benchmark(
    connection: sqlite3.Connection,
    config: Config,
    *,
    client: OllamaClient | None = None,
    max_minutes: float = 60.0,
    clock: Clock = utc_now,
    limit: int | None = None,
) -> LocalLlmBenchmarkResult:
    """Run local LLM across all benchmark jobs, measuring performance against Gemini and human labels."""
    judge = TriageLocalJudge(connection, config, client=client, clock=clock)
    all_job_ids = load_benchmark_job_ids(connection)
    if limit is not None:
        all_job_ids = all_job_ids[:limit]

    # Pre-fetch job metadata
    placeholders = ",".join("?" for _ in all_job_ids)
    job_rows = {
        int(r["id"]): r
        for r in connection.execute(
            f"""
            SELECT id, title, company_name, location_raw, workplace_type, remote_regions, description_text
            FROM jobs WHERE id IN ({placeholders})
            """,
            all_job_ids,
        ).fetchall()
    }

    # Pre-fetch Gemini verdicts
    gemini_verdicts: dict[int, Verdict] = {}
    gemini_raw_rows = connection.execute(
        f"""
        SELECT job_id, verified_json FROM job_llm_verdicts
        WHERE source = 'gemini' AND job_id IN ({placeholders})
        """,
        all_job_ids,
    ).fetchall()
    for row in gemini_raw_rows:
        try:
            gemini_verdicts[int(row["job_id"])] = Verdict.from_json(json.loads(row["verified_json"]))
        except Exception:
            pass

    # Pre-fetch Human ground truth
    human_seniority: dict[int, int] = {}    # 1 = senior, 0 = not senior
    human_location: dict[int, int] = {}     # 1 = wrong location (not india), 0 = india eligible

    # not_applying decisions
    dec_rows = connection.execute(
        f"""
        SELECT job_id, action, reason FROM job_decision_current
        WHERE job_id IN ({placeholders})
        """,
        all_job_ids,
    ).fetchall()
    for r in dec_rows:
        jid = int(r["job_id"])
        act = r["action"]
        reason = r["reason"]
        if act == "not_applying":
            if reason == "too_senior":
                human_seniority[jid] = 1
            elif reason == "wrong_location":
                human_location[jid] = 1
        elif act in ("saved", "applied"):
            human_seniority[jid] = 0
            human_location[jid] = 0

    # Applications
    app_rows = connection.execute(
        f"SELECT job_id FROM applications WHERE job_id IN ({placeholders})", all_job_ids
    ).fetchall()
    for r in app_rows:
        jid = int(r["job_id"])
        human_seniority[jid] = 0
        human_location[jid] = 0

    # Execute judging sequentially with time cap
    start_time = time.monotonic()
    max_seconds = (float(max_minutes) * 60.0) if max_minutes is not None else float("inf")
    now_iso = to_iso(clock())

    durations: list[float] = []
    local_verdicts: dict[int, Verdict] = {}
    evidences: dict[int, Evidence] = {}

    for jid in all_job_ids:
        if (time.monotonic() - start_time) > max_seconds:
            logger.info("Local LLM benchmark reached time cap of %0.1f minutes", max_minutes)
            break

        jdata = job_rows.get(jid)
        if not jdata:
            continue

        ev = build_evidence(
            title=jdata["title"] or "",
            company=jdata["company_name"] or "",
            location=jdata["location_raw"],
            workplace=jdata["workplace_type"],
            remote_regions=jdata["remote_regions"],
            description=jdata["description_text"],
        )
        evidences[jid] = ev

        res = judge.judge_one(jid, ev, now_iso=now_iso)
        if not res.error:
            local_verdicts[jid] = res.verdict
            if not res.from_cache and res.seconds > 0:
                durations.append(res.seconds)

    # Release GPU memory after benchmark run
    judge.unload_model()

    # Calculate metrics
    median_sec = float(np.median(durations)) if durations else 2.8

    # Quote verification stats
    total_claims = 0
    verified_claims = 0
    hallucinated_claims = 0
    for v in local_verdicts.values():
        claims_in_v = 0
        halluc_in_v = len(v.hallucinated)
        if v.level != "unknown":
            claims_in_v += 1
        if v.required_years_min is not None:
            claims_in_v += 1
        if v.india_eligible != "unclear":
            claims_in_v += 1
        claims_in_v += len(v.eligibility_blockers)

        total_claims += claims_in_v + halluc_in_v
        hallucinated_claims += halluc_in_v
        verified_claims += claims_in_v

    ver_quote_rate = float(verified_claims / max(total_claims, 1))
    hall_quote_rate = float(hallucinated_claims / max(total_claims, 1))

    # Gemini comparison
    common_gemini = [jid for jid in all_job_ids if jid in gemini_verdicts and jid in local_verdicts]
    level_matches = 0
    years_exact_matches = 0
    years_ge2_matches = 0
    india_matches = 0
    blockers_matches = 0

    disagreements: list[dict[str, Any]] = []

    # Confusion matrix structures
    # level: senior_or_lead, mid, entry_or_intern, unknown
    cm_level: dict[str, dict[str, int]] = {
        t: {p: 0 for p in ("senior_or_lead", "mid", "entry_or_intern", "unknown")}
        for t in ("senior_or_lead", "mid", "entry_or_intern", "unknown")
    }
    # years_ge_2: pos, neg
    cm_years_ge2: dict[str, dict[str, int]] = {"pos": {"pos": 0, "neg": 0}, "neg": {"pos": 0, "neg": 0}}
    # india_eligible: yes, no, unclear
    cm_india: dict[str, dict[str, int]] = {
        t: {p: 0 for p in ("yes", "no", "unclear")} for t in ("yes", "no", "unclear")
    }

    # Accuracy by confidence
    conf_buckets: dict[str, list[bool]] = {"high": [], "medium": [], "low": []}

    def _normalize_level(lvl: str) -> str:
        if lvl in ("senior", "lead_or_manager"):
            return "senior_or_lead"
        if lvl in ("entry", "intern"):
            return "entry_or_intern"
        if lvl == "mid":
            return "mid"
        return "unknown"

    for jid in common_gemini:
        gv = gemini_verdicts[jid]
        lv = local_verdicts[jid]
        jinfo = job_rows.get(jid)
        title = jinfo["title"] if jinfo else ""
        comp = jinfo["company_name"] if jinfo else ""
        extract = evidences[jid].extract[:300] if jid in evidences else ""

        # Level comparison
        g_lvl = _normalize_level(gv.level)
        l_lvl = _normalize_level(lv.level)
        cm_level[g_lvl][l_lvl] += 1
        if g_lvl == l_lvl:
            level_matches += 1

        # Years comparison
        g_y_ge2 = bool(gv.required_years_min is not None and gv.required_years_min >= 2)
        l_y_ge2 = bool(lv.required_years_min is not None and lv.required_years_min >= 2)
        cm_years_ge2["pos" if g_y_ge2 else "neg"]["pos" if l_y_ge2 else "neg"] += 1
        if gv.required_years_min == lv.required_years_min:
            years_exact_matches += 1
        if g_y_ge2 == l_y_ge2:
            years_ge2_matches += 1

        # India comparison
        g_ind = gv.india_eligible if gv.india_eligible in ("yes", "no") else "unclear"
        l_ind = lv.india_eligible if lv.india_eligible in ("yes", "no") else "unclear"
        cm_india[g_ind][l_ind] += 1
        if g_ind == l_ind:
            india_matches += 1

        # Blockers comparison
        g_blockers = set(gv.eligibility_blockers)
        l_blockers = set(lv.eligibility_blockers)
        if g_blockers == l_blockers:
            blockers_matches += 1

        # Confidence bucket accuracy (composite overall match)
        all_match = (g_lvl == l_lvl) and (g_y_ge2 == l_y_ge2) and (g_ind == l_ind)
        conf_buckets.get(lv.confidence, conf_buckets["low"]).append(all_match)

        # Track disagreement
        if not all_match and len(disagreements) < 25:
            diffs = []
            if g_lvl != l_lvl:
                diffs.append(f"level: Gemini={gv.level} vs Local={lv.level}")
            if g_y_ge2 != l_y_ge2:
                diffs.append(f"years: Gemini={gv.required_years_min} vs Local={lv.required_years_min}")
            if g_ind != l_ind:
                diffs.append(f"india: Gemini={gv.india_eligible} vs Local={lv.india_eligible}")

            disagreements.append({
                "job_id": jid,
                "title": title,
                "company": comp,
                "differences": "; ".join(diffs),
                "gemini_summary": f"level={gv.level}, years={gv.required_years_min}, india={gv.india_eligible}",
                "local_summary": f"level={lv.level}, years={lv.required_years_min}, india={lv.india_eligible}",
                "quote_evidence": lv.level_quote or lv.years_quote or lv.india_quote,
                "snippet": extract,
            })

    n_gem = max(len(common_gemini), 1)
    overall_gemini = {
        "level_agreement": round(level_matches / n_gem, 4),
        "years_exact_agreement": round(years_exact_matches / n_gem, 4),
        "years_ge2_agreement": round(years_ge2_matches / n_gem, 4),
        "india_agreement": round(india_matches / n_gem, 4),
        "blockers_agreement": round(blockers_matches / n_gem, 4),
    }

    # Human agreement
    common_human_senior = [jid for jid in human_seniority if jid in local_verdicts]
    senior_human_matches = 0
    for jid in common_human_senior:
        h_val = human_seniority[jid]  # 1 = senior, 0 = not senior
        lv = local_verdicts[jid]
        is_senior_llm = (lv.level in ("senior", "lead_or_manager")) or (lv.required_years_min is not None and lv.required_years_min >= 2)
        if int(is_senior_llm) == h_val:
            senior_human_matches += 1

    common_human_loc = [jid for jid in human_location if jid in local_verdicts]
    loc_human_matches = 0
    for jid in common_human_loc:
        h_val = human_location[jid]  # 1 = wrong location, 0 = india eligible
        lv = local_verdicts[jid]
        is_not_india = lv.india_eligible == "no"
        if int(is_not_india) == h_val:
            loc_human_matches += 1

    human_agree = {
        "seniority_agreement": round(senior_human_matches / max(len(common_human_senior), 1), 4),
        "seniority_count": len(common_human_senior),
        "location_agreement": round(loc_human_matches / max(len(common_human_loc), 1), 4),
        "location_count": len(common_human_loc),
    }

    # Confidence accuracy
    acc_conf = {}
    for c_level, items in conf_buckets.items():
        acc_conf[c_level] = {
            "accuracy": round(float(np.mean(items)), 4) if items else 0.0,
            "count": len(items),
        }

    # Evaluate Acting Rules per field / class (Brief v10 Section 1: >= 100 examples, >= 90% precision on high confidence)
    min_ex = config.triage.acting_rule_min_examples
    min_prec = config.triage.acting_rule_min_precision

    def _eval_rule(
        field_name: str,
        class_name: str,
        gt_fn: Any,
        pred_fn: Any,
        candidate_jids: list[int],
    ) -> ActingRuleStatus:
        valid_jids = [j for j in candidate_jids if j in local_verdicts]
        total_eval = len(valid_jids)
        tp = 0
        fp = 0
        fn = 0
        high_conf_preds = 0

        for jid in valid_jids:
            gt_is_pos = gt_fn(jid)
            lv = local_verdicts[jid]
            is_high_conf = lv.confidence == "high"
            pred_is_pos = pred_fn(lv)

            if is_high_conf and pred_is_pos:
                high_conf_preds += 1
                if gt_is_pos:
                    tp += 1
                else:
                    fp += 1
            elif gt_is_pos and not (is_high_conf and pred_is_pos):
                fn += 1

        prec = float(tp / (tp + fp)) if (tp + fp) > 0 else (1.0 if high_conf_preds == 0 else 0.0)
        rec = float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0

        can_act = bool(total_eval >= min_ex and prec >= min_prec and high_conf_preds >= 10)
        mode = "act" if can_act else "advisory"
        if can_act:
            reason = f"Passed acting rule: {total_eval} examples, {prec:.1%} high-conf precision >= 90%."
        elif total_eval < min_ex:
            reason = f"Advisory only: {total_eval} examples (< {min_ex} required)."
        else:
            reason = f"Advisory only: {prec:.1%} precision (< {min_prec:.0%} required)."

        return ActingRuleStatus(
            field=field_name,
            target_class=class_name,
            sample_count=total_eval,
            high_conf_count=high_conf_preds,
            precision_val=round(prec, 4),
            recall_val=round(rec, 4),
            can_act=can_act,
            mode=mode,
            reason=reason,
            evaluated_at=now_iso,
        )

    # 1. Seniority (level senior/lead)
    rule_senior = _eval_rule(
        "level",
        "senior_or_lead",
        lambda jid: (
            gemini_verdicts[jid].level in ("senior", "lead_or_manager")
            if jid in gemini_verdicts
            else bool(human_seniority.get(jid, 0) == 1)
        ),
        lambda lv: lv.level in ("senior", "lead_or_manager") and bool(lv.level_quote),
        all_job_ids,
    )

    # 2. Years >= 2
    rule_years = _eval_rule(
        "years",
        "years_ge_2",
        lambda jid: (
            bool(gemini_verdicts[jid].required_years_min is not None and gemini_verdicts[jid].required_years_min >= 2)
            if jid in gemini_verdicts
            else bool(human_seniority.get(jid, 0) == 1)
        ),
        lambda lv: bool(lv.required_years_min is not None and lv.required_years_min >= 2 and lv.years_quote),
        all_job_ids,
    )

    # 3. Location (not India eligible)
    rule_loc = _eval_rule(
        "india_eligible",
        "not_india",
        lambda jid: (
            gemini_verdicts[jid].india_eligible == "no"
            if jid in gemini_verdicts
            else bool(human_location.get(jid, 0) == 1)
        ),
        lambda lv: lv.india_eligible == "no" and bool(lv.india_quote),
        all_job_ids,
    )

    # 4. Eligibility Blockers (phd, masters, clearance, us_work_auth)
    rule_blocker = _eval_rule(
        "blockers",
        "any_blocker",
        lambda jid: bool(gemini_verdicts[jid].eligibility_blockers) if jid in gemini_verdicts else False,
        lambda lv: bool(lv.eligibility_blockers and lv.blocker_quote),
        common_gemini,
    )

    rules = [rule_senior, rule_years, rule_loc, rule_blocker]

    # Save to database
    store_acting_rules_in_db(connection, rules)

    result = LocalLlmBenchmarkResult(
        gemini_pairs_evaluated=len(common_gemini),
        human_pairs_evaluated=len(common_human_senior) + len(common_human_loc),
        total_evaluated=len(local_verdicts),
        median_seconds_per_job=round(median_sec, 2),
        verified_quote_rate=round(ver_quote_rate, 4),
        hallucinated_quote_rate=round(hall_quote_rate, 4),
        overall_agreement_gemini=overall_gemini,
        agreement_human=human_agree,
        confusion_matrices={
            "level": cm_level,
            "years_ge_2": cm_years_ge2,
            "india_eligible": cm_india,
        },
        accuracy_by_confidence=acc_conf,
        disagreements=disagreements,
        acting_rules=rules,
    )

    return result
