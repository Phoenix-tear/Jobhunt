"""Queries and actions behind the Resumes page (no model, no tailoring: these functions only read and label).

A resume version is a `tailored_resumes` row: global number R-0001..., per-job version v1..., a lifecycle
(draft / approved / sent / superseded), the change summary versus the base resume and the stored files.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.tailoring import changes, strength
from jobhunt.timeutil import to_iso, utc_now

LIFECYCLES = ("draft", "approved", "sent", "superseded")
MAX_ROWS = 500

_LIST_SQL = """
SELECT t.*, j.title AS job_title, j.company_name, j.canonical_apply_url,
       (SELECT s.source_url FROM job_sightings s WHERE s.job_id = j.id AND s.source_url IS NOT NULL
        ORDER BY CASE WHEN s.source IN ('greenhouse','lever','ashby','workable','smartrecruiters','recruitee',
                                        'workday','oracle','amazon_jobs','manual') THEN 0 ELSE 1 END, s.id LIMIT 1) AS sighting_url,
       a.id AS application_id, a.stage AS application_stage
FROM tailored_resumes t JOIN jobs j ON j.id = t.job_id
LEFT JOIN applications a ON a.tailored_resume_id = t.id
"""


def resume_code(number: int | None) -> str:
    return f"R-{int(number):04d}" if number else "R-????"


def _clean(text: str, limit: int) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:limit]


def download_filename(number: int | None, company: str, title: str, extension: str = "pdf") -> str:
    """`Resume_R0007_Company_Title.pdf` (ASCII only, no path characters)."""
    number_part = f"R{int(number):04d}" if number else "R0000"
    parts = ["Resume", number_part, _clean(company, 30) or "Company", _clean(title, 40) or "Role"]
    return "_".join(parts) + f".{extension}"


def _safe_url(value: str | None) -> str | None:
    from jobhunt.api.queries import safe_http_url

    return safe_http_url(value)


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def file_ok(path: str | None, config: Config) -> bool:
    if not path:
        return False
    resolved = Path(path).resolve()
    return config.resolve_path("data/tailored").resolve() in resolved.parents and resolved.is_file()


def row_to_item(row: sqlite3.Row, config: Config, current_profile_hash: str | None = None) -> dict[str, Any]:
    stale = bool(current_profile_hash and row["profile_hash"] and row["profile_hash"] != current_profile_hash)
    return {
        "id": row["id"], "resume_number": row["resume_number"], "code": resume_code(row["resume_number"]),
        "version": row["version"], "job_id": row["job_id"], "job_title": row["job_title"],
        "company_name": row["company_name"], "apply_url": _safe_url(row["canonical_apply_url"]) or _safe_url(row["sighting_url"]),
        "created_at": row["created_at"], "base": row["base"] or row["base_profile"], "recipe": row["recipe"],
        "summary_variant": row["summary_variant"], "fill_ratio": row["fill_ratio"],
        "page_count": row["page_count"] if row["page_count"] is not None else row["pages"],
        "validator_status": row["validator_status"], "advice_source": row["advice_source"] or "deterministic",
        "model": row["model"] or "", "cost_usd": row["cost_usd"] or 0.0, "run_status": row["status"],
        "lifecycle": row["lifecycle"], "sent_at": row["sent_at"], "archived": bool(row["archived"]),
        "has_pdf": file_ok(row["pdf_path"], config), "file_note": row["file_note"],
        "application_id": row["application_id"], "application_stage": row["application_stage"],
        "parent_resume_id": row["parent_resume_id"], "stale_profile": stale,
        "tailoring_strength": strength.tailoring_strength(_loads(row["change_summary_json"], None), config.tailoring),
    }


def current_profile_hash(config: Config) -> str | None:
    """Hash of the ticked facts right now (read-only: reads the profile files, never writes)."""
    from jobhunt.tailoring.engine import load_bases, ticks_hash

    try:
        return ticks_hash(load_bases(config))
    except Exception:  # noqa: BLE001 - a missing profile only means "staleness unknown"
        return None


@dataclass(frozen=True)
class ResumeFilters:
    company: str = ""
    lifecycle: str = ""
    base: str = ""
    since: str = ""                      # YYYY-MM-DD
    job_id: int | None = None
    include_superseded: bool = False
    include_archived: bool = False


def list_resumes(connection: sqlite3.Connection, config: Config, filters: ResumeFilters) -> dict[str, Any]:
    where, params = ["1 = 1"], []
    if filters.company:
        where.append("LOWER(j.company_name) LIKE ?")
        params.append(f"%{filters.company.lower()}%")
    if filters.lifecycle in LIFECYCLES:
        where.append("t.lifecycle = ?")
        params.append(filters.lifecycle)
    elif not filters.include_superseded:
        where.append("t.lifecycle != 'superseded'")
    if filters.base in ("ai", "fullstack"):
        where.append("COALESCE(t.base, t.base_profile) = ?")
        params.append(filters.base)
    if filters.since:
        where.append("substr(t.created_at, 1, 10) >= ?")
        params.append(filters.since[:10])
    if filters.job_id is not None:
        where.append("t.job_id = ?")
        params.append(filters.job_id)
    if not filters.include_archived:
        where.append("t.archived = 0")
    rows = connection.execute(f"{_LIST_SQL} WHERE {' AND '.join(where)} ORDER BY t.id DESC LIMIT {MAX_ROWS}",
                              params).fetchall()
    profile = current_profile_hash(config)
    counts = {row[0]: row[1] for row in connection.execute(
        "SELECT lifecycle, COUNT(*) FROM tailored_resumes WHERE archived = 0 GROUP BY lifecycle")}
    visible = sum(n for key, n in counts.items() if key != "superseded")
    return {"total": len(rows), "items": [row_to_item(r, config, profile) for r in rows],
            "counts": {**{k: counts.get(k, 0) for k in LIFECYCLES}, "visible": visible}}


def get_row(connection: sqlite3.Connection, resume_id: int) -> sqlite3.Row | None:
    return connection.execute(f"{_LIST_SQL} WHERE t.id = ?", (resume_id,)).fetchone()


def resume_detail(connection: sqlite3.Connection, config: Config, resume_id: int) -> dict[str, Any] | None:
    row = get_row(connection, resume_id)
    if row is None:
        return None
    item = row_to_item(row, config, current_profile_hash(config))
    summary = _loads(row["change_summary_json"], None) or dict(changes.NOT_RECORDED)
    previous = None
    if row["parent_resume_id"]:
        parent = connection.execute("SELECT version, change_summary_json FROM tailored_resumes WHERE id = ?",
                                    (row["parent_resume_id"],)).fetchone()
        if parent is not None:
            previous = changes.compare_versions(_loads(parent["change_summary_json"], None), summary,
                                                previous_version=parent["version"], current_version=row["version"])
    item.update({
        "change_summary": summary, "change_text": changes.render_text(summary), "diff_vs_previous": previous,
        "keywords_matched": _loads(row["keywords_matched_json"], []), "keywords_missing": _loads(row["keywords_missing_json"], []),
        "validation": _loads(row["validation_json"], {}), "message": row["message"] or "",
        "dropped": _loads(row["dropped_json"], []), "has_tex": file_ok(row["tex_path"], config),
        "pdf_path": row["pdf_path"], "download_name": download_filename(row["resume_number"], row["company_name"], row["job_title"]),
        "how_made": how_made(row), "profile_hash_recorded": row["profile_hash"] is not None,
    })
    return item


def how_made(row: sqlite3.Row) -> str:
    """One plain sentence on how this version was produced. The model never writes resume text."""
    if (row["advice_source"] or "deterministic") == "gemini":
        text = f"Gemini advised ({row['model']}, ${float(row['cost_usd'] or 0):.3f})"
    else:
        reason = (row["message"] or "").lower()
        if "budget" in reason or "interactive limit" in reason or "cap" in reason:
            why = "over budget"
        elif row["status"] == "fallback" or (row["model"] and row["status"] != "ok"):
            why = "model failed or its plan was rejected"
        else:
            why = "user chose"
        text = f"deterministic path (no model; reason: {why})"
    return text + ". The resume text is never written by the model: it only picks from your own approved text."


# --- actions ------------------------------------------------------------------------------------------------------


def set_lifecycle_after_approval(connection: sqlite3.Connection, job_id: int) -> None:
    """Keep `lifecycle` in step with the `approved` flag: approved stays approved, other live drafts stay drafts."""
    connection.execute(
        "UPDATE tailored_resumes SET lifecycle = 'approved' WHERE job_id = ? AND approved = 1 AND lifecycle NOT IN ('sent')",
        (job_id,))
    connection.execute(
        """UPDATE tailored_resumes SET lifecycle = CASE
               WHEN id = (SELECT MAX(b.id) FROM tailored_resumes b WHERE b.job_id = tailored_resumes.job_id)
               THEN 'draft' ELSE 'superseded' END
           WHERE job_id = ? AND approved = 0 AND lifecycle = 'approved'""", (job_id,))


def mark_sent(connection: sqlite3.Connection, resume_id: int) -> dict[str, Any]:
    """Set `sent_at`, lifecycle `sent`, and attach it to the job's tracker entry (a Saved entry is created if none)."""
    row = connection.execute("SELECT id, job_id, status FROM tailored_resumes WHERE id = ?", (resume_id,)).fetchone()
    if row is None:
        raise LookupError("resume not found")
    if row["status"] == "failed":
        raise ValueError("a failed version cannot be marked as sent")
    now = to_iso(utc_now())
    existing = connection.execute("SELECT id FROM applications WHERE job_id = ?", (row["job_id"],)).fetchone()
    if existing is None:
        cursor = connection.execute(
            "INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (?, 'saved', ?, ?)",
            (row["job_id"], now, now))
        application_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO application_stage_history (application_id, from_stage, to_stage, changed_at) VALUES (?, NULL, 'saved', ?)",
            (application_id, now))
    else:
        application_id = int(existing["id"])
    connection.execute("UPDATE applications SET tailored_resume_id = ?, updated_at = ? WHERE id = ?",
                       (resume_id, now, application_id))
    connection.execute("UPDATE tailored_resumes SET lifecycle = 'sent', sent_at = ? WHERE id = ?", (now, resume_id))
    return {"application_id": application_id, "sent_at": now}


