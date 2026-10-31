"""Application factory: wires middleware, routers and the built frontend together.

Local-only by design (Spring Boot analogy: a servlet filter chain in front of the controllers):

* `serve` binds 127.0.0.1 and has no host option;
* `Host` header allow-list (stops DNS-rebinding: a hostile web page cannot reach this API through
  a domain that resolves to 127.0.0.1);
* state-changing requests must be `application/json` and, when an `Origin` header is present, it
  must be a localhost origin (a hostile page cannot POST here from the user's browser);
* CORS only for the Vite dev origin.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from jobhunt.api import (
    routes_applications,
    routes_audit,
    routes_company_rules,
    routes_decisions,
    routes_facts,
    routes_health,
    routes_jobs,
    routes_learning,
    routes_profile,
    routes_resumes,
    routes_resumes_base,
    routes_settings,
    routes_setup,
    routes_tailor,
    routes_today,
    routes_writing,
)
from jobhunt.api.security import csrf_and_origin_guard, generate_csrf_token
from jobhunt import liveness
from jobhunt.config import Config
from jobhunt.db import connect, migrate_with_backup
from jobhunt.settings.secrets import install_secret_masking
from jobhunt.settings.setup_state import check_and_autodone_existing_install

logger = logging.getLogger(__name__)

DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SinglePageAppFiles(StaticFiles):
    """Serve the built React app; unknown paths fall back to index.html so client routes survive refresh.

    Paths under api/ are never rewritten: a mistyped API URL must stay a real 404, not an HTML page.
    (Starlette hands us an OS-normalized path, i.e. backslashes on Windows, hence the replace.)
    """

    def __init__(self, *args: Any, app: FastAPI | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._app = app

    async def get_response(self, path: str, scope: Scope) -> Response:
        is_api_path = path.replace("\\", "/").startswith("api/")
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code != 404 or is_api_path:
                raise
            response = await super().get_response("index.html", scope)
        if response.status_code == 404 and not is_api_path:
            response = await super().get_response("index.html", scope)

        # Inject CSRF token into HTML bootstrap
        if response.status_code == 200 and response.headers.get("content-type", "").startswith("text/html"):
            csrf_token = getattr(self._app.state, "csrf_token", "") if self._app else ""
            if csrf_token and hasattr(response, "body"):
                body = response.body.decode("utf-8")
                injected = f'<script>window.__JOBHUNT_TOKEN__ = "{csrf_token}";</script>'
                if "</head>" in body:
                    body = body.replace("</head>", f"{injected}</head>")
                else:
                    body = f"{injected}{body}"
                return Response(content=body, media_type="text/html", headers=dict(response.headers))
        return response


def create_app(
    config: Config,
    *,
    frontend_dir: Path | None = None,
    extra_allowed_hosts: tuple[str, ...] = (),
    migrate: bool = True,
) -> FastAPI:
    install_secret_masking()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if migrate:
            connection = connect(config.database_file)
            try:
                applied = migrate_with_backup(connection, config.database_file, config.migrations_directory,
                                            config.backup_directory)
                if applied:
                    logger.info("applied migration(s) %s", applied)
                try:
                    check_and_autodone_existing_install(connection, config)
                except Exception:
                    logger.exception("check_and_autodone_existing_install failed")
                try:                      # fills "what changed" for older rows; reads files, never writes them
                    from jobhunt.tailoring.resumes import backfill_change_summaries

                    filled = backfill_change_summaries(connection, config)
                    if filled:
                        logger.info("computed change summaries for %d resume version(s)", filled)
                except Exception:  # noqa: BLE001 - the site must start even if a summary cannot be computed
                    logger.exception("change-summary backfill failed")
            finally:
                connection.close()

        scheduler = None
        if config.schedule.backend == "in_app":
            try:
                from jobhunt.schedule.backends.in_app import InAppScheduler

                scheduler = InAppScheduler(config)
                scheduler.start()
            except Exception:
                logger.exception("Failed to start in_app scheduler")

        try:
            yield
        finally:
            if scheduler:
                scheduler.stop()

    app = FastAPI(title="jobhunt", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.state.config = config
    app.state.csrf_token = generate_csrf_token()
    app.state.demo_mode = False
    app.state.demo_config = None
    app.state.facet_cache = {}
    app.state.liveness_fetcher_factory = lambda: liveness.make_fetcher(config)   # tests swap in a mock transport

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
        allow_headers=["Content-Type", "X-Jobhunt-Token"],
        allow_credentials=False,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*ALLOWED_HOSTS, *extra_allowed_hosts])
    app.middleware("http")(csrf_and_origin_guard)

    # Base resume routes mounted before tailored resume /api/resumes/{id}
    app.include_router(routes_setup.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_profile.router)
    app.include_router(routes_facts.router)
    app.include_router(routes_resumes_base.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_applications.router)
    app.include_router(routes_health.router)
    app.include_router(routes_tailor.router)
    app.include_router(routes_resumes.router)
    app.include_router(routes_decisions.router)
    app.include_router(routes_company_rules.router)
    app.include_router(routes_learning.router)
    app.include_router(routes_today.router)
    app.include_router(routes_writing.router)
    app.include_router(routes_audit.router)

    dist = frontend_dir if frontend_dir is not None else config.project_root / "web" / "dist"
    if (dist / "index.html").is_file():
        app.mount("/", SinglePageAppFiles(directory=dist, html=True, app=app), name="web")
    else:
        @app.get("/", include_in_schema=False)
        def frontend_missing() -> JSONResponse:
            return JSONResponse(
                {"message": "The API is running but the web app is not built. Run `npm install && npm run build` "
                            "in web/, or use `npm run dev` during development. API docs: /api/docs"}
            )

    return app
