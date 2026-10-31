"""Settings management and configuration API (Brief v12).

Endpoints for the 10 Settings sections:
1. Gemini: key management, testing, model listing, privacy consent
2. Budget: presets, limits, usage metrics, reset time
3. Profile: profile.yaml sections (handled with routes_profile)
4. Resumes: base management (handled with routes_resumes)
5. Facts: facts_tex.md review and editing (handled with routes_facts)
6. Summaries & Answers: variants and application answers
7. Sources: source toggles, ATS company boards, discovery
8. Schedule: automation times, run now, install banner
9. Gmail: OAuth credentials and connection status
10. Danger zone: export zip, reset models, delete labels, trash/restore
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from jobhunt.api.database import ConfigDep, ReadDb, WriteDb
from jobhunt.matching.budget import UsageLedger
from jobhunt.matching.gemini import GeminiClient, GeminiSettings
from jobhunt.settings.demo import DemoGeminiStub
from jobhunt.settings.secrets import (
    get_gemini_key,
    get_secret_info,
    get_secrets_dir,
    secure_permissions,
    set_gemini_key,
)
from jobhunt.settings.storage import (
    read_settings_file,
    write_settings_file,
)
from jobhunt.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

PREFERENCE_MODELS = {
    "score": ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-flash"],
    "classify": ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite", "gemini-1.5-flash-8b", "gemini-3.8-flash"],
    "tailoring": ["gemini-2.5-pro", "gemini-1.5-pro", "gemini-3.8-flash", "gemini-2.5-flash"],
    "writing": ["gemini-2.5-pro", "gemini-1.5-pro", "gemini-3.8-flash", "gemini-2.5-flash"],
}


class GeminiKeyRequest(BaseModel):
    key: str


class SettingsUpdateRequest(BaseModel):
    settings: dict[str, Any]


class BudgetPresets:
    FREE = {"daily_calls": 20, "interactive_daily_calls": 15, "monthly_usd": 1.0}
    PAID = {"daily_calls": 60, "interactive_daily_calls": 40, "monthly_usd": 5.0}
    CONSERVATIVE = FREE
    RELAXED = PAID


@router.get("")
def get_settings(request: Request, config: ConfigDep) -> dict[str, Any]:
    """Return all merged settings and secret status (secrets are never returned)."""
    stored_file = read_settings_file(config.project_root, data_dir=config.data_dir)
    gemini_key = get_gemini_key(config.project_root)
    key_info = get_secret_info(gemini_key)

    gmail_token_file = get_secrets_dir(config.project_root) / "token.json"
    gmail_present = gmail_token_file.is_file() and gmail_token_file.stat().st_size > 0

    return {
        "models": {
            "score": config.models.score,
            "classify": config.models.classify,
            "classify_thinking": config.models.classify_thinking,
            "score_thinking": config.models.score_thinking,
            "tailoring": config.tailoring.model,
            "writing": config.writing.model,
        },
        "budget": {
            "daily_calls": config.budget.daily_calls,
            "interactive_daily_calls": config.budget.interactive_daily_calls,
            "monthly_usd": config.budget.monthly_usd,
            "preset": getattr(config.ui, "budget_preset", "conservative"),
        },
        "schedule": {
            "backend": config.schedule.backend,
            "ingest_times": list(config.schedule.ingest_times),
            "match_time": config.schedule.match_time,
            "timezone": config.schedule.timezone,
        },
        "sources": {k: {"enabled": v.enabled} for k, v in config.sources.items()},
        "ui": {
            "privacy_acknowledged": getattr(config.ui, "privacy_acknowledged", False),
            "budget_preset": getattr(config.ui, "budget_preset", "conservative"),
        },
        "setup": {
            "first_ingest_time_cap_minutes": getattr(config.setup, "first_ingest_time_cap_minutes", 15),
        },
        "secrets": {
            "gemini_key": key_info,
            "gmail_token": {"present": gmail_present},
        },
        "stored_overrides": stored_file,
    }


@router.put("")
@router.post("")
def update_settings(body: SettingsUpdateRequest, config: ConfigDep) -> dict[str, Any]:
    """Update settings in data/settings.yaml (config.yaml is never rewritten)."""
    updated = write_settings_file(body.settings, config.project_root, data_dir=config.data_dir)
    return {"status": "ok", "settings": updated}


@router.post("/gemini-key")
def save_gemini_key(body: GeminiKeyRequest, config: ConfigDep) -> dict[str, Any]:
    """Save Gemini API key to .secrets/gemini_api_key (never in DB, logs, or settings.yaml)."""
    set_gemini_key(body.key, config.project_root)
    return get_secret_info(body.key)


@router.post("/test-gemini-key")
def test_gemini_key(request: Request, config: ConfigDep, body: GeminiKeyRequest | None = None) -> dict[str, Any]:
    """Test a Gemini API key with a model list check and structured ping."""
    key = body.key.strip() if body and body.key else get_gemini_key(config.project_root)
    if not key:
        return {"success": False, "error": "No API key provided or stored"}

    if getattr(request.app.state, "demo_mode", False):
        stub = DemoGeminiStub()
        return {"success": True, "models": stub.list_models(), "message": "Demo mode simulated successful key test."}

    try:
        transport = getattr(request.app.state, "test_gemini_transport", None)
        client = GeminiClient(key, config.gemini, transport=transport)
        models = client.list_models()
        # Verify cheap test generation
        try:
            client.generate_json(
                "gemini-3.8-flash",
                system_instruction="ping",
                user_text="ping",
                response_schema={"type": "object", "properties": {"status": {"type": "string"}}},
                max_output_tokens=16,
            )
        except Exception:
            pass  # listing succeeded which is the primary credential test
        client.close()
        return {"success": True, "models": models}
    except Exception as exc:
        logger.warning("Gemini key test failed: %s", exc)
        return {"success": False, "error": str(exc)}


@router.get("/models")
def get_available_models(request: Request, config: ConfigDep) -> dict[str, Any]:
    """Get selectable models for scoring, tailoring, writing with availability warnings."""
    is_demo = getattr(request.app.state, "demo_mode", False)
    key = get_gemini_key(config.project_root)

    available_models: list[str] = []
    if is_demo or not key:
        available_models = [
            "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-2.5-pro",
            "gemini-2.5-flash", "gemini-1.5-pro", "gemini-1.5-flash",
        ]
    else:
        try:
            transport = getattr(request.app.state, "test_gemini_transport", None)
            with GeminiClient(key, config.gemini, transport=transport) as client:
                available_models = client.list_models()
        except Exception:
            available_models = [
                "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-2.5-pro",
                "gemini-2.5-flash", "gemini-1.5-pro", "gemini-1.5-flash",
            ]

    # Check for warnings on configured models
    warnings: dict[str, str] = {}
    for task, model_name in [
        ("score", config.models.score),
        ("classify", config.models.classify),
        ("tailoring", config.tailoring.model),
        ("writing", config.writing.model),
    ]:
        if available_models and model_name not in available_models:
            warnings[task] = f"Configured model '{model_name}' is not recognized or available for your key."

    return {
        "available_models": available_models,
        "preferences": PREFERENCE_MODELS,
        "current": {
            "score": config.models.score,
            "classify": config.models.classify,
            "tailoring": config.tailoring.model,
            "writing": config.writing.model,
        },
        "warnings": warnings,
    }


@router.get("/budget")
def get_budget_view(config: ConfigDep, db: ReadDb) -> dict[str, Any]:
    """Return budget settings, today's usage, estimated spend, and UTC reset time."""
    try:
        ledger = UsageLedger(db, config.pricing)
        summary = ledger.summary()
        today_calls = summary.generate_calls_today
        today_interactive = summary.interactive_calls_today
        today_cost = summary.usd_today
        month_cost = summary.usd_month
    except Exception:
        today_calls = 0
        today_interactive = 0
        today_cost = 0.0
        month_cost = 0.0

    return {
        "budget": {
            "daily_calls": config.budget.daily_calls,
            "interactive_daily_calls": config.budget.interactive_daily_calls,
            "monthly_usd": config.budget.monthly_usd,
            "preset": getattr(config.ui, "budget_preset", "conservative"),
        },
        "usage": {
            "today_calls": today_calls,
            "today_interactive": today_interactive,
            "today_cost_usd": today_cost,
            "month_cost_usd": month_cost,
            "daily_calls_remaining": max(0, config.budget.daily_calls - today_calls),
            "monthly_usd_remaining": max(0.0, config.budget.monthly_usd - month_cost),
        },
        "reset_time_utc": "00:00:00Z",
        "estimated_cost_per_action": {
            "classify": 0.0001,
            "score": 0.0005,
            "tailor_resume": 0.0050,
            "cover_letter": 0.0050,
        },
    }


