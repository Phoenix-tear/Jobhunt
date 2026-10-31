"""Measuring the gate and the ranking against labels only I can provide.

`eval-sample` writes a stratified sample of open jobs (target / maybe / excluded) to
`data/eval/gold.csv` with an empty `relevant` column. I fill it with yes / no by hand; the code
never fills labels itself. `eval-run` then reports gate precision and recall, ranking
precision@10 / @20, and lists the jobs the gate rejected although I marked them relevant.
"""

from __future__ import annotations

import csv
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from jobhunt.errors import JobHuntError

GOLD_COLUMNS = (
    "job_id", "title", "company", "location", "level", "employment_type", "workplace_type",
    "gate_status", "gate_rule", "gate_reason", "apply_url", "description_snippet", "relevant",
)
_YES = {"yes", "y", "1", "true", "relevant"}
_NO = {"no", "n", "0", "false", "irrelevant"}


class EvalError(JobHuntError):
    """The gold file is missing or unusable."""


# --- sampling ---------------------------------------------------------------------------------


def write_sample(
    connection: sqlite3.Connection, path: Path, *, size: int = 60, seed: int = 20261014, force: bool = False
) -> dict[str, int]:
    """Write a stratified sample (equal thirds; excluded jobs spread over the rules). Returns counts."""
    if path.exists() and not force:
        raise EvalError(f"{path} already exists (it may hold your labels); pass --force to overwrite it")
    rows = connection.execute(
        """SELECT j.id, j.title, j.company_name, j.location_raw, j.experience_level, j.employment_type,
                  j.workplace_type, j.canonical_apply_url, j.description_text,
                  r.gate_status, r.gate_rule, r.gate_reason
           FROM job_relevance r JOIN jobs j ON j.id = r.job_id WHERE j.status = 'open'"""
    ).fetchall()
    if not rows:
        raise EvalError("no judged jobs yet: run `relevance-run` first")

    rng = random.Random(seed)
    by_status: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_status[row["gate_status"]].append(row)

    quota = {"target": size // 3, "maybe": size // 3, "excluded": size - 2 * (size // 3)}
    chosen: list[sqlite3.Row] = []
    for status in ("target", "maybe", "excluded"):
        pool = by_status.get(status, [])
        rng.shuffle(pool)
        if status == "excluded":   # round-robin across rules so rare rules are represented
            by_rule: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for row in pool:
                by_rule[row["gate_rule"]].append(row)
            picked: list[sqlite3.Row] = []
            buckets = list(by_rule.values())
            while len(picked) < quota[status] and any(buckets):
                for bucket in buckets:
                    if bucket and len(picked) < quota[status]:
                        picked.append(bucket.pop())
            chosen.extend(picked)
        else:
            chosen.extend(pool[: quota[status]])

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=GOLD_COLUMNS)
        writer.writeheader()
        for row in chosen:
            snippet = " ".join((row["description_text"] or "").split())[:280]
            writer.writerow({
                "job_id": row["id"], "title": row["title"], "company": row["company_name"],
                "location": row["location_raw"] or "", "level": row["experience_level"],
                "employment_type": row["employment_type"], "workplace_type": row["workplace_type"],
                "gate_status": row["gate_status"], "gate_rule": row["gate_rule"], "gate_reason": row["gate_reason"],
                "apply_url": row["canonical_apply_url"] or "", "description_snippet": snippet, "relevant": "",
            })
    counts: dict[str, int] = defaultdict(int)
    for row in chosen:
        counts[row["gate_status"]] += 1
    return dict(counts)


# --- reporting --------------------------------------------------------------------------------


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None


@dataclass
class EvalReport:
    labeled: int = 0
    unlabeled: int = 0
    missing: int = 0
    relevant_yes: int = 0
    gate_target_or_maybe: Counts = field(default_factory=Counts)
    gate_target_only: Counts = field(default_factory=Counts)
    yes_rate_by_status: dict[str, tuple[int, int]] = field(default_factory=dict)   # status -> (yes, labeled)
    precision_at: dict[int, float | None] = field(default_factory=dict)
    ranked_labeled: int = 0
    false_rejections: list[dict[str, str]] = field(default_factory=list)
    false_accepts: list[dict[str, str]] = field(default_factory=list)


def _parse_label(value: str) -> bool | None:
    cleaned = (value or "").strip().lower()
    if cleaned in _YES:
        return True
    if cleaned in _NO:
        return False
    return None


