"""Disagreement audit import CLI and validation (Brief v10b Section 5).

Imports user audit verdicts from CSV, stores them as human labels (source 'audit', weight 5.0),
measures each model's accuracy against user answers, and updates the acting rules table.
"""

from __future__ import annotations

import csv
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.matching.triage_benchmark import ActingRuleStatus, recompute_acting_rules, wilson_interval
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

ALLOWED_VERDICTS = {"local_right", "gemini_right", "both_wrong", "unsure", "both_right"}


@dataclass
class AuditImportResult:
    total_rows: int = 0
    answered_rows: int = 0
    skipped_unsure: int = 0
    empty_rows: int = 0
    invalid_rows: int = 0
    imported_labels: int = 0
    gemini_correct: int = 0
    local_correct: int = 0
    gemini_accuracy: float = 0.0
    local_accuracy: float = 0.0
    acting_rules: list[ActingRuleStatus] | None = None
    errors: list[str] | None = None


def validate_and_import_audit_csv(
    connection: sqlite3.Connection,
    config: Config,
    csv_path: Path | str,
    *,
    clock: Clock = utc_now,
) -> AuditImportResult:
    """Validate and import audit CSV answers into job_audit_labels."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"Audit CSV file not found: {path}")

    res = AuditImportResult(errors=[])
    rows_to_import: list[dict[str, Any]] = []

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "job_id" not in fieldnames or "my_verdict" not in fieldnames:
            raise ValueError(f"CSV must contain 'job_id' and 'my_verdict' columns. Found: {fieldnames}")

        for line_no, r in enumerate(reader, start=2):
            res.total_rows += 1
            jid_str = (r.get("job_id") or "").strip()
            if not jid_str.isdigit():
                res.invalid_rows += 1
                res.errors.append(f"Line {line_no}: invalid job_id '{jid_str}'")
                continue

            jid = int(jid_str)
            my_v = (r.get("my_verdict") or "").strip().lower()
            my_note = (r.get("my_note") or "").strip()
            category = (r.get("category") or "").strip().lower()

            if not my_v:
                res.empty_rows += 1
                continue

            if my_v not in ALLOWED_VERDICTS:
                res.invalid_rows += 1
                res.errors.append(
                    f"Line {line_no} (job {jid}): invalid my_verdict '{my_v}'. "
                    f"Must be one of {', '.join(sorted(ALLOWED_VERDICTS))} or empty."
                )
                continue

            res.answered_rows += 1
            if my_v == "unsure":
                res.skipped_unsure += 1
                continue

            # Determine head and label
            head = "location" if "location" in category or "india" in category else "seniority"

            # Parse Gemini and Local predictions from the row
            gem_ver = (r.get("gemini_verdict") or "").lower()
            loc_ver = (r.get("local_verdict") or "").lower()

            if head == "seniority":
                # Senior / years >= 2 is positive (1.0), entry / < 2 is negative (0.0)
                g_is_senior = ("senior" in gem_ver or "lead" in gem_ver or "years=2" in gem_ver or "years=3" in gem_ver or "years=4" in gem_ver or "years=5" in gem_ver)
                l_is_senior = ("senior" in loc_ver or "lead" in loc_ver or "years=2" in loc_ver or "years=3" in loc_ver or "years=4" in loc_ver or "years=5" in loc_ver)

                if my_v == "gemini_right":
                    label = 1.0 if g_is_senior else 0.0
                    res.gemini_correct += 1
                elif my_v == "local_right":
                    label = 1.0 if l_is_senior else 0.0
                    res.local_correct += 1
                elif my_v == "both_wrong":
                    # If both predicted same, label is opposite
                    label = 0.0 if (g_is_senior or l_is_senior) else 1.0
                elif my_v == "both_right":
                    label = 1.0 if g_is_senior else 0.0
            else:
                # Location: not India is positive (1.0), India eligible is negative (0.0)
                g_not_india = "india=no" in gem_ver
                l_not_india = "india=no" in loc_ver

                if my_v == "gemini_right":
                    label = 1.0 if g_not_india else 0.0
                    res.gemini_correct += 1
                elif my_v == "local_right":
                    label = 1.0 if l_not_india else 0.0
                    res.local_correct += 1
                elif my_v == "both_wrong":
                    label = 0.0 if (g_not_india or l_not_india) else 1.0
                elif my_v == "both_right":
                    label = 1.0 if g_not_india else 0.0

            rows_to_import.append({
                "job_id": jid,
                "head": head,
                "label": label,
                "my_verdict": my_v,
                "my_note": my_note,
            })

    # Accuracy computation
    decisive_answers = res.gemini_correct + res.local_correct + sum(1 for r in rows_to_import if r["my_verdict"] == "both_wrong")
    if decisive_answers > 0:
        res.gemini_accuracy = round(res.gemini_correct / decisive_answers, 4)
        res.local_accuracy = round(res.local_correct / decisive_answers, 4)

    # Insert into database
    now_iso = to_iso(clock())
    for item in rows_to_import:
        connection.execute(
            """
            INSERT OR REPLACE INTO job_audit_labels (
                job_id, head, label, source, my_verdict, my_note, created_at
            ) VALUES (?, ?, ?, 'audit', ?, ?, ?)
            """,
            (item["job_id"], item["head"], item["label"], item["my_verdict"], item["my_note"], now_iso),
        )
        res.imported_labels += 1

    connection.commit()

    # Recompute acting rules with new labels
    res.acting_rules = recompute_acting_rules(connection, config, clock=clock)

    return res


def compute_deterministic_hint(category: str, snippet: str, title: str) -> str | None:
    """Deterministic hint based on unambiguous cues: years, countries, or title levels (never auto-applied)."""
    text = f"{snippet or ''} {title or ''}"

    # 1. Years phrase with number (e.g. 5+ years, 3-5 yrs)
    m_years = re.search(r"\b(\d+)\+?\s*(?:-\s*\d+\+?\s*)?(?:years?|yrs?)\b", text, re.IGNORECASE)
    if m_years:
        matched_str = m_years.group(0)
        num = int(m_years.group(1))
        if num >= 2:
            return f"Snippet contains '{matched_str}': supports years >= 2 (senior/lead)"
        return f"Snippet contains '{matched_str}': supports years < 2 (entry/mid)"

    # 2. Explicit US-only or country restriction
    m_loc = re.search(
        r"\b(US only|United States only|USA only|remote in US|remote - US|must reside in the US|must be based in the US|North America only)\b",
        text,
        re.IGNORECASE,
    )
    if m_loc:
        return f"Snippet contains '{m_loc.group(0)}': explicit US restriction (not India eligible)"

    m_india = re.search(
        r"\b(India|Bangalore|Bengaluru|Hyderabad|Pune|Mumbai|Delhi|Noida|Gurgaon)\b",
        text,
        re.IGNORECASE,
    )
    if m_india:
        return f"Snippet contains '{m_india.group(0)}': India location (India eligible)"

    # 3. Explicit title level words
    m_early = re.search(
        r"\b(intern|internship|graduate|new grad|entry level|junior|associate)\b",
        title or "",
        re.IGNORECASE,
    )
    if m_early:
        return f"Title contains '{m_early.group(0)}': early career / junior level"

    m_senior = re.search(
        r"\b(director|staff|principal|vice president|vp|head of)\b",
        title or "",
        re.IGNORECASE,
    )
    if m_senior:
        return f"Title contains '{m_senior.group(0)}': senior / lead level"

    return None


def ensure_audit_items_table(connection: sqlite3.Connection, config: Config) -> None:
    """Additive table storing the 60 candidate audit disagreement rows from v10b_audit.csv."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS triage_audit_items (
            job_id INTEGER PRIMARY KEY,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            snippet TEXT NOT NULL,
            gemini_verdict TEXT,
            gemini_quote TEXT,
            local_verdict TEXT,
            local_quote TEXT,
            deterministic_hint TEXT,
            row_order INTEGER NOT NULL
        )
    """)
    row_count = connection.execute("SELECT COUNT(*) FROM triage_audit_items").fetchone()[0]
    if row_count == 0:
        csv_path = config.resolve_path("data/eval/v10b_audit.csv")
        if csv_path.is_file():
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for order, r in enumerate(reader):
                    jid_str = (r.get("job_id") or "").strip()
                    if not jid_str.isdigit():
                        continue
                    jid = int(jid_str)
                    comp = (r.get("company") or "").strip()
                    title = (r.get("title") or "").strip()
                    src = (r.get("source") or "").strip()
                    cat = (r.get("category") or "").strip()
                    snip = (r.get("snippet") or "").strip()
                    gv = (r.get("gemini_verdict") or "").strip()
                    gq = (r.get("gemini_quote") or "").strip()
                    lv = (r.get("local_verdict") or "").strip()
                    lq = (r.get("local_quote") or "").strip()
                    hint = compute_deterministic_hint(cat, snip, title)
                    connection.execute("""
                        INSERT OR REPLACE INTO triage_audit_items (
                            job_id, company, title, source, category, snippet,
                            gemini_verdict, gemini_quote, local_verdict, local_quote,
                            deterministic_hint, row_order
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (jid, comp, title, src, cat, snip, gv, gq, lv, lq, hint, order))
            connection.commit()