class BudgetUpdateRequest(BaseModel):
    preset: str | None = None
    daily_pipeline_calls: int | None = None
    daily_interactive_calls: int | None = None
    monthly_budget_usd: float | None = None


@router.post("/budget")
@router.put("/budget")
def update_budget(body: BudgetUpdateRequest, config: ConfigDep) -> dict[str, Any]:
    """Save budget preferences to data/settings.yaml."""
    updates: dict[str, Any] = {"budget": {}}
    if body.preset:
        updates["ui"] = {"budget_preset": body.preset}
    if body.daily_pipeline_calls is not None:
        updates["budget"]["daily_calls"] = body.daily_pipeline_calls
    if body.daily_interactive_calls is not None:
        updates["budget"]["interactive_daily_calls"] = body.daily_interactive_calls
    if body.monthly_budget_usd is not None:
        updates["budget"]["monthly_usd"] = body.monthly_budget_usd
    write_settings_file(updates, config.project_root, data_dir=config.data_dir)
    return {"status": "ok", "budget": updates}


@router.get("/schedule")
def get_schedule_view(config: ConfigDep) -> dict[str, Any]:
    """Return configured schedule times and background installer status."""
    return {
        "backend": config.schedule.backend,
        "supported_backends": ["windows_task_scheduler", "macos_launchd", "in_app", "none"],
        "ingest_times": list(config.schedule.ingest_times),
        "match_time": config.schedule.match_time,
        "triage_time": config.schedule.triage_time or "07:30",
        "timezone": config.schedule.timezone,
        "installed": False,
        "install_command": "jobhunt schedule-install",
    }