def run_eval(connection: sqlite3.Connection, path: Path, *, at: tuple[int, ...] = (10, 20)) -> EvalReport:
    if not path.is_file():
        raise EvalError(f"{path} not found: run `eval-sample` first and fill the `relevant` column")
    report = EvalReport()
    with path.open(newline="", encoding="utf-8") as handle:
        records = list(csv.DictReader(handle))

    scored: list[tuple[float, bool]] = []
    per_status: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for record in records:
        label = _parse_label(record.get("relevant", ""))
        if label is None:
            report.unlabeled += 1
            continue
        row = connection.execute(
            """SELECT r.gate_status, r.gate_rule, r.gate_reason, r.final_score, r.similarity_ai, r.similarity_fullstack,
                      j.title, j.company_name
               FROM job_relevance r JOIN jobs j ON j.id = r.job_id WHERE r.job_id = ?""",
            (int(record["job_id"]),),
        ).fetchone()
        if row is None:
            report.missing += 1
            continue
        report.labeled += 1
        report.relevant_yes += int(label)
        status = row["gate_status"]
        per_status[status][1] += 1
        per_status[status][0] += int(label)

        for counts, predicted in (
            (report.gate_target_or_maybe, status in {"target", "maybe"}),
            (report.gate_target_only, status == "target"),
        ):
            if predicted and label:
                counts.tp += 1
            elif predicted and not label:
                counts.fp += 1
            elif not predicted and label:
                counts.fn += 1
            else:
                counts.tn += 1

        description = {"job_id": record["job_id"], "title": row["title"], "company": row["company_name"],
                       "rule": row["gate_rule"], "reason": row["gate_reason"]}
        if status == "excluded" and label:
            report.false_rejections.append(description)
        if status == "target" and not label:
            report.false_accepts.append(description)

        score = row["final_score"]
        if score is None:
            similarities = [value for value in (row["similarity_ai"], row["similarity_fullstack"]) if value is not None]
            score = max(similarities) * 100 if similarities else None
        if score is not None and status != "excluded":
            scored.append((float(score), label))

    report.yes_rate_by_status = {status: (yes, total) for status, (yes, total) in per_status.items()}
    scored.sort(key=lambda item: item[0], reverse=True)
    report.ranked_labeled = len(scored)
    for k in at:
        report.precision_at[k] = (sum(1 for _, label in scored[:k] if label) / k) if len(scored) >= k else None
    return report


def format_report(report: EvalReport) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:5.1f}%"

    lines = [
        f"labeled={report.labeled} (relevant yes={report.relevant_yes}), unlabeled={report.unlabeled}, "
        f"no longer in job_relevance={report.missing}",
        "",
        "Gate (predicted relevant = target or maybe):",
        f"  precision {pct(report.gate_target_or_maybe.precision)}   recall {pct(report.gate_target_or_maybe.recall)}"
        f"   (tp={report.gate_target_or_maybe.tp} fp={report.gate_target_or_maybe.fp} "
        f"fn={report.gate_target_or_maybe.fn} tn={report.gate_target_or_maybe.tn})",
        "Gate (predicted relevant = target only):",
        f"  precision {pct(report.gate_target_only.precision)}   recall {pct(report.gate_target_only.recall)}"
        f"   (tp={report.gate_target_only.tp} fp={report.gate_target_only.fp} "
        f"fn={report.gate_target_only.fn} tn={report.gate_target_only.tn})",
        "",
        "Share of labeled jobs I marked relevant, by gate status:",
    ]
    for status in ("target", "maybe", "excluded"):
        yes, total = report.yes_rate_by_status.get(status, (0, 0))
        lines.append(f"  {status:<9} {yes}/{total}")
    lines.append("")
    lines.append(f"Ranking (labeled, non-excluded, by final_score or similarity; n={report.ranked_labeled}):")
    for k, value in sorted(report.precision_at.items()):
        lines.append(f"  precision@{k}: {pct(value)}" + ("" if value is not None else f"  (needs {k} scored labeled jobs)"))
    lines.append("")
    lines.append(f"False rejections: {len(report.false_rejections)} (excluded by the gate but marked relevant)")
    for item in report.false_rejections:
        lines.append(f"  #{item['job_id']} {item['title']} | {item['company']} | {item['rule']}: {item['reason']}")
    lines.append(f"False accepts: {len(report.false_accepts)} (target but marked not relevant)")
    for item in report.false_accepts[:15]:
        lines.append(f"  #{item['job_id']} {item['title']} | {item['company']} | {item['rule']}")
    return "\n".join(lines)
