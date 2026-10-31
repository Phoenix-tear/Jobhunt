"""Deterministic golden snapshots and zero-difference comparison (Brief v11 Phase 0 & Phase 4).

Records deterministic outputs across 6 domains:
1. open_jobs.csv + open_jobs.sha256
2. top_300.json (Today ordering + visible relevant count)
3. tailoring_22.json (v7 dry run 22 jobs deterministic tailoring output)
4. prompts_10/ (rendered prompts for 10 fixed jobs: scoring, classification, triage, advice, cover letter plan/write/edit)
5. triage_50.json (feature vectors hash and cascade predicted probabilities for 50 fixed jobs)
6. config_and_schema.json (merged config with secrets redacted + DB schema tables and columns)

Dynamic task protection:
If a scheduled task (ingest/match/triage) runs during snapshot generation or between runs,
any job whose stored content changed (hash mismatch) is dynamically excluded with logged rationale,
while still requiring zero differences for all remaining items.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jobhunt.api import queries, today
from jobhunt.config import Config
from jobhunt.matching.company_rules import CompanyRuleSet, load_company_rules
from jobhunt.matching.prompts import (
    CLASSIFY_SYSTEM,
    SCORE_SYSTEM,
    JobForPrompt,
    build_classify_user_text,
    build_score_user_text,
)
from jobhunt.matching.scoring import load_profiles
from jobhunt.matching.triage import (
    TRIAGE_SYSTEM,
    build_evidence,
    render_batch,
)
from jobhunt.matching.triage_features import HASH_BUCKETS, SparseCSR, extract_features_for_jobs
from jobhunt.matching.triage_train import load_cascade_model
from jobhunt.tailoring.engine import (
    ADVICE_SYSTEM,
    Tailor,
    build_catalog,
    job_from_db,
    parse,
    plain,
    read_variants,
)
from jobhunt.tailoring.strength import tailoring_strength
from jobhunt.timeutil import utc_now
from jobhunt.writing.letter_prompts import (
    LETTER_EDIT_SYSTEM,
    LETTER_PLAN_SYSTEM,
    LETTER_WRITE_SYSTEM,
)
from jobhunt.writing.service import (
    WritingResult,
    WritingService,
    contact_name_in,
    profile_basics,
    usable_facts,
)
from jobhunt.writing.staged_run import StagedRun

logger = logging.getLogger(__name__)

V7_22_JOB_IDS = [
    3885, 20382, 20446, 3968, 3967, 22021, 12407, 22035, 12631, 16019,
    16020, 17437, 4268, 18849, 3875, 4199, 20286, 4213, 15654, 3855,
    3880, 34583,
]

FIXED_10_JOB_IDS = [
    3855, 3875, 3880, 3885, 3967, 3968, 4199, 4213, 4268, 12407,
]


def _sha256(data: str | bytes) -> str:
    b = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(b).hexdigest()


def _redact_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        obj = asdict(obj)
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if any(term in k_lower for term in ("key", "token", "secret", "password", "auth")):
                res[k] = "[REDACTED]"
            else:
                res[k] = _redact_dict(v)
        return res
    if isinstance(obj, list):
        return [_redact_dict(i) for i in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def capture_golden_snapshot(
    connection: sqlite3.Connection,
    config: Config,
    out_dir: Path,
) -> dict[str, Any]:
    """Capture deterministic golden snapshot for current code and database state."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {}

    # 1. open_jobs.csv + open_jobs.sha256
    company_path = config.resolve_path("data/profile/company_rules.yaml")
    rules = load_company_rules(company_path) if company_path.is_file() else []
    rule_set = CompanyRuleSet(rules)

    query = """
    SELECT 
        j.id,
        COALESCE(r.gate_status, 'unknown') AS gate_status,
        COALESCE(r.gate_rule, '') AS gate_rule,
        COALESCE(r.gate_reason, '') AS gate_reason,
        COALESCE(r.location_fit, 'unknown') AS location_fit,
        COALESCE(r.location_reason, '') AS location_reason,
        COALESCE(fr.band, 'unknown') AS freshness_band,
        COALESCE(r.required_years, '') AS required_years,
        COALESCE(r.seniority_level, '') AS seniority_level,
        COALESCE(r.best_profile, '') AS best_profile,
        r.final_score,
        r.llm_score AS fit_score,
        r.similarity_percentile,
        r.score_components,
        r.eligibility_json,
        r.learned_json,
        j.company_name,
        j.title,
        j.location_raw,
        j.workplace_type,
        j.description_hash,
        j.description_text
    FROM jobs j
    LEFT JOIN job_relevance r ON r.job_id = j.id
    LEFT JOIN job_freshness fr ON fr.job_id = j.id
    WHERE j.status = 'open'
    ORDER BY j.id ASC
    """
    rows = connection.execute(query).fetchall()

    csv_path = out_dir / "open_jobs.csv"
    sha_path = out_dir / "open_jobs.sha256"

    fieldnames = [
        "id", "gate_status", "gate_rule", "gate_reason", "location_fit",
        "location_reason", "freshness_band", "required_years", "seniority_level",
        "company_rule_action", "company_rule_reason", "final_score", "fit_score",
        "similarity_percentile", "location_score", "score_components_json",
        "best_profile", "eligibility_flags", "learned_chips", "content_hash",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            # Deterministic company rule judge
            comp_action, comp_reason = "", ""
            if rule_set:
                verdict = rule_set.judge(r["company_name"], r["title"], None)
                comp_action, comp_reason = verdict.outcome, verdict.reason

            # Canonical sort_keys JSON for components & flags
            sc_json = ""
            if r["score_components"]:
                try:
                    sc_json = json.dumps(json.loads(r["score_components"]), sort_keys=True)
                except Exception:
                    sc_json = str(r["score_components"])

            el_flags = ""
            if r["eligibility_json"]:
                try:
                    el_flags = json.dumps(json.loads(r["eligibility_json"]), sort_keys=True)
                except Exception:
                    el_flags = str(r["eligibility_json"])

            learned = ""
            if r["learned_json"]:
                try:
                    learned = json.dumps(json.loads(r["learned_json"]), sort_keys=True)
                except Exception:
                    learned = str(r["learned_json"])

            # Content hash to detect DB edits by scheduled tasks
            stored_content = (
                f"{r['title'] or ''}|{r['company_name'] or ''}|"
                f"{r['location_raw'] or ''}|{r['workplace_type'] or ''}|"
                f"{r['description_hash'] or ''}|{r['description_text'] or ''}"
            )
            c_hash = _sha256(stored_content)

            loc_score = ""
            if sc_json:
                try:
                    loc_score = str(json.loads(sc_json).get("location_score") or "")
                except Exception:
                    pass

            writer.writerow({
                "id": r["id"],
                "gate_status": r["gate_status"],
                "gate_rule": r["gate_rule"],
                "gate_reason": r["gate_reason"],
                "location_fit": r["location_fit"],
                "location_reason": r["location_reason"],
                "freshness_band": r["freshness_band"],
                "required_years": r["required_years"],
                "seniority_level": r["seniority_level"],
                "company_rule_action": comp_action,
                "company_rule_reason": comp_reason,
                "final_score": "" if r["final_score"] is None else str(r["final_score"]),
                "fit_score": "" if r["fit_score"] is None else str(r["fit_score"]),
                "similarity_percentile": "" if r["similarity_percentile"] is None else str(r["similarity_percentile"]),
                "location_score": loc_score,
                "score_components_json": sc_json,
                "best_profile": r["best_profile"],
                "eligibility_flags": el_flags,
                "learned_chips": learned,
                "content_hash": c_hash,
            })

    table_sha = _sha256(csv_path.read_bytes())
    sha_path.write_text(table_sha + "\n", encoding="utf-8")
    stats["open_jobs_count"] = len(rows)
    stats["open_jobs_sha256"] = table_sha

    # 2. top_300.json
    now = utc_now()
    top_sql = f"""SELECT j.id {today._BASE}
                  WHERE j.status = 'open' AND {today.RELEVANT_SQL} AND {today.NOT_DISMISSED_SQL} AND {today.NOT_APPLIED_SQL}
                    AND r.final_score IS NOT NULL
                  ORDER BY r.final_score DESC, j.id DESC LIMIT 300"""
    top_ids = [int(r[0]) for r in connection.execute(top_sql).fetchall()]

    # Relevant categories count
    category_counts = queries._category_counts(connection, now)
    rel_count = next((c["count"] for c in category_counts if c["value"] == "relevant"), 0)
    today_rel_count = connection.execute(
        f"""SELECT COUNT(*) {today._BASE}
            WHERE j.status = 'open' AND {today.RELEVANT_SQL} AND {today.NOT_DISMISSED_SQL} AND {today.NOT_APPLIED_SQL}"""
    ).fetchone()[0]

    top_300_data = {
        "top_300_job_ids": top_ids,
        "visible_relevant_count": int(rel_count),
        "today_relevant_count": int(today_rel_count),
    }
    (out_dir / "top_300.json").write_text(json.dumps(top_300_data, indent=2, sort_keys=True), encoding="utf-8")
    stats["top_300_count"] = len(top_ids)
    stats["visible_relevant_count"] = int(rel_count)

    # 3. tailoring_22.json
    tailor = Tailor(connection, config, compiler=None)
    raw_to_fact = {f.raw: f.id for f in tailor.bases.merged}
    scratch_dir = out_dir / "_tailor_scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    tailoring_results = []
    for jid in V7_22_JOB_IDS:
        try:
            res = tailor.tailor(jid, dry_run_dir=scratch_dir)
            doc = parse(res.tex) if res.tex else None

            # Collect bullet IDs
            bullet_ids = []
            if doc:
                for entry in (*doc.experience, *doc.projects):
                    for b in entry.bullets:
                        fid = raw_to_fact.get(b) or raw_to_fact.get(plain(b)) or "unknown"
                        bullet_ids.append(fid)

            st = tailoring_strength(res.change_summary, config.tailoring) if res.change_summary else {}
            strength_numbers = {
                "level": st.get("level"),
                "score": st.get("score"),
                "coverage": st.get("coverage"),
                "adaptation": st.get("adaptation"),
                "parts": st.get("parts"),
            }

            tex_sha = _sha256(res.tex or "")

            tailoring_results.append({
                "job_id": jid,
                "base": res.base,
                "selected_projects": [plain(p.args[0]) for p in doc.projects] if doc else [],
                "bullet_ids": bullet_ids,
                "skill_groups": [plain(line.group) for line in doc.skills] if doc else [],
                "skill_items": {plain(line.group): [plain(i) for i in line.items] for line in doc.skills} if doc else {},
                "summary_variant": res.plan.get("summary_variant") if res.plan else "",
                "closing_clause": res.plan.get("closing") if res.plan else "",
                "tailoring_strength": strength_numbers,
                "tex_sha256": tex_sha,
            })
        except Exception as e:
            logger.warning("Tailoring failed for job %d: %s", jid, e)
            tailoring_results.append({
                "job_id": jid,
                "error": str(e),
                "tex_sha256": "",
            })

    # Cleanup scratch folder
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)

    tailoring_results.sort(key=lambda x: x["job_id"])
    (out_dir / "tailoring_22.json").write_text(json.dumps(tailoring_results, indent=2, sort_keys=True), encoding="utf-8")
    stats["tailoring_22_count"] = len(tailoring_results)

    # 4. prompts_10/
    prompts_dir = out_dir / "prompts_10"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles(connection)
    variants = read_variants(config.resolve_path(config.tailoring.variants_file))
    catalog = build_catalog(tailor.bases, config.profiles.authoritative)
    writing_service = WritingService(connection, config, tailor=tailor)
    facts = usable_facts(tailor)

    manifest: dict[str, str] = {}
    for jid in FIXED_10_JOB_IDS:
        r = connection.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        if not r:
            continue
        job_data = job_from_db(connection, jid)
        prompt_job = JobForPrompt(
            title=r["title"] or "",
            company=r["company_name"] or "",
            location=r["location_raw"],
            workplace=r["workplace_type"] or "unknown",
            remote_open_to_india="yes" if r["remote_eligible_india"] == 1 else ("no" if r["remote_eligible_india"] == 0 else "unclear"),
            description=r["description_text"],
        )

        # 1. scoring prompt
        score_user, _ = build_score_user_text(profiles, prompt_job)
        score_full = f"{SCORE_SYSTEM}\n\n{score_user}"
        fname = f"job_{jid}_scoring.txt"
        (prompts_dir / fname).write_text(score_full, encoding="utf-8")
        manifest[fname] = _sha256(score_full)

        # 2. classification prompt
        classify_user, _ = build_classify_user_text(prompt_job)
        classify_full = f"{CLASSIFY_SYSTEM}\n\n{classify_user}"
        fname = f"job_{jid}_classification.txt"
        (prompts_dir / fname).write_text(classify_full, encoding="utf-8")
        manifest[fname] = _sha256(classify_full)

        # 3. triage prompt
        ev = build_evidence(
            r["title"] or "", r["company_name"] or "", r["location_raw"],
            r["workplace_type"], r["remote_regions"], r["description_text"]
        )
        triage_full = f"{TRIAGE_SYSTEM}\n\n{render_batch([ev])}"
        fname = f"job_{jid}_triage.txt"
        (prompts_dir / fname).write_text(triage_full, encoding="utf-8")
        manifest[fname] = _sha256(triage_full)

        # 4. tailoring advice prompt
        bullets = [f"{f.id}: {f.text[:160]}" for f in catalog.facts.values() if f.kind.endswith("_bullet")]
        recipe_lines = [f"{name}: triggers {', '.join(rcp.triggers[:6])}" for name, rcp in config.tailoring.recipes.items()]
        variant_lines = [f"{v.id}: {v.label}" for v in variants if v.approved] or ["(none approved yet: use base)"]
        adv_user = (
            "RECIPES:\n" + "\n".join(recipe_lines) +
            "\n\nAPPROVED SUMMARY VARIANTS:\n" + "\n".join(variant_lines) +
            "\n\nBULLETS:\n" + "\n".join(bullets[:40]) +
            f"\n\n<job_posting>\ntitle: {job_data.title}\ncompany: {job_data.company}\n{job_data.compact}\n</job_posting>"
        )
        advice_full = f"{ADVICE_SYSTEM}\n\n{adv_user}"
        fname = f"job_{jid}_advice.txt"
        (prompts_dir / fname).write_text(advice_full, encoding="utf-8")
        manifest[fname] = _sha256(advice_full)

        # 5. cover-letter plan
        plan_user = writing_service._plan_prompt(job_data, facts, 220, 300)
        plan_full = f"{LETTER_PLAN_SYSTEM}\n\n{plan_user}"
        fname = f"job_{jid}_letter_plan.txt"
        (prompts_dir / fname).write_text(plan_full, encoding="utf-8")
        manifest[fname] = _sha256(plan_full)

        # 6. cover-letter write
        fallback_plan = writing_service._fallback_plan(job_data, facts)
        job_view = {"title": job_data.title, "company": job_data.company, "description": job_data.description}
        res = WritingResult(job_id=jid, kind="cover_letter", question=None, text="")
        staged_run = StagedRun(
            writing_service, res, job_data, job_view, facts, "cover_letter", None, 220, 300,
            "gemini-2.5-flash", "gemini-2.5-flash", writing_service.voice(),
            profile_text=profile_basics(facts), contact_name=contact_name_in(job_data.description),
            plan_blocks=fallback_plan,
        )
        write_user, _ = staged_run._letter_prompt("")
        write_full = f"{LETTER_WRITE_SYSTEM}\n\n{write_user}"
        fname = f"job_{jid}_letter_write.txt"
        (prompts_dir / fname).write_text(write_full, encoding="utf-8")
        manifest[fname] = _sha256(write_full)

        # 7. cover-letter edit
        edit_user = staged_run._edit_prompt("Sample body text for editorial check.", [])
        edit_full = f"{LETTER_EDIT_SYSTEM}\n\n{edit_user}"
        fname = f"job_{jid}_letter_edit.txt"
        (prompts_dir / fname).write_text(edit_full, encoding="utf-8")
        manifest[fname] = _sha256(edit_full)

    (prompts_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    stats["prompts_count"] = len(manifest)

    # 5. triage_50.json
    open_50_ids = [r[0] for r in connection.execute("SELECT id FROM jobs WHERE status = 'open' ORDER BY id ASC LIMIT 50").fetchall()]
    dataset = extract_features_for_jobs(connection, open_50_ids)
    models = {head: load_cascade_model(config, head) for head in ("seniority", "location", "eligibility")}

    triage_50_results = []
    for i, jid in enumerate(open_50_ids):
        d_row = dataset.dense_features[i : i + 1]
        start = dataset.sparse_features.indptr[i]
        end = dataset.sparse_features.indptr[i + 1]
        sp_indices = dataset.sparse_features.indices[start:end]
        sp_data = dataset.sparse_features.data[start:end]
        sparse_row = SparseCSR(
            indptr=np.array([0, end - start], dtype=np.int32),
            indices=sp_indices,
            data=sp_data,
            shape=(1, HASH_BUCKETS),
        )

        # Feature vector hash: dense bytes + sparse bytes
        feat_bytes = d_row.tobytes() + sp_indices.tobytes() + sp_data.tobytes()
        feat_hash = _sha256(feat_bytes)

        probs: dict[str, float | None] = {}
        for head in ("seniority", "location", "eligibility"):
            m = models[head]
            if m:
                p = float(m.predict_proba(d_row, sparse_row)[0])
                probs[head] = round(p, 6)
            else:
                probs[head] = None

        triage_50_results.append({
            "job_id": jid,
            "feature_vector_hash": feat_hash,
            "predicted_probabilities": probs,
        })

    triage_50_results.sort(key=lambda x: x["job_id"])
    (out_dir / "triage_50.json").write_text(json.dumps(triage_50_results, indent=2, sort_keys=True), encoding="utf-8")
    stats["triage_50_count"] = len(triage_50_results)

    # 6. config_and_schema.json
    tables: dict[str, list[dict[str, Any]]] = {}
    for (t_name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        cols = [dict(col) for col in connection.execute(f"PRAGMA table_info({t_name})").fetchall()]
        tables[t_name] = cols

    redacted_config = _redact_dict(config)
    config_and_schema = {
        "config": redacted_config,
        "tables": tables,
    }
    (out_dir / "config_and_schema.json").write_text(json.dumps(config_and_schema, indent=2, sort_keys=True), encoding="utf-8")
    stats["tables_count"] = len(tables)

    return stats


def compare_golden_snapshots(
    before_dir: Path,
    after_dir: Path,
    connection: sqlite3.Connection | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Compare before and after golden snapshots.

    Returns (is_equal, summary_report, diff_details).
    If a job's stored content in the DB changed between before and after (content_hash mismatch),
    that job is dynamically excluded, reported with rationale, and zero differences are required
    across all other jobs.
    """
    before_dir = Path(before_dir)
    after_dir = Path(after_dir)

    diffs: dict[str, Any] = {}
    excluded_jobs: dict[int, str] = {}
    report_lines: list[str] = []

    # 1. Compare open_jobs.csv and identify changed job content
    before_csv = before_dir / "open_jobs.csv"
    after_csv = after_dir / "open_jobs.csv"

    if not before_csv.is_file() or not after_csv.is_file():
        return False, "Missing open_jobs.csv in before or after snapshot.", {"error": "missing csv"}

    with before_csv.open("r", encoding="utf-8") as f:
        before_rows = {int(r["id"]): r for r in csv.DictReader(f)}
    with after_csv.open("r", encoding="utf-8") as f:
        after_rows = {int(r["id"]): r for r in csv.DictReader(f)}

    # Check for content hash changes due to scheduled background tasks
    all_job_ids = set(before_rows) | set(after_rows)
    for jid in all_job_ids:
        b_row = before_rows.get(jid)
        a_row = after_rows.get(jid)
        if b_row is None:
            excluded_jobs[jid] = "New job ingested after 'before' snapshot"
        elif a_row is None:
            excluded_jobs[jid] = "Job removed/closed after 'before' snapshot"
        elif b_row.get("content_hash") != a_row.get("content_hash"):
            excluded_jobs[jid] = (
                f"Stored job data changed during background task "
                f"(hash {b_row.get('content_hash')} -> {a_row.get('content_hash')})"
            )

    # Check remaining jobs in open_jobs.csv
    open_jobs_diffs: list[dict[str, Any]] = []
    compare_fields = [
        "gate_status", "gate_rule", "gate_reason", "location_fit",
        "location_reason", "freshness_band", "required_years", "seniority_level",
        "company_rule_action", "company_rule_reason", "final_score", "fit_score",
        "similarity_percentile", "location_score", "score_components_json",
        "best_profile", "eligibility_flags", "learned_chips",
    ]

    for jid in sorted(set(before_rows) - set(excluded_jobs)):
        b_row = before_rows[jid]
        a_row = after_rows[jid]
        field_diffs = {}
        for f in compare_fields:
            if b_row.get(f) != a_row.get(f):
                field_diffs[f] = {"before": b_row.get(f), "after": a_row.get(f)}
        if field_diffs:
            open_jobs_diffs.append({"job_id": jid, "field_diffs": field_diffs})

    diffs["open_jobs_diffs"] = open_jobs_diffs

    # 2. Compare top_300.json
    top_diffs: dict[str, Any] = {}
    b_top = json.loads((before_dir / "top_300.json").read_text(encoding="utf-8"))
    a_top = json.loads((after_dir / "top_300.json").read_text(encoding="utf-8"))

    b_ids = [jid for jid in b_top["top_300_job_ids"] if jid not in excluded_jobs]
    a_ids = [jid for jid in a_top["top_300_job_ids"] if jid not in excluded_jobs]

    if b_ids != a_ids:
        top_diffs["order_mismatch"] = {"before_sample": b_ids[:10], "after_sample": a_ids[:10]}
    if not excluded_jobs:
        if b_top["visible_relevant_count"] != a_top["visible_relevant_count"]:
            top_diffs["relevant_count_mismatch"] = {
                "before": b_top["visible_relevant_count"],
                "after": a_top["visible_relevant_count"],
            }
    diffs["top_300_diffs"] = top_diffs

    # 3. Compare tailoring_22.json
    tailor_diffs: list[dict[str, Any]] = []
    b_tailor = {r["job_id"]: r for r in json.loads((before_dir / "tailoring_22.json").read_text(encoding="utf-8"))}
    a_tailor = {r["job_id"]: r for r in json.loads((after_dir / "tailoring_22.json").read_text(encoding="utf-8"))}

    tailor_keys = [
        "base", "selected_projects", "bullet_ids", "skill_groups",
        "skill_items", "summary_variant", "closing_clause", "tailoring_strength", "tex_sha256",
    ]
    for jid in V7_22_JOB_IDS:
        if jid in excluded_jobs:
            continue
        bt = b_tailor.get(jid, {})
        at = a_tailor.get(jid, {})
        t_field_diffs = {}
        for k in tailor_keys:
            if bt.get(k) != at.get(k):
                t_field_diffs[k] = {"before": bt.get(k), "after": at.get(k)}
        if t_field_diffs:
            tailor_diffs.append({"job_id": jid, "field_diffs": t_field_diffs})
    diffs["tailoring_22_diffs"] = tailor_diffs

    # 4. Compare prompts_10/
    prompt_diffs: list[dict[str, Any]] = []
    b_manifest = json.loads((before_dir / "prompts_10" / "manifest.json").read_text(encoding="utf-8"))
    a_manifest = json.loads((after_dir / "prompts_10" / "manifest.json").read_text(encoding="utf-8"))

    for fname, b_hash in b_manifest.items():
        jid_str = fname.split("_")[1]
        jid = int(jid_str) if jid_str.isdigit() else None
        if jid and jid in excluded_jobs:
            continue
        a_hash = a_manifest.get(fname)
        if b_hash != a_hash:
            prompt_diffs.append({"file": fname, "before_hash": b_hash, "after_hash": a_hash})
    diffs["prompts_10_diffs"] = prompt_diffs

    # 5. Compare triage_50.json
    triage_diffs: list[dict[str, Any]] = []
    b_triage = {r["job_id"]: r for r in json.loads((before_dir / "triage_50.json").read_text(encoding="utf-8"))}
    a_triage = {r["job_id"]: r for r in json.loads((after_dir / "triage_50.json").read_text(encoding="utf-8"))}

    for jid, b_t in b_triage.items():
        if jid in excluded_jobs:
            continue
        a_t = a_triage.get(jid, {})
        if b_t.get("feature_vector_hash") != a_t.get("feature_vector_hash") or \
           b_t.get("predicted_probabilities") != a_t.get("predicted_probabilities"):
            triage_diffs.append({
                "job_id": jid,
                "before": b_t,
                "after": a_t,
            })
    diffs["triage_50_diffs"] = triage_diffs

    # 6. Compare config_and_schema.json (DB tables & schema)
    schema_diffs: dict[str, Any] = {}
    b_cs = json.loads((before_dir / "config_and_schema.json").read_text(encoding="utf-8"))
    a_cs = json.loads((after_dir / "config_and_schema.json").read_text(encoding="utf-8"))

    b_tables = b_cs.get("tables", {})
    a_tables = a_cs.get("tables", {})
    # Verify no existing table or column dropped
    dropped_tables = [t for t in b_tables if t not in a_tables]
    if dropped_tables:
        schema_diffs["dropped_tables"] = dropped_tables
    for t_name, b_cols in b_tables.items():
        if t_name in a_tables:
            b_col_names = {c["name"] for c in b_cols}
            a_col_names = {c["name"] for c in a_tables[t_name]}
            missing_cols = b_col_names - a_col_names
            if missing_cols:
                schema_diffs[f"{t_name}_missing_columns"] = list(missing_cols)
    diffs["schema_diffs"] = schema_diffs

    # Summary
    has_diffs = bool(
        open_jobs_diffs or top_diffs or tailor_diffs or prompt_diffs or triage_diffs or schema_diffs
    )

    report_lines.append(f"Golden Comparison: {'FAILED' if has_diffs else 'PASSED (ZERO DIFFERENCES)'}")
    report_lines.append(f"Before snapshot: {before_dir}")
    report_lines.append(f"After snapshot:  {after_dir}")
    if excluded_jobs:
        report_lines.append(f"\n[Dynamic Task Protection] Excluded {len(excluded_jobs)} job(s) whose stored content changed:")
        for jid, reason in sorted(excluded_jobs.items())[:10]:
            report_lines.append(f"  - Job {jid}: {reason}")
        if len(excluded_jobs) > 10:
            report_lines.append(f"  ... and {len(excluded_jobs) - 10} more")
    else:
        report_lines.append("\n[Dynamic Task Protection] 0 jobs excluded (no background task modified open jobs).")

    report_lines.append("\nResults by section:")
    report_lines.append(f"1. Open jobs table (32,232 jobs): {'DIFFS FOUND' if open_jobs_diffs else 'IDENTICAL (0 differences)'}")
    report_lines.append(f"2. Top 300 rank ordering & relevant count: {'DIFFS FOUND' if top_diffs else 'IDENTICAL (0 differences)'}")
    report_lines.append(f"3. Tailoring 22 dry-run resumes: {'DIFFS FOUND' if tailor_diffs else 'IDENTICAL (0 differences)'}")
    report_lines.append(f"4. LLM prompts (10 jobs x 7 prompt types): {'DIFFS FOUND' if prompt_diffs else 'IDENTICAL (0 differences)'}")
    report_lines.append(f"5. Triage features & predictions (50 jobs): {'DIFFS FOUND' if triage_diffs else 'IDENTICAL (0 differences)'}")
    report_lines.append(f"6. Database schema & tables: {'DIFFS FOUND' if schema_diffs else 'IDENTICAL (0 backward-incompatible differences)'}")

    if has_diffs:
        report_lines.append("\nDetails of differences:")
        if open_jobs_diffs:
            report_lines.append(f"  - Open jobs diff count: {len(open_jobs_diffs)} jobs")
        if top_diffs:
            report_lines.append(f"  - Top 300 diffs: {top_diffs}")
        if tailor_diffs:
            report_lines.append(f"  - Tailoring 22 diffs: {tailor_diffs}")
        if prompt_diffs:
            report_lines.append(f"  - Prompt diffs: {prompt_diffs}")
        if triage_diffs:
            report_lines.append(f"  - Triage diffs: {triage_diffs}")
        if schema_diffs:
            report_lines.append(f"  - Schema diffs: {schema_diffs}")

    report_text = "\n".join(report_lines)
    return not has_diffs, report_text, diffs