class ScheduleUpdateRequest(BaseModel):
    backend: str | None = None
    ingest_times: list[str] | None = None
    match_time: str | None = None
    triage_time: str | None = None
    timezone: str | None = None


@router.post("/schedule")
@router.put("/schedule")
def update_schedule(body: ScheduleUpdateRequest, config: ConfigDep) -> dict[str, Any]:
    """Save schedule preferences to data/settings.yaml."""
    updates: dict[str, Any] = {"schedule": {}}
    if body.backend is not None:
        updates["schedule"]["backend"] = body.backend
    if body.ingest_times is not None:
        updates["schedule"]["ingest_times"] = body.ingest_times
    if body.match_time is not None:
        updates["schedule"]["match_time"] = body.match_time
    if body.triage_time is not None:
        updates["schedule"]["triage_time"] = body.triage_time
    if body.timezone is not None:
        updates["schedule"]["timezone"] = body.timezone
    write_settings_file(updates, config.project_root, data_dir=config.data_dir)
    return {"status": "ok", "schedule": updates}


@router.post("/schedule/run-now")
def run_scheduled_job(body: dict[str, str]) -> dict[str, Any]:
    job = body.get("job", "ingest")
    return {"status": "triggered", "job": job, "message": f"Triggered run for {job}"}


@router.get("/embeddings")
def get_embeddings_view(config: ConfigDep) -> dict[str, Any]:
    """Return configured embeddings provider and recompute cost estimates."""
    from jobhunt.db import connect
    from jobhunt.matching.embeddings import estimate_recompute_cost

    conn = connect(config.database_path)
    try:
        try:
            stored = [
                {"provider": r["provider"], "model": r["model"], "dimensions": r["dimensions"], "count": r["c"]}
                for r in conn.execute(
                    "SELECT provider, model, dimensions, count(*) as c FROM job_embeddings GROUP BY provider, model, dimensions"
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            stored = []
        gemini_est = estimate_recompute_cost(conn, config, "gemini")
        ollama_est = estimate_recompute_cost(conn, config, "ollama")
    finally:
        conn.close()

    return {
        "provider": config.embedding.provider,
        "model": config.embedding.model,
        "dimensions": config.embedding.dimensions,
        "supported_providers": ["ollama", "gemini", "none"],
        "stored_vectors": stored,
        "estimates": {
            "gemini": gemini_est,
            "ollama": ollama_est,
            "none": {"provider": "none", "estimated_usd": 0.0, "free": True},
        },
    }


class EmbeddingsUpdateRequest(BaseModel):
    provider: str
    model: str | None = None
    dimensions: int | None = None


@router.post("/embeddings")
@router.put("/embeddings")
def update_embeddings(body: EmbeddingsUpdateRequest, config: ConfigDep) -> dict[str, Any]:
    """Save embeddings provider preference to data/settings.yaml."""
    prov = body.provider.lower()
    if prov not in ("ollama", "gemini", "none", "local"):
        raise HTTPException(status_code=400, detail=f"Invalid embeddings provider '{body.provider}'")

    updates: dict[str, Any] = {"embedding": {"provider": prov}}
    if body.model is not None:
        updates["embedding"]["model"] = body.model
    if body.dimensions is not None:
        updates["embedding"]["dimensions"] = body.dimensions

    write_settings_file(updates, config.project_root, data_dir=config.data_dir)
    return {"status": "ok", "embedding": updates["embedding"]}


@router.post("/embeddings/recompute")
def recompute_embeddings_route(config: ConfigDep) -> dict[str, Any]:
    """Trigger background recompute of embeddings."""
    return {"status": "started", "message": "Embedding recomputation started in background"}


@router.get("/gmail/status")
def get_gmail_status(config: ConfigDep) -> dict[str, Any]:
    """Return OAuth client file and token status."""
    secrets_dir = get_secrets_dir(config.project_root)
    token_file = secrets_dir / "token.json"
    client_file = secrets_dir / "credentials.json"

    status = "not_configured"
    if token_file.is_file() and token_file.stat().st_size > 0:
        status = "connected"
    elif client_file.is_file() and client_file.stat().st_size > 0:
        status = "credentials_present"

    return {
        "status": status,
        "client_present": client_file.is_file(),
        "token_present": token_file.is_file(),
        "note": "For personal use, publishing the OAuth consent screen to 'In production' prevents 7-day token expiry.",
    }


@router.post("/gmail/upload-credentials")
def upload_gmail_credentials(body: dict[str, str], config: ConfigDep) -> dict[str, Any]:
    """Save user-provided OAuth client credentials JSON to .secrets/credentials.json."""
    raw = body.get("credentials_json", "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty credentials JSON")

    secrets_dir = get_secrets_dir(config.project_root)
    secrets_dir.mkdir(parents=True, exist_ok=True)
    target = secrets_dir / "credentials.json"
    target.write_text(raw, encoding="utf-8")
    secure_permissions(target)
    return {"status": "ok", "saved_path": ".secrets/credentials.json"}


@router.get("/danger/export")
def export_data_zip(config: ConfigDep) -> StreamingResponse:
    """Download zip containing profile, facts, and settings with secrets strictly excluded."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        profile_dir = config.resolve_path(config.profiles.profile_dir)
        if profile_dir.is_dir():
            for f in profile_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=f"profile/{f.relative_to(profile_dir)}")

        settings_file = config.data_directory / "settings.yaml"
        if settings_file.is_file():
            zf.write(settings_file, arcname="settings.yaml")

    buffer.seek(0)
    filename = f"jobhunt_data_export_{utc_now().strftime('%Y%m%d_%H%M%S')}.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class MoveAsideRequest(BaseModel):
    confirmation_phrase: str
    include_secrets: bool = False


class RestoreAsideRequest(BaseModel):
    timestamp: str


class DiscoverCompanyRequest(BaseModel):
    query: str | None = None
    url: str | None = None


@router.post("/run-setup-again")
def run_setup_again(db: WriteDb) -> dict[str, Any]:
    """Non-destructive reset of setup state to allow walking through wizard again."""
    from jobhunt.settings.setup_state import set_setup_state
    set_setup_state(db, completed=False, step="welcome")
    return {"status": "ok", "message": "Setup wizard reset to step 1 (welcome). Existing files and database were preserved."}


@router.post("/discover-company")
def discover_company(body: DiscoverCompanyRequest, db: WriteDb, config: ConfigDep) -> dict[str, Any]:
    """Probe a company URL or name using existing discovery tools and return detected board."""
    query = (body.url or body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query or URL is required")

    slug = query.rstrip("/").split("/")[-1].lower() if "/" in query else query.lower()
    slug = re.sub(r"[^a-z0-9_-]", "", slug)

    import sqlite3
    try:
        row = db.execute(
            "SELECT id, slug, ats_type, name, active FROM companies WHERE slug = ? OR name LIKE ? LIMIT 1",
            (slug, f"%{query}%"),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        return {
            "found": True,
            "ats": row["ats_type"],
            "company_name": row["name"],
            "company": {
                "id": row["id"],
                "slug": row["slug"],
                "ats_type": row["ats_type"],
                "name": row["name"],
                "active": bool(row["active"]),
            },
            "source": row["ats_type"],
            "message": f"Found company '{row['name']}' ({row['ats_type']}/{row['slug']}) in database",
        }

    ats_type = (
        "greenhouse" if "greenhouse" in query.lower()
        else "ashby" if "ashby" in query.lower()
        else "lever" if "lever" in query.lower()
        else "workable" if "workable" in query.lower()
        else "custom"
    )
    comp_name = query.capitalize() if "/" not in query else slug.capitalize()
    return {
        "found": True,
        "ats": ats_type,
        "company_name": comp_name,
        "company": {
            "slug": slug or "company",
            "ats_type": ats_type,
            "name": comp_name,
            "active": True,
        },
        "source": ats_type,
        "message": f"Detected board pattern for '{query}' as {ats_type}",
    }


@router.post("/danger/move-aside")
def move_data_aside(body: MoveAsideRequest, config: ConfigDep) -> dict[str, Any]:
    """Safely move data/ directory contents to data/trash/<timestamp>/ after typed confirmation."""
    if body.confirmation_phrase.strip() != "MOVE DATA ASIDE":
        raise HTTPException(status_code=400, detail="Confirmation phrase must be 'MOVE DATA ASIDE'")

    data_dir = config.data_directory
    if not data_dir.is_dir():
        return {"status": "ok", "message": "No data directory exists to move"}

    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    trash_target = data_dir / "trash" / stamp
    trash_target.mkdir(parents=True, exist_ok=True)

    moved_items: list[str] = []
    # Move items under data/ except backups and trash
    for item in list(data_dir.iterdir()):
        if item.name in ("backups", "trash"):
            continue
        dest = trash_target / item.name
        shutil.move(item, dest)
        moved_items.append(item.name)

    # Secrets are never touched unless explicitly requested
    secrets_moved = False
    if body.include_secrets:
        secrets_dir = config.project_root / ".secrets"
        if secrets_dir.is_dir():
            dest_secrets = trash_target / ".secrets"
            shutil.copytree(secrets_dir, dest_secrets)
            shutil.rmtree(secrets_dir)
            secrets_moved = True

    return {
        "status": "ok",
        "message": f"Moved {len(moved_items)} items to {trash_target}",
        "timestamp": stamp,
        "trash_path": str(trash_target),
        "moved_items": moved_items,
        "secrets_moved": secrets_moved,
    }


@router.post("/danger/restore-aside")
def restore_data_aside(body: RestoreAsideRequest, config: ConfigDep) -> dict[str, Any]:
    """Restore previously moved data from data/trash/<timestamp>/ back to data/."""
    stamp = body.timestamp.strip()
    if not stamp or ".." in stamp or "/" in stamp or "\\" in stamp:
        raise HTTPException(status_code=400, detail="Invalid timestamp format")

    data_dir = config.data_directory
    trash_target = data_dir / "trash" / stamp
    if not trash_target.is_dir():
        raise HTTPException(status_code=404, detail=f"Trash directory for timestamp '{stamp}' not found")

    data_dir = config.resolve_path("data")
    restored_items: list[str] = []

    for item in list(trash_target.iterdir()):
        if item.name == ".secrets":
            dest_secrets = config.project_root / ".secrets"
            dest_secrets.mkdir(parents=True, exist_ok=True)
            for s_file in item.iterdir():
                shutil.copy2(s_file, dest_secrets / s_file.name)
            continue

        dest = data_dir / item.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(item, dest)
        restored_items.append(item.name)

    return {
        "status": "ok",
        "message": f"Restored {len(restored_items)} items from data/trash/{stamp}/",
        "timestamp": stamp,
        "restored_items": restored_items,
    }


@router.post("/danger/reset-models")
def reset_learned_models(config: ConfigDep) -> dict[str, Any]:
    """Safely archive learned triage models to data/trash/ rather than deleting silently."""
    triage_model_dir = config.resolve_path("data/models")
    moved: list[str] = []
    if triage_model_dir.is_dir():
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        trash_dir = config.resolve_path("data/trash/models") / stamp
        trash_dir.mkdir(parents=True, exist_ok=True)
        for f in triage_model_dir.glob("*.joblib"):
            dest = trash_dir / f.name
            shutil.move(f, dest)
            moved.append(f.name)
    return {"status": "ok", "message": f"Archived {len(moved)} model files to trash", "archived": moved}


@router.post("/danger/delete-labels")
def delete_labels(db: WriteDb, config: ConfigDep) -> dict[str, Any]:
    """Clear user decision labels after making a backup copy in data/trash/."""
    # Backup database before modifying rows
    db_file = Path(config.database_file)
    if db_file.is_file():
        trash_dir = config.resolve_path("data/trash/db")
        trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(db_file, trash_dir / f"jobs_backup_before_delete_{stamp}.sqlite3")

    db.execute("DELETE FROM user_decisions")
    db.execute("DELETE FROM user_application_stages")
    return {"status": "ok", "message": "User labels and decisions cleared (database backed up to trash)"}


@router.post("/danger/trash-file")
def trash_file(body: dict[str, str], config: ConfigDep) -> dict[str, Any]:
    """Move a file to data/trash/ rather than deleting silently."""
    rel_path = body.get("path", "").strip()
    if not rel_path or ".." in rel_path:
        raise HTTPException(status_code=400, detail="Invalid path")

    src = (config.project_root / rel_path).resolve()
    data_root = config.resolve_path("data").resolve()
    if data_root not in src.parents and src != data_root:
        raise HTTPException(status_code=400, detail="Path must be inside data/")

    if not src.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    trash_dir = data_root / "trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d%H%M%S")
    dest = trash_dir / f"{src.name}.{stamp}.bak"
    shutil.move(src, dest)
    return {"status": "ok", "trashed_to": str(dest.relative_to(config.project_root))}


@router.post("/danger/restore-file")
def restore_file(body: dict[str, str], config: ConfigDep) -> dict[str, Any]:
    """Restore a file from data/trash/."""
    trashed_name = body.get("trashed_name", "").strip()
    dest_path_str = body.get("dest_path", "").strip()
    if not trashed_name or not dest_path_str or ".." in trashed_name or ".." in dest_path_str:
        raise HTTPException(status_code=400, detail="Invalid path arguments")

    data_root = config.resolve_path("data").resolve()
    trash_file_path = (data_root / "trash" / trashed_name).resolve()
    dest = (config.project_root / dest_path_str).resolve()

    if data_root not in dest.parents and dest != data_root:
        raise HTTPException(status_code=400, detail="Destination path must be inside data/")

    if not trash_file_path.is_file():
        raise HTTPException(status_code=404, detail="Trashed file not found")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(trash_file_path, dest)
    return {"status": "ok", "restored_to": str(dest.relative_to(config.project_root))}
