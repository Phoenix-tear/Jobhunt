"""Learning from my decisions: label counts, the ranker's state, suggested rules (Approve / Reject) and active rules.

Nothing here applies a rule by itself and nothing calls a model. Approved rules live in `data/profile/learned_rules.yaml`
(see matching/learned_rules.py for the limits) and take effect at the next `relevance-run`.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from jobhunt.api.database import ConfigDep, ReadDb, WriteDb
from jobhunt.db import transaction
from jobhunt.decisions import ROLE_FIT_NEGATIVE_REASONS
from jobhunt.matching import learned_rules, ranker
from jobhunt.matching.rule_suggestions import suggest
from jobhunt.timeutil import to_iso, utc_now

router = APIRouter(prefix="/api", tags=["learning"])

HONESTY = ("Only decisions you made yourself teach the ranker what you like; Gemini scores and gold labels fill in the "
           "rest. With few of your own decisions the learned signal is weak and the model can be wrong. It improves "
           "only as you label more. It never excludes a job, and rules only apply after you approve them.")


class ProposalKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=3, max_length=200)


def _label_counts(db: Any, config: Any) -> dict[str, int]:
    decided = {row[0]: row[1] for row in db.execute("SELECT action, COUNT(*) FROM job_decision_current GROUP BY action")}
    role_marks = ",".join("?" for _ in ROLE_FIT_NEGATIVE_REASONS)
    role_negative = db.execute(
        f"SELECT COUNT(*) FROM job_decision_current WHERE action = 'not_applying' AND reason IN ({role_marks})",
        sorted(ROLE_FIT_NEGATIVE_REASONS)).fetchone()[0]
    not_applying = decided.get("not_applying", 0)
    legacy = db.execute("SELECT COUNT(*) FROM job_feedback_current f WHERE NOT EXISTS "
                        "(SELECT 1 FROM job_decision_current d WHERE d.job_id = f.job_id)").fetchone()[0]
    return {"applied": decided.get("applied", 0), "saved": decided.get("saved", 0),
            "not_applying_role_fit": role_negative, "not_applying_other_reasons": not_applying - role_negative,
            "older_like_dismiss_clicks": legacy}


@router.get("/learning", summary="Labels, ranker state, suggested rules and active rules")
def get_learning(db: ReadDb, config: ConfigDep) -> dict[str, Any]:
    meta = ranker.read_meta(config)
    rules = learned_rules.load_rules(config.resolve_path(learned_rules.LEARNED_FILE))
    counts = _label_counts(db, config)
    cv = (meta.get("cv", {}).get(meta.get("chosen", ""), {}) or {}).get("spearman")
    return {
        "labels": counts, "labels_in_last_training": meta.get("label_sources", {}),
        "human_labels": counts["applied"] + counts["saved"] + counts["not_applying_role_fit"],
        "ranker": {
            "trained_at": meta.get("trained_at"), "rows": meta.get("rows"), "chosen": meta.get("chosen"),
            "cv_spearman": cv, "similarity_only_spearman": (meta.get("cv", {}).get("similarity_only", {}) or {}).get("spearman"),
            "in_use": meta.get("in_use", "new" if meta else None), "last_decision": meta.get("last_decision"),
            "version": meta.get("version"),
        },
        "proposals": [p.as_dict() for p in suggest(db)],
        "rules": [{"id": r.id, "kind": r.kind, "phrase": r.phrase, "demote": r.demote, "penalty": r.penalty,
                   "count": r.evidence_count, "job_ids": list(r.job_ids[:10]), "created": r.created, "reason": r.reason}
                  for r in rules],
        "honesty": HONESTY,
        "applies": "Approved or removed rules take effect at the next relevance run (the scheduled ingests run it).",
    }


@router.post("/learning/proposals/approve", summary="Approve a suggested rule (writes learned_rules.yaml)")
def approve(payload: ProposalKey, db: WriteDb, config: ConfigDep) -> dict[str, Any]:
    proposal = next((p for p in suggest(db, limit=200) if p.key == payload.key), None)
    if proposal is None:
        raise HTTPException(status_code=404, detail="that suggestion no longer applies")
    now = to_iso(utc_now())
    try:
        rule = learned_rules.add_rule(
            config.resolve_path(learned_rules.LEARNED_FILE), kind=proposal.kind, phrase=proposal.phrase,
            evidence_count=proposal.count, job_ids=proposal.job_ids, reason=proposal.reason, created=now[:10],
            source_key=proposal.key, demote=proposal.demote, penalty=proposal.penalty)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    with transaction(db):
        db.execute("INSERT OR REPLACE INTO rule_decisions (proposal_key, status, decided_at, evidence_json) "
                   "VALUES (?, 'approved', ?, ?)", (proposal.key, now, json.dumps(proposal.as_dict())))
    return {"id": rule.id, "phrase": rule.phrase, "applies": "at the next relevance run"}


@router.post("/learning/proposals/reject", summary="Reject a suggestion (it will not be proposed again)")
def reject(payload: ProposalKey, db: WriteDb) -> dict[str, str]:
    with transaction(db):
        db.execute("INSERT OR REPLACE INTO rule_decisions (proposal_key, status, decided_at, evidence_json) "
                   "VALUES (?, 'rejected', ?, NULL)", (payload.key, to_iso(utc_now())))
    return {"key": payload.key, "status": "rejected"}


@router.delete("/learning/rules/{rule_id}", summary="Remove an active learned rule")
def remove(rule_id: str, db: WriteDb, config: ConfigDep) -> dict[str, str]:
    path = config.resolve_path(learned_rules.LEARNED_FILE)
    rule = next((r for r in learned_rules.load_rules(path) if r.id == rule_id), None)
    if rule is None or not learned_rules.remove_rule(path, rule_id):
        raise HTTPException(status_code=404, detail="rule not found")
    with transaction(db):
        db.execute("DELETE FROM rule_decisions WHERE proposal_key = ? AND status = 'approved'", (rule.source_key,))
    return {"id": rule_id, "status": "removed", "applies": "at the next relevance run"}
