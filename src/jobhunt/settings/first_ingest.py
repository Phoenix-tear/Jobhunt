"""Background quick first-run ingestion runner and progress tracker (Brief v12b).

Key capabilities:
- Time-capped first ingest (default 15 minutes, configurable).
- Fast, responsive sources fetched first.
- Gate and relevance computation for new jobs.
- Graceful offline Ollama handling: embeddings/similarity skipped with plain notice.
- Resumable and cancellable without corrupting state.
- Demo mode support: seeds from demo persona fixtures and stub client.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.classify import Classifier, TitlePrefilter
from jobhunt.config import Config
from jobhunt.db import connect
from jobhunt.http import HttpClient
from jobhunt.matching.gate import run_relevance
from jobhunt.runner import IngestionRunner, RunSummary
from jobhunt.sources import ATS_SOURCE_NAMES
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)


@dataclass
class FirstIngestProgress:
    status: str = "idle"               # idle | running | completed | cancelled | failed
    stage: str = "idle"                # ingest | relevance | matching | done
    sources: list[dict[str, Any]] = field(default_factory=list)
    total_jobs: int = 0
    new_jobs: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    estimated_time_remaining: int = 0
    ollama_status: str = "offline (similarity skipped)"
    cancelled: bool = False
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        pct = 100 if self.status in {"completed", "done"} else (50 if self.status == "running" else 0)
        return {
            "status": self.status,
            "progress": pct,
            "stage": self.stage,
            "sources": self.sources,
            "total_jobs": self.total_jobs,
            "new_jobs": self.new_jobs,
            "errors": self.errors,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "estimated_time_remaining": self.estimated_time_remaining,
            "ollama_status": self.ollama_status,
            "cancelled": self.cancelled,
            "message": self.message,
        }


class FirstIngestManager:
    _instance: FirstIngestManager | None = None

    def __init__(self, config: Config | None = None) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._progress = FirstIngestProgress()
        self._cancel_flag = threading.Event()

    def is_running(self) -> bool:
        with self._lock:
            return self._progress.status == "running"

    def start_ingest(self, config: Config | None = None, **kwargs: Any) -> dict[str, Any]:
        cfg = config or self.config
        if cfg is None:
            raise ValueError("No configuration provided for first ingest")
        return self.start(cfg, **kwargs)

    @classmethod
    def get_instance(cls) -> FirstIngestManager:
        if cls._instance is None:
            cls._instance = FirstIngestManager()
        return cls._instance

    def get_progress(self) -> dict[str, Any]:
        with self._lock:
            return self._progress.as_dict()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._progress.status == "running":
                self._cancel_flag.set()
                self._progress.cancelled = True
                self._progress.status = "cancelled"
                self._progress.message = "Ingest cancelled by user."
            return self._progress.as_dict()

    def start(
        self,
        config: Config,
        *,
        time_cap_minutes: int = 15,
        demo_mode: bool = False,
        sources_to_run: list[str] | None = None,
        sync: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._progress.status == "running":
                return self._progress.as_dict()

            self._cancel_flag.clear()
            self._progress = FirstIngestProgress(
                status="running",
                stage="ingest",
                message="Starting quick first ingest...",
                estimated_time_remaining=time_cap_minutes * 60,
            )

        if sync:
            self._execute_run(config, time_cap_minutes, demo_mode, sources_to_run)
            return self.get_progress()

        self._thread = threading.Thread(
            target=self._execute_run,
            args=(config, time_cap_minutes, demo_mode, sources_to_run),
            daemon=True,
        )
        self._thread.start()
        return self.get_progress()

    def _execute_run(
        self,
        config: Config,
        time_cap_minutes: int,
        demo_mode: bool,
        sources_to_run: list[str] | None,
    ) -> None:
        start_time = time.monotonic()
        time_cap_seconds = time_cap_minutes * 60

        try:
            # Check Ollama status
            ollama_status = "offline (similarity skipped)"
            try:
                import httpx
                resp = httpx.get("http://127.0.0.1:11434/api/tags", timeout=1.0)
                if resp.status_code == 200:
                    ollama_status = "available"
            except Exception:
                ollama_status = "offline (similarity skipped)"

            with self._lock:
                self._progress.ollama_status = ollama_status

            if demo_mode:
                self._run_demo_ingest(config, start_time)
                return

            # Real Ingestion Mode
            self._run_real_ingest(config, time_cap_seconds, sources_to_run, start_time)

        except Exception as exc:
            logger.exception("First ingest encountered an error: %s", exc)
            with self._lock:
                self._progress.status = "failed"
                self._progress.message = f"Ingestion error: {exc}"
                self._progress.errors.append(str(exc))

    def _run_demo_ingest(self, config: Config, start_time: float) -> None:
        from jobhunt.settings.demo import setup_demo_workspace
        demo_cfg = setup_demo_workspace(config)
        conn = connect(demo_cfg.database_file)
        try:
            time.sleep(0.5)
            row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
            total = row[0] if row else 0

            with self._lock:
                self._progress.sources = [
                    {"source": "demo_simplify", "jobs_found": 18, "jobs_new": 18, "status": "completed"},
                    {"source": "demo_curated", "jobs_found": 12, "jobs_new": 12, "status": "completed"},
                ]
                self._progress.total_jobs = total
                self._progress.new_jobs = total
                self._progress.stage = "relevance"
                self._progress.message = f"Found {total} demo jobs. Computing relevance..."
                self._progress.elapsed_seconds = time.monotonic() - start_time

            time.sleep(0.3)
            with self._lock:
                self._progress.stage = "done"
                self._progress.status = "completed"
                self._progress.message = f"Quick first ingest completed: {total} jobs ready."
                self._progress.elapsed_seconds = time.monotonic() - start_time
                self._progress.estimated_time_remaining = 0
        finally:
            conn.close()

    def _run_real_ingest(
        self,
        config: Config,
        time_cap_seconds: float,
        sources_to_run: list[str] | None,
        start_time: float,
    ) -> None:
        from jobhunt.envfile import load_secrets
        from jobhunt.sources import build_all_sources

        connection = connect(config.database_file)
        try:
            sources_map = build_all_sources(config, load_secrets(config.project_root))
            if not sources_map:
                with self._lock:
                    self._progress.status = "completed"
                    self._progress.stage = "done"
                    self._progress.message = "No sources enabled. Check config or settings."
                return

            # Order: fast / responsive feeds first
            priority_order = ["curated_companies", "simplify_intern", "simplify_new_grad", "simplify", "openroles", "startup_jobs"]
            ordered_keys = [k for k in priority_order if k in sources_map]
            ordered_keys += [k for k in sources_map if k not in ordered_keys]

            if sources_to_run:
                ordered_keys = [k for k in ordered_keys if k in sources_to_run]

            total_seen = 0
            total_new = 0
            sources_summary: list[dict[str, Any]] = []

            with HttpClient(config.http, config.user_agent) as http:
                runner = IngestionRunner(connection, config, sources_map, http)

                for src_name in ordered_keys:
                    if self._cancel_flag.is_set():
                        with self._lock:
                            self._progress.status = "cancelled"
                            self._progress.message = "Ingestion cancelled."
                        return

                    elapsed = time.monotonic() - start_time
                    if elapsed >= time_cap_seconds:
                        logger.info("Time cap reached (%s s); stopping first ingest early.", elapsed)
                        break

                    with self._lock:
                        self._progress.message = f"Fetching from {src_name}..."
                        self._progress.elapsed_seconds = elapsed
                        rem = max(0, int(time_cap_seconds - elapsed))
                        self._progress.estimated_time_remaining = rem

                    # Run for this source with a modest company limit for quick responsiveness
                    try:
                        summaries = runner.run([src_name], company_limit=15, force=False)
                        for s in summaries:
                            total_seen += s.jobs_seen
                            total_new += s.jobs_new
                            sources_summary.append({
                                "source": s.source,
                                "jobs_found": s.jobs_seen,
                                "jobs_new": s.jobs_new,
                                "status": "completed",
                            })
                            for err in s.errors:
                                self._progress.errors.append(f"{s.source}: {err.get('message', '')}")
                    except Exception as s_exc:
                        sources_summary.append({
                            "source": src_name,
                            "jobs_found": 0,
                            "jobs_new": 0,
                            "status": f"failed: {s_exc}",
                        })
                        self._progress.errors.append(f"{src_name}: {s_exc}")

                    with self._lock:
                        self._progress.sources = list(sources_summary)
                        self._progress.total_jobs = total_seen
                        self._progress.new_jobs = total_new

            # Phase 2: Compute relevance for new jobs
            with self._lock:
                self._progress.stage = "relevance"
                self._progress.message = "Evaluating relevance rules for newly fetched jobs..."

            if not self._cancel_flag.is_set():
                try:
                    run_relevance(connection, config)
                except Exception as g_exc:
                    logger.warning("Relevance evaluation warning during first ingest: %s", g_exc)

            with self._lock:
                self._progress.stage = "done"
                self._progress.status = "completed"
                self._progress.message = f"First ingest complete: {total_new} new jobs found ({total_seen} seen)."
                self._progress.elapsed_seconds = time.monotonic() - start_time
                self._progress.estimated_time_remaining = 0
        finally:
            connection.close()