def save_single_audit_label(
    connection: sqlite3.Connection,
    config: Config,
    job_id: int,
    my_verdict: str,
    my_note: str = "",
    *,
    clock: Clock = utc_now,
) -> None:
    """Store one audit verdict through the same code path and table as CSV import (weight 5.0, source 'audit')."""
    ensure_audit_items_table(connection, config)
    item = connection.execute("SELECT * FROM triage_audit_items WHERE job_id = ?", (job_id,)).fetchone()
    if not item:
        head = "seniority"
        label = 0.0
    else:
        cat = (item["category"] or "").lower()
        head = "location" if "location" in cat or "india" in cat else "seniority"
        gem_ver = (item["gemini_verdict"] or "").lower()
        loc_ver = (item["local_verdict"] or "").lower()

        if head == "seniority":
            g_is_senior = any(k in gem_ver for k in ("senior", "lead", "years=2", "years=3", "years=4", "years=5"))
            l_is_senior = any(k in loc_ver for k in ("senior", "lead", "years=2", "years=3", "years=4", "years=5"))
            if my_verdict == "gemini_right":
                label = 1.0 if g_is_senior else 0.0
            elif my_verdict == "local_right":
                label = 1.0 if l_is_senior else 0.0
            elif my_verdict == "both_wrong":
                label = 0.0 if (g_is_senior or l_is_senior) else 1.0
            elif my_verdict == "both_right":
                label = 1.0 if g_is_senior else 0.0
            else:  # unsure
                label = 0.0
        else:
            g_not_india = "india=no" in gem_ver
            l_not_india = "india=no" in loc_ver
            if my_verdict == "gemini_right":
                label = 1.0 if g_not_india else 0.0
            elif my_verdict == "local_right":
                label = 1.0 if l_not_india else 0.0
            elif my_verdict == "both_wrong":
                label = 0.0 if (g_not_india or l_not_india) else 1.0
            elif my_verdict == "both_right":
                label = 1.0 if g_not_india else 0.0
            else:
                label = 0.0

    now_iso = to_iso(clock())
    connection.execute("""
        INSERT OR REPLACE INTO job_audit_labels (
            job_id, head, label, source, my_verdict, my_note, created_at
        ) VALUES (?, ?, ?, 'audit', ?, ?, ?)
    """, (job_id, head, label, my_verdict, my_note, now_iso))
    connection.commit()

    recompute_acting_rules(connection, config, clock=clock)


