"""Read-only numbers about the LLM triage: the job panel's verdict block and the Health counters. No model calls."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from jobhunt.config import Config
from jobhunt.matching.triage import EVIDENCE_FIELDS, StoredVerdict, Verdict, effect_of, load_verdicts
from jobhunt.matching.triage_apply import gold_yes_ids
from jobhunt.timeutil import to_iso

LLM_RULES = ("llm_seniority", "llm_seniority_caution")
STUDENT_RULES = ("student_classifier",)
RULE_MINED = ("student_rule",)
LEAK_WINDOW = 100


def protection_reason(connection: sqlite3.Connection, config: Config, job_id: int) -> str | None:
    """Why a verdict may not move this job (None when it may)."""
    checks = (
        ("applied", "SELECT 1 FROM applications WHERE job_id = ?"),
        ("saved or applied", "SELECT 1 FROM job_decision_current WHERE job_id = ? AND action IN ('saved', 'applied')"),
        ("tailored resume", "SELECT 1 FROM tailored_resumes WHERE job_id = ?"),
        ("added by hand", "SELECT 1 FROM job_sightings WHERE job_id = ? AND source = 'manual'"),
    )
    for label, sql in checks:
        if connection.execute(sql, (job_id,)).fetchone():
            return label
    return "labelled relevant in the gold file" if job_id in gold_yes_ids(config) else None


def _quotes(verdict: Verdict) -> list[dict[str, str]]:
    rows = []
    if verdict.level_quote:
        rows.append({"field": "Level", "value": verdict.level, "quote": verdict.level_quote})
    if verdict.years_quote:
        rows.append({"field": "Years required", "value": f"{verdict.required_years_min}+", "quote": verdict.years_quote})
    if verdict.india_quote:
        rows.append({"field": "Open to India", "value": verdict.india_eligible, "quote": verdict.india_quote})
    if verdict.blocker_quote:
        rows.append({"field": "Blocker", "value": ", ".join(verdict.eligibility_blockers), "quote": verdict.blocker_quote})
    return rows


def triage_panel(connection: sqlite3.Connection, config: Config, job_id: int) -> dict[str, Any] | None:
    """The newest verdict of a job for the job panel: values, quotes, which rule used it, and whether it was held back."""
    cols = [r[1] for r in connection.execute("PRAGMA table_info(job_llm_verdicts)").fetchall()]
    has_source = "source" in cols
    source_expr = "v.source" if has_source else "'local_llm' AS source"

    row = connection.execute(
        f"SELECT v.id, v.verified_json, v.model, v.rubric_version, v.created_at, v.human_label, v.human_reason, {source_expr} "
        "FROM job_llm_verdicts v WHERE v.job_id = ? ORDER BY v.id DESC LIMIT 1",
        (job_id,)
    ).fetchone()
    relevance = connection.execute("SELECT * FROM job_relevance WHERE job_id = ?", (job_id,)).fetchone()
    student = None
    if relevance is not None:
        try:
            if relevance["triage_json"]:
                student = json.loads(relevance["triage_json"])
        except (IndexError, KeyError, ValueError):
            student = None
    cascade_items = []
    try:
        c_res = connection.execute(
            "SELECT head, tier, action, confidence, evidence, mode, model, decided_at "
            "FROM job_cascade_decisions WHERE job_id = ? ORDER BY id DESC",
            (job_id,)
        ).fetchall()
        for r in c_res:
            cascade_items.append({
                "head": r["head"],
                "tier": int(r["tier"]),
                "action": r["action"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "evidence": r["evidence"],
                "mode": r["mode"],
                "model": r["model"],
                "decided_at": r["decided_at"],
            })
    except sqlite3.OperationalError:
        pass

    if row is None and not student and not cascade_items:
        return None
    panel: dict[str, Any] = {"verdict": None, "student": student, "cascade": cascade_items}
    if row is not None:
        verdict = Verdict.from_json(json.loads(row["verified_json"]))
        effect = effect_of(verdict, config.triage)
        rule = ""
        if relevance is not None:
            try:
                rule = relevance["gate_rule"] or ""
            except (IndexError, KeyError):
                rule = ""
        protected = protection_reason(connection, config, job_id)
        used_by, used = "", ""
        if rule in LLM_RULES:
            used_by, used = rule, "this verdict moved the job"
        elif effect.location == "hidden":
            used_by, used = "llm_location", "this verdict hides the job by location when the posting itself is vague"
        elif effect.blockers:
            used_by, used = "eligibility_flag", "its blocker became an eligibility flag"
        if row["human_label"]:
            used_by, used = "", "you marked this verdict wrong, so it is ignored"
        elif protected and (effect.seniority != "none" or effect.location != "none"):
            used_by, used = "", f"held back: the job is {protected}, so a verdict never moves it"

        # Check model disagreement: head says senior with p >= 0.88, LLM says entry
        models_disagree = False
        try:
            head_p = connection.execute(
                "SELECT confidence FROM job_cascade_decisions WHERE job_id = ? AND tier = 1 AND head = 'seniority' ORDER BY id DESC LIMIT 1",
                (job_id,)
            ).fetchone()
            if head_p and head_p[0] is not None and float(head_p[0]) >= 0.88:
                lvl = verdict.level
                yrs = verdict.required_years_min
                if lvl in ("entry", "intern") or (yrs is not None and yrs == 0):
                    models_disagree = True
        except Exception:
            pass

        # Check company allow-rule precedence
        job_info = connection.execute("SELECT title, company_name, description_text FROM jobs WHERE id = ?", (job_id,)).fetchone()
        job_title = job_info["title"] or "" if job_info else ""
        job_company = job_info["company_name"] or "" if job_info else ""
        job_desc = job_info["description_text"] or "" if job_info else ""

        from jobhunt.matching.triage_apply_llm import is_company_rule_seniority_allowed, check_strong_seniority
        company_allows = is_company_rule_seniority_allowed(connection, config, job_id, job_title, job_company)
        company_precedence_applied = company_allows

        strength_reason = ""
        gate_reason = ""
        if relevance is not None:
            try:
                gate_reason = relevance["gate_reason"] or ""
            except (IndexError, KeyError):
                gate_reason = ""
        if "llm_seniority:" in gate_reason or "advisory:" in gate_reason or "llm_location:" in gate_reason:
            parts = gate_reason.split(": ", 1)
            if len(parts) > 1:
                strength_reason = parts[1]

        if not strength_reason:
            _, s_reason = check_strong_seniority(
                verdict, job_title, job_desc, None, None, company_allows
            )
            strength_reason = s_reason

        v_source = row["source"] if has_source else ("gemini" if "gemini" in (row["model"] or "").lower() else "local_llm")
        acted_or_advised = "acted" if used_by else "advised"
        acted = bool(used_by)

        panel["verdict"] = {
            "model": row["model"], "rubric_version": row["rubric_version"], "created_at": row["created_at"],
            "source": v_source,
            "acted_or_advised": acted_or_advised,
            "acted": acted,
            "strength_reason": strength_reason,
            "company_precedence_applied": company_precedence_applied,
            "models_disagree": models_disagree,
            "level": verdict.level, "required_years_min": verdict.required_years_min,
            "suitable_for_new_grad": verdict.suitable_for_new_grad, "india_eligible": verdict.india_eligible,
            "location_scope": verdict.location_scope, "eligibility_blockers": verdict.eligibility_blockers,
            "role_family": verdict.role_family, "confidence": verdict.confidence, "quotes": _quotes(verdict),
            "unverified_fields": verdict.hallucinated, "used_by": used_by, "used_text": used,
            "would_exclude": effect.seniority == "exclude" or effect.location == "hidden",
            "human_label": row["human_label"], "human_reason": row["human_reason"],
        }
    return panel


def _claimed(raw: dict[str, Any]) -> int:
    claims = 0
    claims += str(raw.get("level")) not in ("unknown", "None", "")
    years = raw.get("required_years_min")
    claims += isinstance(years, int) and not isinstance(years, bool) and years >= 0
    claims += str(raw.get("india_eligible")) in ("yes", "no")
    claims += bool(raw.get("eligibility_blockers"))
    return claims


def triage_health(connection: sqlite3.Connection, config: Config, now: datetime) -> dict[str, Any] | None:
    """Verdict counts, leak rate on the last 100 judged jobs, hallucinated-evidence rate, what was demoted this week,
    tier mix, calls saved, and head status."""
    try:
        rubric = config.triage.rubric_version
        rows = connection.execute(
            "SELECT id, job_id, verified_json, raw_json, hallucinated, human_label FROM job_llm_verdicts "
            "WHERE rubric_version = ? ORDER BY id DESC", (rubric,)).fetchall()
        week_ago = to_iso(now - timedelta(days=7))
        by_rule = {name: int(count) for name, count in connection.execute(
            "SELECT gate_rule, COUNT(*) FROM job_relevance WHERE computed_at >= ? AND gate_rule IN "
            "('llm_seniority','llm_seniority_caution','student_rule','student_classifier') GROUP BY 1", (week_ago,))}
        location_llm = int(connection.execute(
            "SELECT COUNT(*) FROM job_relevance WHERE location_reason LIKE 'llm_location:%' AND computed_at >= ?",
            (week_ago,)).fetchone()[0])
    except sqlite3.OperationalError:
        return None
    levels: dict[str, int] = {}
    seen: set[int] = set()
    latest: list[sqlite3.Row] = []
    for row in rows:
        if row["job_id"] in seen:
            continue
        seen.add(row["job_id"])
        latest.append(row)
    for row in latest:
        level = json.loads(row["verified_json"]).get("level", "unknown")
        levels[level] = levels.get(level, 0) + 1

    # Verdict counts by field / class
    verdict_counts_by_field: dict[str, dict[str, int]] = {
        "level": {"senior": 0, "mid": 0, "entry": 0, "intern": 0, "lead_or_manager": 0, "unknown": 0},
        "years": {"years_0": 0, "years_1": 0, "years_ge_2": 0, "unstated": 0},
        "location": {"india_yes": 0, "india_no": 0, "india_unclear": 0},
        "blockers": {"phd": 0, "masters": 0, "clearance": 0, "us_work_auth": 0, "none": 0},
    }
    for row in latest:
        try:
            v_data = json.loads(row["verified_json"])
            lvl = v_data.get("level", "unknown")
            verdict_counts_by_field["level"][lvl] = verdict_counts_by_field["level"].get(lvl, 0) + 1

            yrs = v_data.get("required_years_min")
            if yrs is None:
                verdict_counts_by_field["years"]["unstated"] += 1
            elif yrs == 0:
                verdict_counts_by_field["years"]["years_0"] += 1
            elif yrs == 1:
                verdict_counts_by_field["years"]["years_1"] += 1
            else:
                verdict_counts_by_field["years"]["years_ge_2"] += 1

            ind = v_data.get("india_eligible")
            if ind == "yes":
                verdict_counts_by_field["location"]["india_yes"] += 1
            elif ind == "no":
                verdict_counts_by_field["location"]["india_no"] += 1
            else:
                verdict_counts_by_field["location"]["india_unclear"] += 1

            blks = v_data.get("eligibility_blockers") or []
            if not blks:
                verdict_counts_by_field["blockers"]["none"] += 1
            else:
                for b in blks:
                    verdict_counts_by_field["blockers"][b] = verdict_counts_by_field["blockers"].get(b, 0) + 1
        except Exception:
            pass

    claimed = not_verbatim_count = irrelevant_count = 0
    for row in latest:
        try:
            claimed += _claimed(json.loads(row["raw_json"]))
        except ValueError:
            continue
        try:
            v_data = json.loads(row["verified_json"])
            not_verbatim_count += len(v_data.get("hallucinated", []))
            irrelevant_count += len(v_data.get("irrelevant_evidence", []))
        except ValueError:
            not_verbatim_count += int(row["hallucinated"])

    not_verbatim_rate = round(not_verbatim_count / claimed, 4) if claimed else None
    irrelevant_evidence_rate = round(irrelevant_count / claimed, 4) if claimed else None

    visible = {int(r[0]) for r in connection.execute(
        """SELECT j.id FROM jobs j JOIN job_relevance r ON r.job_id = j.id WHERE j.status = 'open'
           AND r.gate_status IN ('target', 'maybe')
           AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')""")}
    window = latest[:LEAK_WINDOW]
    leaked = 0
    acted = wrong = 0
    for row in latest:
        effect = effect_of(Verdict.from_json(json.loads(row["verified_json"])), config.triage)
        would_act = effect.seniority == "exclude" or effect.location == "hidden"
        if row in window and would_act and row["job_id"] in visible:
            leaked += 1
        if would_act:
            acted += 1
            wrong += 1 if row["human_label"] else 0

    tier_mix_7d = {"rules": 0, "classifier": 0, "local_llm": 0, "gemini": 0}
    try:
        for r in connection.execute(
            "SELECT tier, COUNT(*) FROM job_cascade_decisions WHERE decided_at >= ? GROUP BY tier",
            (week_ago,)
        ):
            name = {0: "rules", 1: "classifier", 2: "local_llm", 3: "gemini"}.get(int(r[0]), f"tier_{r[0]}")
            tier_mix_7d[name] = int(r[1])
    except sqlite3.OperationalError:
        pass

    gemini_calls_saved_7d = tier_mix_7d.get("rules", 0) + tier_mix_7d.get("classifier", 0) + tier_mix_7d.get("local_llm", 0)

    meta_path = config.database_file.parent / "models" / "triage_cascade_meta.json"
    if not meta_path.is_file():
        meta_path = config.database_file.parent / "models" / "triage_meta.json"
    last_trained_at = None
    last_train_metrics = {}
    saved_head_meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            last_trained_at = meta.get("trained_at")
            last_train_metrics = meta.get("metrics", {})
            saved_head_meta = meta.get("heads", {})
        except Exception:
            pass

    heads_status: dict[str, Any] = {}
    for head in ("seniority", "location", "eligibility"):
        head_cfg = getattr(config.triage.heads, head, None)
        mode = head_cfg.mode if head_cfg else "shadow"
        hm = saved_head_meta.get(head, {})
        n_pos = hm.get("n_pos", hm.get("n_positives", 0))
        n_neg = hm.get("n_neg", max(0, hm.get("n_samples", 0) - n_pos))
        precision = hm.get("precision", hm.get("grouped_cv_precision"))
        coverage = hm.get("coverage", hm.get("coverage_at_act"))
        auc = hm.get("auc", hm.get("grouped_cv_auc"))
        can_activate = hm.get("can_activate", False)
        needs_hint = hm.get("needs_hint")
        if not needs_hint and not can_activate:
            needed_pos = max(0, 30 - n_pos)
            needed_neg = max(0, 30 - n_neg)
            if needed_pos > 0 or needed_neg > 0:
                parts = []
                if needed_pos > 0:
                    parts.append(f"{needed_pos} more positive")
                if needed_neg > 0:
                    parts.append(f"{needed_neg} more negative")
                needs_hint = f"needs {' and '.join(parts)} labels"
            elif precision is not None and precision < 0.95:
                needs_hint = f"precision {precision * 100:.1f}% below 95% target"
            elif coverage is not None and coverage < 0.20:
                needs_hint = f"coverage {coverage * 100:.1f}% below 20% target"
            else:
                needs_hint = "needs model training"
        heads_status[head] = {
            "mode": mode,
            "n_pos": n_pos,
            "n_neg": n_neg,
            "precision": precision,
            "coverage": coverage,
            "auc": auc,
            "can_activate": can_activate,
            "needs_hint": needs_hint,
        }

    label_counts = {"human": 0, "verified": 0, "weak": 0, "total": 0}
    try:
        from jobhunt.matching.triage_labels import build_unified_labels
        labels_by_head = build_unified_labels(connection, config)
        total_unique = set()
        for head_labels in labels_by_head.values():
            for l in head_labels:
                total_unique.add((l.job_id, l.head))
                src = l.source
                if "human" in src:
                    label_counts["human"] += 1
                elif "verdict" in src or src in ("gemini", "local_llm"):
                    label_counts["verified"] += 1
                elif "weak" in src:
                    label_counts["weak"] += 1
        label_counts["total"] = len(total_unique)
    except Exception:
        pass

    shadow_summary: dict[str, Any] = {"demoted_count": 0}
    try:
        s_count = connection.execute(
            "SELECT COUNT(DISTINCT job_id) FROM job_cascade_decisions WHERE mode = 'shadow' AND action = 'demote'"
        ).fetchone()[0]
        shadow_summary["demoted_count"] = int(s_count)
    except sqlite3.OperationalError:
        pass

    # Coverage statistics (Brief v10b Section 1 & 7)
    from jobhunt.matching.triage_queue import get_triage_coverage
    cov = get_triage_coverage(connection, config)

    # Acting rules table (Brief v10b Section 4)
    acting_rules: list[dict[str, Any]] = []
    try:
        r_rows = connection.execute(
            """SELECT field, target_class, sample_count, high_conf_count,
                      precision_val, recall_val, can_act, mode, reason, evaluated_at,
                      ci_lower, ci_upper, rule_desc
               FROM job_triage_acting_rules
               ORDER BY field ASC, target_class ASC"""
        ).fetchall()
        for r in r_rows:
            acting_rules.append({
                "field": r["field"],
                "target_class": r["target_class"],
                "sample_count": int(r["sample_count"]),
                "high_conf_count": int(r["high_conf_count"]),
                "precision": float(r["precision_val"]),
                "recall": float(r["recall_val"]),
                "can_act": bool(r["can_act"]),
                "mode": r["mode"],
                "reason": r["reason"] or "",
                "evaluated_at": r["evaluated_at"],
                "ci_lower": float(r["ci_lower"]) if r["ci_lower"] is not None else None,
                "ci_upper": float(r["ci_upper"]) if r["ci_upper"] is not None else None,
                "rule_desc": r["rule_desc"] or "",
            })
    except sqlite3.OperationalError:
        pass

    # Last run duration and Gemini confirmations used
    last_run_duration: float | None = None
    last_jobhunt_triage_run: str | None = None
    try:
        lr_row = connection.execute(
            "SELECT started_at, duration_seconds FROM scheduled_runs WHERE task IN ('triage', 'jobhunt-triage') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if lr_row:
            last_jobhunt_triage_run = lr_row["started_at"]
            if lr_row["duration_seconds"] is not None:
                last_run_duration = float(lr_row["duration_seconds"])
    except sqlite3.OperationalError:
        pass

    gemini_confirmations = 0
    try:
        gc_row = connection.execute("SELECT COUNT(*) FROM job_llm_disagreements").fetchone()
        if gc_row:
            gemini_confirmations = int(gc_row[0])
    except sqlite3.OperationalError:
        pass

    # Models disagree count
    models_disagree_count = 0
    try:
        md_row = connection.execute(
            """
            SELECT COUNT(DISTINCT c.job_id)
            FROM job_cascade_decisions c
            JOIN job_llm_verdicts v ON v.job_id = c.job_id
            WHERE c.tier = 1 AND c.head = 'seniority' AND c.confidence >= 0.88
              AND (
                json_extract(v.verified_json, '$.level') IN ('entry', 'intern')
                OR json_extract(v.verified_json, '$.required_years_min') = 0
              )
            """
        ).fetchone()
        if md_row:
            models_disagree_count = int(md_row[0])
    except Exception:
        pass

    # Audit progress (Brief v10b Section 5 & 7)
    audit_imported = 0
    try:
        a_row = connection.execute("SELECT COUNT(DISTINCT job_id) FROM job_audit_labels").fetchone()
        if a_row:
            audit_imported = int(a_row[0])
    except sqlite3.OperationalError:
        pass
    audit_total = 60
    audit_progress = f"imported {audit_imported} of {audit_total}"

    return {
        "enabled": config.triage.enabled, "rubric_version": rubric, "model": config.triage.model,
        "verdicts": len(latest), "by_level": levels,
        "verdict_counts_by_field": verdict_counts_by_field,
        "leak_rate_last_100": round(leaked / len(window), 4) if window else None, "judged_in_window": len(window),
        "leaked_in_window": leaked,
        "hallucinated_evidence_rate": not_verbatim_rate,
        "not_verbatim_rate": not_verbatim_rate,
        "irrelevant_evidence_rate": irrelevant_evidence_rate,
        "hallucinated_fields": not_verbatim_count,
        "not_verbatim_fields": not_verbatim_count,
        "irrelevant_fields": irrelevant_count,
        "claimed_fields": claimed,
        "demoted_week_llm": by_rule.get("llm_seniority", 0) + by_rule.get("llm_seniority_caution", 0) + location_llm,
        "demoted_week_classifier": by_rule.get("student_classifier", 0),
        "demoted_week_rules": by_rule.get("student_rule", 0),
        "verdict_wrong_marked": wrong, "verdicts_that_acted": acted,
        "agreement": round(1 - wrong / acted, 4) if acted else None,
        "daily_calls_cap": config.budget.triage_daily_calls,
        "tier_mix_7d": tier_mix_7d,
        "gemini_calls_saved_7d": gemini_calls_saved_7d,
        "heads_status": heads_status,
        "last_trained_at": last_trained_at,
        "last_train_metrics": last_train_metrics,
        "label_counts": label_counts,
        "shadow_summary": shadow_summary,
        "coverage_visible_relevant_judged": cov["judged_relevant"],
        "coverage_visible_relevant_total": cov["total_relevant"],
        "coverage_visible_relevant_pct": cov["relevant_coverage_pct"],
        "coverage_gemini": cov.get("coverage_gemini", 0),
        "coverage_local_only": cov.get("coverage_local_only", 0),
        "coverage_none": cov.get("coverage_none", 0),
        "queue_length": cov["unjudged_queue_len"],
        "last_run_duration_seconds": last_run_duration,
        "last_jobhunt_triage_run": last_jobhunt_triage_run,
        "gemini_confirmations_used": gemini_confirmations,
        "models_disagree_count": models_disagree_count,
        "acting_rules": acting_rules,
        "audit_progress": audit_progress,
        "audit_imported": audit_imported,
        "audit_total": audit_total,
    }


def would_act_on(connection: sqlite3.Connection, config: Config, job_ids: set[int]) -> list[int]:
    """Of these jobs, the ones whose newest verdict would exclude or hide them (used for the gold-yes safety report)."""
    stored: dict[int, StoredVerdict] = load_verdicts(connection, config.triage.rubric_version)
    out = []
    for job_id in sorted(job_ids):
        item = stored.get(job_id)
        if item is None:
            continue
        effect = effect_of(item.verdict, config.triage)
        if effect.seniority == "exclude" or effect.location == "hidden":
            out.append(job_id)
    return out


__all__ = ["EVIDENCE_FIELDS", "protection_reason", "triage_panel", "triage_health", "would_act_on"]