def set_archived(connection: sqlite3.Connection, resume_id: int, archived: bool) -> None:
    if connection.execute("SELECT 1 FROM tailored_resumes WHERE id = ?", (resume_id,)).fetchone() is None:
        raise LookupError("resume not found")
    connection.execute("UPDATE tailored_resumes SET archived = ? WHERE id = ?", (1 if archived else 0, resume_id))


def tailor_estimate(connection: sqlite3.Connection, config: Config, job_id: int) -> dict[str, Any]:
    """What a (re-)tailor click would cost, from past runs, plus the interactive counter. Makes no call."""
    from jobhunt.matching.budget import BudgetGuard, UsageLedger

    average = connection.execute(
        "SELECT AVG(cost_usd) FROM tailored_resumes WHERE advice_source = 'gemini' AND cost_usd > 0").fetchone()[0]
    estimate = round(float(average), 4) if average else 0.005
    ledger = UsageLedger(connection, config.pricing)
    summary = ledger.summary()
    decision = BudgetGuard(ledger, config.budget).check_generate(estimate, interactive=True)
    existing = connection.execute(
        "SELECT COUNT(*) FROM tailored_resumes WHERE job_id = ? AND archived = 0", (job_id,)).fetchone()[0]
    model = config.tailoring.model if config.tailoring.llm_enabled else ""
    return {
        "job_id": job_id, "model": model, "estimated_usd": estimate if (model and decision) else 0.0,
        "interactive_calls_today": summary.interactive_calls_today,
        "interactive_call_cap": config.budget.interactive_daily_calls, "blocked": not decision,
        "message": "" if decision else decision.reason, "existing_versions": existing,
        "will_use_model": bool(model and decision),
    }