def undo_single_audit_label(
    connection: sqlite3.Connection,
    config: Config,
    job_id: int,
    *,
    clock: Clock = utc_now,
) -> None:
    """Revert an audit label for a job."""
    connection.execute("DELETE FROM job_audit_labels WHERE job_id = ? AND source = 'audit'", (job_id,))
    connection.commit()
    recompute_acting_rules(connection, config, clock=clock)


def get_audit_review_state(connection: sqlite3.Connection, config: Config) -> dict[str, Any]:
    """Retrieve full audit review progress, items, accuracy metrics, and acting rules."""
    ensure_audit_items_table(connection, config)
    items_rows = connection.execute("""
        SELECT i.*, a.my_verdict, a.my_note, a.created_at as answered_at
        FROM triage_audit_items i
        LEFT JOIN job_audit_labels a ON a.job_id = i.job_id AND a.source = 'audit'
        ORDER BY i.row_order ASC
    """).fetchall()

    items: list[dict[str, Any]] = []
    gemini_correct = 0
    local_correct = 0
    both_wrong_count = 0
    both_right_count = 0
    unsure_count = 0
    answered_count = 0
    first_unanswered_idx = -1

    for idx, r in enumerate(items_rows):
        v = r["my_verdict"]
        if v:
            answered_count += 1
            if v == "gemini_right":
                gemini_correct += 1
            elif v == "local_right":
                local_correct += 1
            elif v == "both_wrong":
                both_wrong_count += 1
            elif v == "both_right":
                both_right_count += 1
            elif v == "unsure":
                unsure_count += 1
        elif first_unanswered_idx == -1:
            first_unanswered_idx = idx

        items.append({
            "job_id": r["job_id"],
            "company": r["company"],
            "title": r["title"],
            "source": r["source"],
            "category": r["category"],
            "snippet": r["snippet"],
            "gemini_verdict": r["gemini_verdict"],
            "gemini_quote": r["gemini_quote"],
            "local_verdict": r["local_verdict"],
            "local_quote": r["local_quote"],
            "deterministic_hint": r["deterministic_hint"],
            "row_order": r["row_order"],
            "my_verdict": r["my_verdict"],
            "my_note": r["my_note"],
        })

    decisive_answers = gemini_correct + local_correct + both_wrong_count
    gem_acc = round(gemini_correct / decisive_answers, 4) if decisive_answers > 0 else 0.0
    loc_acc = round(local_correct / decisive_answers, 4) if decisive_answers > 0 else 0.0
    gem_ci = list(wilson_interval(gemini_correct, decisive_answers)) if decisive_answers > 0 else [0.0, 0.0]
    loc_ci = list(wilson_interval(local_correct, decisive_answers)) if decisive_answers > 0 else [0.0, 0.0]

    try:
        rules_rows = connection.execute("SELECT * FROM acting_rules ORDER BY field, target_class").fetchall()
        acting_rules = [dict(r) for r in rules_rows]
    except sqlite3.OperationalError:
        acting_rules = []

    return {
        "items": items,
        "current_index": max(0, first_unanswered_idx if first_unanswered_idx != -1 else 0),
        "total_items": len(items),
        "answered_count": answered_count,
        "gemini_correct": gemini_correct,
        "local_correct": local_correct,
        "both_wrong_count": both_wrong_count,
        "both_right_count": both_right_count,
        "unsure_count": unsure_count,
        "decisive_count": decisive_answers,
        "gemini_accuracy": gem_acc,
        "gemini_ci": gem_ci,
        "local_accuracy": loc_acc,
        "local_ci": loc_ci,
        "acting_rules": acting_rules,
    }

