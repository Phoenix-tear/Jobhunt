"""FastAPI endpoints for interactive triage audit review (Brief v14 Section 4).

Allows reviewing and labeling ambiguous jobs directly in the web UI instead of editing CSVs.
Labels are written directly to `job_audit_labels` with weight 5.0 and immediately recompute acting rules.
Protected by CSRF and local origin checks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from jobhunt.api.database import ConfigDep, WriteDb
from jobhunt.api.schemas import AuditLabelIn, AuditStateOut, AuditUndoIn
from jobhunt.matching.triage_audit import (
    ALLOWED_VERDICTS,
    get_audit_review_state,
    save_single_audit_label,
    undo_single_audit_label,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/triage/audit", tags=["triage-audit"])


@router.get("", response_model=AuditStateOut)
def get_triage_audit_state(
    connection: WriteDb,
    config: ConfigDep,
) -> dict[str, Any]:
    """Retrieve audit items, review progress, accuracy stats, and acting rules."""
    return get_audit_review_state(connection, config)


@router.post("/label", response_model=AuditStateOut)
def record_audit_label(
    payload: AuditLabelIn,
    connection: WriteDb,
    config: ConfigDep,
) -> dict[str, Any]:
    """Record a user audit verdict (1-5), update acting rules, and return updated state."""
    if payload.my_verdict not in ALLOWED_VERDICTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid verdict '{payload.my_verdict}'. Must be one of {sorted(ALLOWED_VERDICTS)}",
        )
    save_single_audit_label(
        connection,
        config,
        job_id=payload.job_id,
        my_verdict=payload.my_verdict,
        my_note=payload.my_note,
    )
    return get_audit_review_state(connection, config)


@router.post("/undo", response_model=AuditStateOut)
def undo_audit_label(
    payload: AuditUndoIn,
    connection: WriteDb,
    config: ConfigDep,
) -> dict[str, Any]:
    """Revert audit verdict for a job, recompute acting rules, and return updated state."""
    undo_single_audit_label(
        connection,
        config,
        job_id=payload.job_id,
    )
    return get_audit_review_state(connection, config)