# --- backfill of change summaries (idempotent; never touches a file) ---------------------------------------------


def backfill_change_summaries(connection: sqlite3.Connection, config: Config) -> int:
    """Fill `change_summary_json` for rows that have none. Rows whose files were overwritten by a later run only get
    what the stored plan still says (marked partial); the rest are diffed from their own `.tex` against the base."""
    rows = connection.execute(
        "SELECT id, base_profile, plan_json, tex_path, pdf_path, pages, dropped_json, file_note "
        "FROM tailored_resumes WHERE change_summary_json IS NULL").fetchall()
    if not rows:
        return 0
    from jobhunt.tailoring.engine import load_bases
    from jobhunt.tailoring.summaries import CLOSING_CLAUSES
    from jobhunt.tailoring.texdoc import parse

    try:
        bases = load_bases(config)
    except Exception:  # noqa: BLE001 - without the base resumes every row gets the partial form
        bases = None
    raw_to_fact = {f.raw: f.id for f in bases.merged} if bases else {}
    done = 0
    for row in rows:
        plan = _loads(row["plan_json"], {})
        base = row["base_profile"]
        summary: dict[str, Any]
        if row["file_note"]:
            summary = changes.from_plan_only(plan, base_name=base, pages=row["pages"] or 0,
                                             reason="not fully recorded: the files of this run were overwritten by a later run")
        else:
            try:
                tex = Path(row["tex_path"]).read_text(encoding="utf-8") if row["tex_path"] else ""
                if not tex or bases is None or base not in bases.docs:
                    raise ValueError("files or base resume missing")
                summary = changes.compute_change_summary(
                    bases.docs[base], parse(tex), plan, raw_to_fact, base_name=base,
                    closing_clauses=tuple(CLOSING_CLAUSES.values()), dropped_to_fit=_loads(row["dropped_json"], []),
                    pages=row["pages"] or 0)
            except (OSError, ValueError, KeyError):
                summary = changes.from_plan_only(plan, base_name=base, pages=row["pages"] or 0,
                                                 reason="not fully recorded: the source files are missing")
        connection.execute("UPDATE tailored_resumes SET change_summary_json = ? WHERE id = ?",
                           (json.dumps(summary), row["id"]))
        done += 1
    return done
