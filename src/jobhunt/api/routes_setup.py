"""First-run setup wizard and demo mode API (Brief v12).

Endpoints:
- GET  /api/setup/state: returns wizard completion, current step, and demo mode status
- POST /api/setup/step: updates current wizard step
- POST /api/setup/complete: marks wizard as completed
- POST /api/setup/restart: re-opens the wizard non-destructively
- POST /api/setup/demo/enter: isolates workspace into data/demo/ using demo persona
- POST /api/setup/demo/exit: exits demo mode, restoring real workspace
- POST /api/setup/first-ingest: runs first ingest with step-by-step progress
- GET  /api/bootstrap: delivers CSRF token and initial status for frontend bootstrap
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from jobhunt.api.database import ConfigDep, ReadDb, WriteDb
from jobhunt.settings.demo import setup_demo_workspace
from jobhunt.settings.setup_state import get_setup_state, set_setup_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["setup"])


class SetupStateModel(BaseModel):
    completed: bool
    step: str
    completed_at: str | None = None
    schema_version: int = 1
    demo_mode: bool = False


VALID_SETUP_STEPS = {
    "welcome", "gemini", "budget", "resumes", "facts",
    "profile", "sources", "schedule", "gmail", "finish", "done",
}


class UpdateStepRequest(BaseModel):
    step: str

    @field_validator("step")
    @classmethod
    def validate_step(cls, v: str) -> str:
        if v not in VALID_SETUP_STEPS:
            raise ValueError(f"Invalid setup step '{v}'. Must be one of {sorted(VALID_SETUP_STEPS)}")
        return v


class FirstIngestResponse(BaseModel):
    status: str
    sources: list[dict[str, Any]]
    total_jobs: int
    time_seconds: float


@router.get("/setup/state", response_model=SetupStateModel)
def read_setup_state(request: Request, db: ReadDb) -> dict[str, Any]:
    state = get_setup_state(db)
    state["demo_mode"] = getattr(request.app.state, "demo_mode", False)
    return state


@router.post("/setup/step", response_model=SetupStateModel)
def update_setup_step(body: UpdateStepRequest, request: Request, db: WriteDb) -> dict[str, Any]:
    current = get_setup_state(db)
    res = set_setup_state(db, completed=current["completed"], step=body.step)
    res["demo_mode"] = getattr(request.app.state, "demo_mode", False)
    return res


@router.post("/setup/complete", response_model=SetupStateModel)
def complete_setup(request: Request, db: WriteDb) -> dict[str, Any]:
    res = set_setup_state(db, completed=True, step="done")
    res["demo_mode"] = getattr(request.app.state, "demo_mode", False)
    return res


@router.post("/setup/restart", response_model=SetupStateModel)
def restart_setup(request: Request, db: WriteDb) -> dict[str, Any]:
    """Non-destructive reset of setup state to welcome step."""
    res = set_setup_state(db, completed=False, step="welcome")
    res["demo_mode"] = getattr(request.app.state, "demo_mode", False)
    return res


@router.post("/setup/demo/enter")
def enter_demo_mode(request: Request, config: ConfigDep) -> dict[str, Any]:
    """Switch to demo workspace under data/demo/."""
    demo_cfg = setup_demo_workspace(config)
    request.app.state.demo_mode = True
    request.app.state.demo_config = demo_cfg
    logger.info("Entered demo mode; real data is isolated and untouched.")
    return {"demo_mode": True}


@router.post("/setup/demo/exit")
def exit_demo_mode(request: Request) -> dict[str, Any]:
    """Exit demo mode and restore real workspace."""
    request.app.state.demo_mode = False
    request.app.state.demo_config = None
    logger.info("Exited demo mode; restored real workspace.")
    return {"demo_mode": False}


class FirstIngestStartRequest(BaseModel):
    time_cap_minutes: int | None = None
    sources: list[str] | None = None
    sync: bool = False


@router.post("/setup/first-ingest")
def run_first_ingest(
    request: Request,
    config: ConfigDep,
    body: FirstIngestStartRequest | None = None,
) -> dict[str, Any]:
    """Execute quick first ingest run as a background task with live progress reporting."""
    from jobhunt.settings.first_ingest import FirstIngestManager

    manager = FirstIngestManager.get_instance()
    is_demo = getattr(request.app.state, "demo_mode", False)
    cfg = getattr(request.app.state, "demo_config", None) if is_demo else config
    default_cap = getattr(getattr(cfg, "setup", None), "first_ingest_time_cap_minutes", 15)
    time_cap = body.time_cap_minutes if (body and body.time_cap_minutes is not None) else default_cap
    sources = body.sources if body else None
    sync_mode = body.sync if body else False

    return manager.start(
        cfg or config,
        time_cap_minutes=time_cap,
        demo_mode=is_demo,
        sources_to_run=sources,
        sync=sync_mode,
    )


@router.get("/setup/ingest-progress")
def get_ingest_progress() -> dict[str, Any]:
    """Poll progress metrics of the running or completed first ingest."""
    from jobhunt.settings.first_ingest import FirstIngestManager

    return FirstIngestManager.get_instance().get_progress()


@router.post("/setup/ingest-cancel")
def cancel_first_ingest() -> dict[str, Any]:
    """Cancel the ongoing first ingest job safely."""
    from jobhunt.settings.first_ingest import FirstIngestManager

    return FirstIngestManager.get_instance().cancel()


@router.get("/bootstrap")
def get_bootstrap_data(request: Request, db: ReadDb) -> dict[str, Any]:
    """Bootstrap endpoint delivering the CSRF token and setup completion state."""
    state = get_setup_state(db)
    token = getattr(request.app.state, "csrf_token", "")
    return {
        "token": token,
        "setup_completed": state["completed"],
        "setup_step": state["step"],
        "demo_mode": getattr(request.app.state, "demo_mode", False),
    }
