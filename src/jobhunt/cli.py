"""Command line entry point: `python -m jobhunt <command>` or the `jobhunt` script.

Subcommands: init-db, ingest, discover, stats, gmail-auth, ingest-email, serve, backfill-remote
(and the matching layer's profile-build, relevance-run, embed, score, match, ... as they are added).
Every command is safe to run from cron or Windows Task Scheduler (exit code 0 on success).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from jobhunt import cmdhint
from jobhunt.config import Config, load_config
from jobhunt.db import backup_database, connect, migrate_with_backup
from jobhunt.discovery.pipeline import run_discovery
from jobhunt.envfile import load_secrets
from jobhunt.errors import JobHuntError
from jobhunt.http import HttpClient
from jobhunt.platform.paths import get_python_executable
from jobhunt.runner import IngestionRunner
from jobhunt.sources import build_all_sources, build_ats_sources
from jobhunt.stats import collect_stats, format_stats

logger = logging.getLogger("jobhunt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt", description="Personal job-discovery pipeline")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--data-dir", default=None, help="override data directory (default: ./data or JOBHUNT_DATA_DIR env var)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("init-db", help="create the database and apply migrations")

    ingest = subcommands.add_parser("ingest", help="fetch jobs from the ATS boards we know about")
    ingest.add_argument("--source", action="append", dest="sources", metavar="NAME",
                        help="only this source (repeatable), e.g. --source greenhouse")
    ingest.add_argument("--limit", type=int, metavar="N", help="at most N companies per source (smoke tests)")
    ingest.add_argument("--force", action="store_true",
                        help="ignore a feed's minimum interval (its daily fetch cap still applies)")

    discover = subcommands.add_parser("discover", help="find and verify new company boards")
    discover.add_argument("--seed", type=Path, metavar="FILE", help="YAML seed file (default: discovery.seed_file)")
    discover.add_argument("--common-crawl", dest="common_crawl", action=argparse.BooleanOptionalAction, default=None,
                          help="query the Common Crawl index (default: from config)")
    discover.add_argument("--openroles", action=argparse.BooleanOptionalAction, default=None,
                          help="fetch datascry/openroles slug lists (CC BY-SA 4.0; default: from config)")
    discover.add_argument("--snowball", action=argparse.BooleanOptionalAction, default=None,
                          help="harvest slugs from stored apply URLs (default: from config)")
    discover.add_argument("--no-probe", action="store_true", help="collect slugs but do not verify them")
    discover.add_argument("--probe-limit", type=int, metavar="N", help="probe at most N unverified companies")

    subcommands.add_parser("stats", help="print a summary of what has been ingested")

    install = subcommands.add_parser(
        "schedule-install", help="create daily automation in scheduler (prints the plan; needs --yes)")
    install.add_argument("--backend", choices=["windows_task_scheduler", "macos_launchd", "in_app"], default=None,
                         help="scheduler backend to install (default: from config)")
    install.add_argument("--yes", action="store_true", help="actually create the tasks")
    remove = subcommands.add_parser("schedule-remove", help="delete tasks from scheduler")
    remove.add_argument("--backend", choices=["windows_task_scheduler", "macos_launchd", "in_app"], default=None,
                        help="scheduler backend to remove (default: from config)")
    subcommands.add_parser("schedule-status", help="installed tasks, last runs, next runs")
    subcommands.add_parser("morning-check", help="run morning system diagnostic check and write data/morning_check.txt")
    scheduled = subcommands.add_parser(
        "scheduled-run", help="run one scheduled task with lock, log and bookkeeping (what Task Scheduler executes)")
    scheduled.add_argument("task", help="ingest | ollama-ensure | match | ingest-email | discover | triage | morning-check")
    scheduled.add_argument("--manual", action="store_true", help="record the run as manual, not scheduler")

    auth = subcommands.add_parser("gmail-auth", help="one-time (or periodic) read-only Gmail authorization")
    auth.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    auth.add_argument("--port", type=int, default=0, help="local redirect port (default: any free port)")

    email_cmd = subcommands.add_parser("ingest-email", help="ingest job-alert emails from Gmail (read-only)")
    email_cmd.add_argument("--since-days", type=int, metavar="N", help="look back N days instead of the stored position")
    email_cmd.add_argument("--max", type=int, dest="max_messages", metavar="N", help="at most N messages this run")
    email_cmd.add_argument("--no-resolve", action="store_true",
                           help="do not fetch ATS-hosted postings through the ATS APIs")

    default_serve_port = 8000
    if "PORT" in os.environ:
        try:
            default_serve_port = int(os.environ["PORT"])
        except ValueError:
            pass
    serve = subcommands.add_parser("serve", help="run the local website (API + built web app) on 127.0.0.1")
    serve.add_argument("--data-dir", default=None, help="override data directory")
    serve.add_argument(
        "--port",
        type=int,
        default=default_serve_port,
        help=f"port to listen on (default: {default_serve_port}; automatically selects an open port if occupied)",
    )
    serve.add_argument(
        "--allow-remote",
        nargs="?",
        const="0.0.0.0",
        default=None,
        help="explicitly allow binding to non-local interfaces (default: 0.0.0.0 when passed without argument)",
    )

    backfill = subcommands.add_parser(
        "backfill-remote", help="derive workplace type and who a remote job is open to (offline, incremental)")
    backfill.add_argument("--all", action="store_true", dest="force",
                          help="recompute every row, not only rows whose inputs changed (LLM-settled rows are kept)")
    backfill.add_argument("--open-only", action="store_true", help="skip closed jobs")

    score = subcommands.add_parser(
        "score", help="triage and score the daily shortlist with Gemini (paid, budget-capped; --dry-run is free)")
    score.add_argument("--k", type=int, metavar="N", help="shortlist size (default: scoring.top_k)")
    score.add_argument("--submit-batch", action="store_true",
                       help="send the shortlist to the Gemini Batch API (50%% price, results within 24 h)")
    score.add_argument("--collect-batch", action="store_true", help="apply the results of submitted batches")
    score.add_argument("--workplace-only", action="store_true",
                       help="only classify + the workplace check; skip the remote tie-break and scoring")
    score.add_argument("--dry-run", action="store_true",
                       help="show what would be sent with estimated tokens and cost; no network call")
    score.add_argument("--show-prompt", type=int, default=0, metavar="N",
                       help="with --dry-run: print the first N prompts in full (PII-free profile text + job text)")

    subcommands.add_parser(
        "triage", help="judge the top un-judged shortlist jobs (LLM verdicts, budget.triage_daily_calls) and re-run the gate")
    student = subcommands.add_parser(
        "triage-student", help="retrain the rules and classifier mined from the LLM verdicts (offline; needs 50+ new rows)")
    student.add_argument("--force", action="store_true", help="retrain even without 50 new verdicts or labels (never replaces a better model)")
    backfill = subcommands.add_parser(
        "triage-backfill", help="judge un-judged Relevant jobs with hard limits (resumable, cached; paid)")
    backfill.add_argument("--max-calls", type=int, default=10, metavar="N", help="at most N model calls (default 10)")
    backfill.add_argument("--max-usd", type=float, default=0.25, metavar="X", help="at most X dollars (default 0.25)")
    backfill.add_argument("--no-gate", action="store_true", help="do not re-run relevance-run afterwards")

    usage = subcommands.add_parser("usage", help="LLM tokens and estimated cost today and this month, against the budget")
    usage.add_argument("--project", action="store_true",
                       help="projected monthly Gemini cost at current settings, also after the announced price change")
    subcommands.add_parser("suggest-rules", help="print rule suggestions mined from dismissals (never edits config)")

    detect = subcommands.add_parser(
        "detect-ats", help="find which career platform each company in enterprise_companies.yaml runs (public pages only)")
    detect.add_argument("--only", nargs="*", metavar="NAME", help="check only these companies")
    detect.add_argument("--limit", type=int, metavar="N")

    tailor = subcommands.add_parser("tailor", help="tailor a resume to one job (validated, one page, LaTeX -> PDF)")
    tailor.add_argument("job_id", type=int)
    tailor.add_argument("--base", choices=["ai", "fullstack", "backend"], help="base resume (default: the job's recommended one)")
    tailor.add_argument("--rephrase", action="store_true", help=argparse.SUPPRESS)   # removed: the model never writes text
    tailor.add_argument("--no-llm", action="store_true", help="deterministic selection only, no model call")
    tailor.add_argument("--force", action="store_true", help="ignore the cache and tailor again")
    top = subcommands.add_parser(
        "tailor-top", help="MANUAL ONLY: tailor the top N jobs by final score; prints the estimated cost, needs --yes")
    top.add_argument("--yes", action="store_true", help="confirm that you want to spend the estimated amount")
    top.add_argument("n", type=int, nargs="?", default=10)
    top.add_argument("--no-llm", action="store_true")
    top.add_argument("--max-usd", type=float, default=0.25, help="stop after spending this much (default 0.25)")

    train = subcommands.add_parser(
        "train-ranker", help="train the local ranker on Gemini scores, your feedback and gold labels (free, seconds)")
    train.add_argument("--folds", type=int, default=5)
    subcommands.add_parser(
        "retrain-ranker", help="guarded retrain (10+ new labels, never replaces a better model; used by `match`)")

    match = subcommands.add_parser(
        "match", help="nightly: backfill-remote, relevance-run, embed, score (paid steps are skipped without a key)")
    match.add_argument("--skip-paid", action="store_true", help="only the offline steps")
    match.add_argument("--no-local-llm", action="store_true", help="skip the local Ollama LLM stage")

    subcommands.add_parser("rank", help="apply the trained local ranker to open jobs (free, seconds)")
    local = subcommands.add_parser("local-llm", help="run the optional local Ollama stage (time-boxed, then unloads)")
    local.add_argument("--time-box", type=float, metavar="SECONDS", help="override local_llm.time_box_seconds")

    embed = subcommands.add_parser(
        "embed", help="embed profiles and open target/maybe jobs, then compute similarity (paid; --dry-run is free)")
    embed.add_argument("--limit", type=int, metavar="N", help="embed at most N new job texts this run")
    embed.add_argument("--dry-run", action="store_true",
                       help="show what would be embedded with estimated tokens and cost; no network call")

    sample = subcommands.add_parser(
        "eval-sample", help="write a stratified sample of jobs to data/eval/gold.csv for me to label (never fills labels)")
    sample.add_argument("--size", type=int, default=60, help="about this many jobs (default 60)")
    sample.add_argument("--seed", type=int, default=20261014)
    sample.add_argument("--force", action="store_true", help="overwrite an existing gold.csv (it may hold labels)")

    evaluate = subcommands.add_parser(
        "eval-run", help="report gate precision/recall, ranking precision@10/20 and false rejections from gold.csv")
    evaluate.add_argument("--file", type=Path, help="gold file (default: data/eval/gold.csv)")

    subcommands.add_parser(
        "profile-build", help="build PII-stripped profiles from the resumes and write review_facts.md (offline)")

    relevance = subcommands.add_parser(
        "relevance-run", help="apply the deterministic relevance gate (offline, incremental; reasons are stored)")
    relevance.add_argument("--all", action="store_true", dest="force",
                           help="re-judge every open job, not only new / changed ones or after a config change")

    subcommands.add_parser(
        "freshness-run", help="store each open job's age band and move stale jobs to Auto-hidden (offline, no model)")
    subcommands.add_parser(
        "liveness-check", help="the daily shortlist check: is each candidate still open? (<= 100 polite requests, cached 24 h)")

    parse = subcommands.add_parser("parse-email", help="parse a saved alert email offline (no network, no database)")
    parse.add_argument("file", type=Path, help=".eml or .html file")
    parse.add_argument("--platform", required=True, help="linkedin, naukri, internshala, indeed, ...")

    t_train = subcommands.add_parser(
        "triage-train", help="train cascade classifiers across all heads and save models"
    )
    t_train.add_argument("--force", action="store_true", help="retrain even if fewer than 30 new labels exist")

    subcommands.add_parser(
        "triage-eval",
        help="run grouped CV, time split, coverage and learning curves, writes data/eval/triage_eval.md",
    )

    t_run = subcommands.add_parser(
        "triage-run",
        help="run 4-tier local triage cascade (rules -> classifier -> local LLM -> Gemini)",
    )
    t_run.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="print decisions without modifying DB (default: true)",
    )
    t_run.add_argument(
        "--apply",
        action="store_true",
        help="write decisions to DB and apply demotions to job_relevance",
    )
    t_run.add_argument(
        "--limit",
        type=int,
        default=300,
        help="max candidate jobs to evaluate (default: 300)",
    )

    subcommands.add_parser(
        "triage-shadow-report",
        help="report what shadow mode would have demoted and agreement with user actions",
    )

    t_llm_run = subcommands.add_parser(
        "triage-llm-run",
        help="run local-LLM-first triage over visible candidate jobs with time-box and reversible application",
    )
    t_llm_run.add_argument(
        "--apply",
        action="store_true",
        help="write decisions to DB and apply demotions/exclusions to job_relevance (default is dry run)",
    )
    t_llm_run.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        metavar="MINS",
        help="maximum execution minutes (default: from config triage.local_llm_max_minutes, 90)",
    )
    t_llm_run.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="max candidate jobs to judge this run",
    )

    t_bench = subcommands.add_parser(
        "triage-llm-benchmark",
        help="evaluate local LLM against Gemini verdicts and human labels, updating acting rules pass/fail table",
    )
    t_bench.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="max jobs to evaluate during benchmark",
    )
    t_bench.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        metavar="MINS",
        help="max time limit for benchmark run in minutes",
    )

    subcommands.add_parser(
        "triage-acting-rules",
        help="display current pass/fail status and precision of local LLM acting rules from DB",
    )

    t_audit = subcommands.add_parser(
        "triage-import-audit",
        help="import user audit labels from CSV, validate answers, print model accuracy, and update acting rules",
    )
    t_audit.add_argument("csv_file", type=Path, help="path to audit CSV file (e.g. data/eval/v10b_audit.csv)")

    snap = subcommands.add_parser("golden-snapshot", help="record deterministic golden snapshot")
    snap.add_argument("--out", type=Path, required=True, help="output directory for golden snapshot")

    cmp = subcommands.add_parser("golden-compare", help="compare two golden snapshots with zero difference assertion")
    cmp.add_argument("--before", type=Path, required=True, help="before snapshot directory")
    cmp.add_argument("--after", type=Path, required=True, help="after snapshot directory")

    subcommands.add_parser(
        "profile-import-legacy",
        help="generate data/profile/profile.yaml from current hardcoded parameters and files (DB backup first)",
    )

    doc_p = subcommands.add_parser(
        "doctor",
        help="diagnose environment, dependencies, tools, and permissions (pass/warn/fail)",
    )
    doc_p.add_argument("--data-dir", default=None, help="override data directory")
    doc_p.add_argument("--online", action="store_true", help="perform live network checks (e.g. Gemini key test)")
    doc_p.add_argument("--report", action="store_true", help="write scrubbed diagnostic report to doctor_report.txt")

    subcommands.add_parser(
        "feedback-template",
        help="print 10-question tester feedback template for reporting experience and bugs",
    )

    subcommands.add_parser(
        "upgrade",
        help="back up database, apply migrations, update dependencies and rebuild frontend (never runs git pull itself)",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # job titles are unicode; Windows consoles are not
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    arguments = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request INFO lines are noise
    for handler in logging.getLogger().handlers:          # suggested commands carry the venv interpreter if needed
        handler.addFilter(cmdhint.QualifyCommands(frozenset(_COMMANDS)))
    if arguments.command != "scheduled-run":
        cmdhint.warn_if_outside_venv(logger)
    try:
        config = load_config(Path(arguments.config), data_dir=arguments.data_dir)
        from jobhunt.profile import ensure_profile
        ensure_profile(config)
        if arguments.command in {"ingest", "discover", "ingest-email"} and not config.user_agent_contact.strip():
            logger.warning("user_agent_contact is empty in config.yaml: set it to a URL or email so site "
                           "operators can reach you (it is sent in the User-Agent header)")
        return _COMMANDS[arguments.command](arguments, config)
    except JobHuntError as error:
        logger.error("%s", error)
        return 2
    except KeyError as error:  # unknown --source name
        logger.error("%s", error.args[0] if error.args else error)
        return 2


def open_database(config: Config) -> sqlite3.Connection:
    """Connect and make sure the schema is current (cheap when nothing is pending)."""
    connection = connect(config.database_file)
    migrate_with_backup(connection, config.database_file, config.migrations_directory,
                        config.backup_directory)
    return connection


def _command_init_db(arguments: argparse.Namespace, config: Config) -> int:
    connection = connect(config.database_file)
    applied = migrate_with_backup(connection, config.database_file, config.migrations_directory,
                        config.backup_directory)
    if applied:
        print(f"Applied migrations: {', '.join(f'{version:03d}' for version in applied)}")
    else:
        print("Database already up to date.")
    print(f"Database: {config.database_file}")
    connection.close()
    return 0


def _command_ingest(arguments: argparse.Namespace, config: Config) -> int:
    connection = open_database(config)
    sources = build_all_sources(config, load_secrets(config.project_root))
    if not sources:
        logger.error("no sources enabled in config.yaml")
        return 2
    with HttpClient(config.http, config.user_agent) as http:
        runner = IngestionRunner(connection, config, sources, http)
        summaries = runner.run(arguments.sources, company_limit=arguments.limit, force=arguments.force)
    connection.close()

    print(f"{'source':<22} {'checked':>7} {'failed':>6} {'seen':>7} {'new':>6} {'merged':>6} {'closed':>6} {'prefiltered':>11}")
    for item in summaries:
        print(f"{item.source:<22} {item.companies_checked:>7} {item.companies_failed:>6} {item.jobs_seen:>7} "
              f"{item.jobs_new:>6} {item.jobs_merged:>6} {item.jobs_closed:>6} {item.jobs_skipped_by_prefilter:>11}")
        if item.skipped_reason:
            print(f"  - skipped: {item.skipped_reason}")
        if item.blocked:
            print("  - BLOCKED: the endpoint now requires credentials")
        for error in item.errors:
            if error["kind"] in {"warning", "suspicious_drop"}:
                print(f"  - {error['kind']}: {error['company']}: {error['message']}")
            elif error["kind"] != "skipped" and "/" not in error["company"]:
                print(f"  - FAILED {error['company']}: {error['kind']}: {error['message']}")
    return 0


def _command_discover(arguments: argparse.Namespace, config: Config) -> int:
    connection = open_database(config)
    sources = build_ats_sources(config)
    with HttpClient(config.http, config.user_agent) as http:
        summary = run_discovery(
            connection, config, sources, http,
            seed_path=arguments.seed,
            use_common_crawl=arguments.common_crawl,
            use_openroles=arguments.openroles,
            use_snowball=arguments.snowball,
            do_probe=not arguments.no_probe,
            probe_limit=arguments.probe_limit,
        )
    connection.close()

    for step, count in summary.added.items():
        print(f"{step:<12} {count:>7} new unverified companies")
    if summary.probe:
        probe = summary.probe
        print(f"probe        probed={probe.probed} promoted={probe.promoted} failed={probe.failed} "
              f"deactivated={probe.deactivated} transient={probe.transient}")
    return 0


def _command_schedule_install(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.schedule.backends import get_scheduler_backend

    backend_name = getattr(arguments, "backend", None)
    backend = get_scheduler_backend(backend_name, config, config_path=Path(arguments.config).resolve())
    print(backend.describe_plan())
    if not arguments.yes:
        print("\nNothing was created. Re-run with --yes to create these tasks.")
        return 0
    results = backend.install()
    for name, ok, message in results:
        print(f"{'created ' if ok else 'FAILED  '} {name}  {message}")
    return 0 if all(ok for _, ok, _ in results) else 1


def _command_schedule_remove(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.schedule.backends import get_scheduler_backend

    backend_name = getattr(arguments, "backend", None)
    backend = get_scheduler_backend(backend_name, config, config_path=Path(arguments.config).resolve())
    results = backend.remove()
    if not results:
        print(f"no tasks found for backend '{backend.name}'")
    for name, ok, message in results:
        print(f"{'removed ' if ok else 'FAILED  '} {name}  {message}")
    return 0 if all(ok for _, ok, _ in results) else 1


def _command_schedule_status(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.schedule.backends import get_scheduler_backend
    from jobhunt.schedule.health import build_schedule_health
    from jobhunt.timeutil import utc_now

    backend = get_scheduler_backend(config.schedule.backend, config, config_path=Path(arguments.config).resolve())
    installed = {row["TaskName"]: row for row in backend.installed_tasks()}
    connection = open_database(config)
    report = build_schedule_health(connection, config, utc_now())
    connection.close()
    print(f"Active Scheduler Backend: {report['backend']}\n")
    print(f"{'task':<24} {'installed':<9} {'last run (UTC)':<21} {'status':<8} {'secs':>6}  {'next run (UTC)':<21} missed")
    for task in report["tasks"]:
        row = (
            installed.get(task["name"])
            or installed.get(f"com.jobhunt.{task['task']}")
            or installed.get(f"in_app:{task['task']}")
        )
        seconds = task["last_duration_seconds"]
        print(f"{task['name']:<24} {'yes' if row else 'NO':<9} {(task['last_run_at'] or '-')[:19]:<21} "
              f"{(task['last_status'] or '-'):<8} {('%.0f' % seconds) if seconds is not None else '-':>6}  "
              f"{(task['next_run_at'] or '-')[:19]:<21} {task['missed_runs']}{'  !!' if task['missed_warning'] else ''}")
    missed_tasks = report.get("missed_runs", [])
    if missed_tasks:
        print(f"\nMissed runs ({len(missed_tasks)}):")
        for m in missed_tasks:
            print(f"  - {m['name']} ({m['task']}): {m['missed_runs']} missed run(s)")
    else:
        print("\nMissed runs: none (all tasks up to date)")
    if report["gmail_auth_expired"]:
        print(f"\nGmail authorization problem: {report['gmail_auth_reason']}\n"
              f"Ingesting alert e-mails is paused until you re-authorize (needs a browser, run it yourself):\n  "
              f"{report['gmail_reauth_command']}")
    return 0


def _command_scheduled_run(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.schedule.runner import TaskRunner

    connection = open_database(config)
    try:
        runner = TaskRunner(config, connection, Path(arguments.config).resolve())
        return runner.run(arguments.task, trigger="manual" if arguments.manual else "scheduler")
    finally:
        connection.close()


def _command_stats(arguments: argparse.Namespace, config: Config) -> int:
    connection = open_database(config)
    print(format_stats(collect_stats(connection)))
    connection.close()
    return 0


def _command_gmail_auth(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.alerts.gmail import GmailApiClient, TokenStore, run_auth_flow

    credentials_file = config.resolve_path(config.email.credentials_file)
    token_file = config.resolve_path(config.email.token_file)
    run_auth_flow(credentials_file, token_file, port=arguments.port, open_browser=not arguments.no_browser)
    with HttpClient(config.http, config.user_agent) as http:
        account = GmailApiClient(http, TokenStore(credentials_file, token_file).bearer_token).profile_email()
    expected = config.email.account.lower()
    if expected and account != expected:
        token_file.unlink(missing_ok=True)
        logger.error("authorized %s but config.yaml expects %s; token removed. Run again with the right account.",
                     account, expected)
        return 2
    print(f"Authorized read-only access for {account}. Token saved to {token_file}")
    return 0


def _command_ingest_email(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.alerts.gmail import GmailApiClient, MailAuthError, TokenStore
    from jobhunt.alerts.pipeline import ingest_emails

    connection = open_database(config)
    store = TokenStore(config.resolve_path(config.email.credentials_file), config.resolve_path(config.email.token_file))
    with HttpClient(config.http, config.user_agent) as http:
        client = GmailApiClient(http, store.bearer_token)
        account = client.profile_email()
        if config.email.account and account != config.email.account.lower():
            raise MailAuthError(f"token belongs to {account}, config.yaml expects {config.email.account}")
        summary = ingest_emails(
            connection, config, client, http=http, ats_sources=build_ats_sources(config),
            resolve_ats=False if arguments.no_resolve else None,
            since_days=arguments.since_days, limit=arguments.max_messages,
        )
    connection.close()
    print(f"messages: listed={summary.messages_listed} new={summary.messages_new} "
          f"skipped_sender={summary.messages_skipped_sender}")
    print(f"jobs:     parsed={summary.jobs_parsed} new={summary.jobs_new} merged={summary.jobs_merged} "
          f"prefiltered={summary.jobs_skipped_by_prefilter} closed={summary.jobs_closed}")
    print(f"ats:      companies discovered={summary.companies_discovered} resolved={summary.ats_companies_resolved}")
    for error in summary.errors:
        print(f"  ! {error}")
    return 0 if not summary.errors else 1


def _command_parse_email(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.alerts.gmail import RawEmail, message_from_eml
    from jobhunt.alerts.pipeline import parse_email
    from jobhunt.alerts.urls import PLATFORMS
    from jobhunt.locations import LocationParser

    if arguments.platform not in PLATFORMS:
        logger.error("unknown platform %r (choose from %s)", arguments.platform, ", ".join(PLATFORMS))
        return 2
    data = arguments.file.read_bytes()
    if arguments.file.suffix.lower() == ".eml":
        message = message_from_eml(data, message_id=arguments.file.name)
    else:
        message = RawEmail(arguments.file.name, "", None, data.decode("utf-8", errors="replace"), "")
    jobs = parse_email(arguments.platform, message, LocationParser(config.classification.extra_city_countries))
    print(f"{len(jobs)} job card(s) found (links are shown as stored; nothing was fetched)")
    for job in jobs:
        kind = "ats" if job.ats else ("link" if job.recognized else "raw-link")
        print(f"- {job.title} | {job.company or '?'} | {job.location or '?'} | {kind}: {job.url}")
    return 0


def _command_detect_ats(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.enterprise.detect import detect_all
    from jobhunt.enterprise.polite import PoliteFetcher
    from jobhunt.enterprise.registry import sync_from_yaml

    connection = open_database(config)
    added = sync_from_yaml(connection, config)
    fetcher = PoliteFetcher(config.user_agent, requests_per_minute=20)
    try:
        summary = detect_all(connection, fetcher, only=arguments.only, limit=arguments.limit)
    finally:
        fetcher.close()
    print(f"companies from yaml: {added} new; checked {summary.checked}; added to boards table: "
          f"{summary.added_to_companies}")
    for platform, count in sorted(summary.by_platform.items(), key=lambda item: -item[1]):
        print(f"  {platform:<16} {count}")
    print("status: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.by_status.items())))
    for host, reason in summary.blocked_hosts.items():
        print(f"  blocked {host}: {reason}")
    connection.close()
    return 0


def _tailor_services(config: Config, use_llm: bool):  # noqa: ANN202
    """(connection, Tailor, closer). The compiler is None when no LaTeX engine exists (the .tex is still written)."""
    from jobhunt.tailoring.service import make_tailor

    connection = open_database(config)
    tailor, closer = make_tailor(connection, config, use_llm=use_llm)
    return connection, tailor, closer


def _print_tailor_result(result) -> None:  # noqa: ANN001
    violations = result.validation.get("violations", []) if result.validation else []
    print(f"job {result.job_id}: base={result.base} mode={result.mode} status={result.status} pages={result.pages} "
          f"cost=${result.cost_usd:.4f} tokens={result.input_tokens}/{result.output_tokens}")
    if result.pdf_path:
        print(f"  pdf: {result.pdf_path}")
    elif result.tex_path:
        print(f"  tex: {result.tex_path}")
    info = result.plan or {}
    if info:
        print(f"  recipe={info.get('recipe')} summary={info.get('summary_variant')} fill={info.get('fill')} "
              f"bullets={info.get('bullets_output')}/{info.get('bullets_base')} model={result.model or '-'}")
        if info.get("missing"):
            print(f"  missing keywords (not on your resume, never added): {', '.join(info['missing'])}")
    if result.dropped:
        print(f"  dropped for the page: {', '.join(result.dropped)}")
    for violation in violations[:6]:
        print(f"  validator: {violation['rule']}: {violation['offending']}")
    if result.message:
        print(f"  note: {result.message}")


def _command_tailor(arguments: argparse.Namespace, config: Config) -> int:
    connection, tailor, client = _tailor_services(config, not arguments.no_llm)
    try:
        result = tailor.tailor(arguments.job_id, base=arguments.base, rephrase=True if arguments.rephrase else None,
                               use_llm=not arguments.no_llm, force=arguments.force)
    except LookupError as error:
        logger.error("%s", error)
        return 2
    finally:
        if client is not None:
            client.close()
    connection.close()
    _print_tailor_result(result)
    return 0 if result.status in {"ok", "fallback", "cached"} else 1


def _command_tailor_top(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.budget import estimate_cost
    from jobhunt.timeutil import utc_now

    limit = min(arguments.n, config.tailoring.top_n_max)
    per_call = estimate_cost(config.pricing, config.tailoring.model, 3000, 2500, utc_now().date())
    estimate = 0.0 if arguments.no_llm else per_call * limit
    print(f"tailor-top would tailor up to {limit} jobs; estimated model cost about ${estimate:.2f} "
          f"({'no model calls' if arguments.no_llm else config.tailoring.model}). Tailoring is normally one job per "
          "click in the site.")
    if not arguments.yes:
        print("Nothing was done. Re-run with --yes to proceed.")
        return 0
    connection, tailor, client = _tailor_services(config, not arguments.no_llm)
    rows = connection.execute(
        """SELECT j.id FROM jobs j JOIN job_relevance r ON r.job_id = j.id
           WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe') AND r.final_score IS NOT NULL
             AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
             AND COALESCE(r.seniority_level, 'unspecified') IN ('junior', 'unspecified')
             AND NOT EXISTS (SELECT 1 FROM job_feedback_current f WHERE f.job_id = j.id AND f.action = 'dismiss')
           ORDER BY r.final_score DESC LIMIT ?""", (limit,)).fetchall()
    done = failed = cached = 0
    spent = 0.0
    llm_allowed = not arguments.no_llm
    try:
        for row in rows:
            result = tailor.tailor(int(row["id"]), use_llm=llm_allowed)
            _print_tailor_result(result)
            spent += result.cost_usd
            cached += result.status == "cached"
            done += result.status in {"ok", "fallback"}
            failed += result.status == "failed"
            if llm_allowed and ("budget" in result.message or spent >= arguments.max_usd):
                # a cap is reached: no more model calls, the rest of the list uses the free deterministic path
                print("model calls stopped (budget cap or --max-usd reached); continuing deterministically")
                llm_allowed = False
    finally:
        if client is not None:
            client.close()
    connection.close()
    print(f"tailor-top: tailored={done} cached={cached} failed={failed} spent=${spent:.4f}")
    return 0 if not failed else 1


def _command_retrain_ranker(arguments: argparse.Namespace, config: Config) -> int:
    """Guarded retrain used by `match` (numpy only): needs 10+ new labels, never replaces a better model."""
    from jobhunt.matching import ranker

    connection = open_database(config)
    try:
        print(ranker.describe_decision(ranker.retrain_if_needed(connection, config)))
    finally:
        connection.close()
    return 0


def _command_rank(arguments: argparse.Namespace, config: Config) -> int:
    import time

    from jobhunt.matching import ranker
    from jobhunt.matching.local_llm import record_stage

    connection = open_database(config)
    started = time.perf_counter()
    stored = ranker.predict_all(connection, config)
    record_stage(connection, "ranker", time.perf_counter() - started, stored, "local ranker",
                 status="ok" if stored else "skipped", detail=None if stored else "no trained model (run train-ranker)")
    connection.close()
    print(f"ranker: predicted {stored} open jobs" if stored else f"ranker: no trained model yet (run {cmdhint.command('train-ranker')})")
    return 0


def _command_local_llm(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.local_llm import record_stage, run_local_llm
    from jobhunt.matching.ollama import OllamaClient

    connection = open_database(config)
    with OllamaClient() as client:
        stats = run_local_llm(connection, config, client, time_box_seconds=arguments.time_box)
    status = "ok" if stats.available and not stats.errors else ("skipped" if not stats.available else "failed")
    record_stage(connection, "local_llm", stats.seconds, stats.workplace + stats.classified + stats.prescored,
                 config.local_llm.model, status=status, detail=stats.stopped_reason or "; ".join(stats.errors[:2]))
    connection.close()
    print(f"local LLM ({stats.model}): workplace={stats.workplace} classified={stats.classified} "
          f"prescored={stats.prescored} cached={stats.skipped_cached} failed={stats.failed} in {stats.seconds}s")
    if stats.stopped_reason:
        print(f"stopped: {stats.stopped_reason}")
    return 0


def _command_train_ranker(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching import ranker

    connection = open_database(config)
    report = ranker.train(connection, config, folds=arguments.folds)
    if not report.trained:
        print(f"not trained: {report.reason}; label sources: {report.label_sources}")
        connection.close()
        return 1
    stored = ranker.predict_all(connection, config)
    connection.close()
    print(f"trained {report.chosen} on {report.rows} labels {report.label_sources}")
    for name, metrics in report.cv.items():
        print(f"  cross-validation {name:<18} " + "  ".join(f"{key}={value}" for key, value in metrics.items()))
    print(f"usable labels: {report.rows} ({report.positives} relevant); gold rows used: {report.gold_rows}")
    print(f"saved to {report.path}; predicted {stored} open jobs")
    print("note: a few hundred labels is small. The ranker only orders and selects; it never excludes.")
    return 0


def _command_backfill_remote(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.locations import LocationParser
    from jobhunt.matching.remote import RemoteDeriver, refresh_remote

    connection = open_database(config)
    deriver = RemoteDeriver(LocationParser(config.classification.extra_city_countries), config.user.home_country)
    stats = refresh_remote(connection, deriver, config.user.home_country,
                           force=arguments.force, only_open=arguments.open_only)
    connection.close()
    print(f"examined={stats.examined} updated={stats.updated} unchanged={stats.skipped_current} "
          f"kept_llm_verdict={stats.skipped_llm}")
    if stats.updated:
        print("workplace:   " + "  ".join(f"{key}={value}" for key, value in sorted(stats.by_workplace.items())))
        print(f"open to {config.user.home_country}: " + "  ".join(
            f"{key}={value}" for key, value in sorted(stats.by_eligibility.items())))
    return 0


def _command_profile_build(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.profiles import run_profile_build

    connection = open_database(config)
    summary = run_profile_build(connection, config)
    connection.close()
    for profile in summary.profiles:
        removed = ", ".join(f"{key}={value}" for key, value in sorted(profile.removed.items())) or "nothing"
        print(f"profile {profile.name:<10} {profile.source_file:<22} {len(profile.text):>6} chars  "
              f"sha256={profile.text_hash[:12]}  PII removed: {removed}")
    for name in summary.skipped_optional:
        print(f"optional base '{name}': no resume file found, skipped (write resume_{name}.tex to add it)")
    if summary.facts_path:
        print(f"review list: {summary.facts_path}  ({summary.conflicts} conflicts, {summary.unconfirmed} unconfirmed)")
    tex = summary.tex_facts
    if tex is not None and getattr(tex, "facts", 0):
        print(f"facts from .tex: {tex.facts} ({tex.ticked} ticked; {tex.preserved} ticks kept, "
              f"{tex.carried_over} carried over from the confirmed PDFs) -> {tex.path}")
        print(f"NEW or CHANGED, unticked and unused until you tick them: {len(tex.new_or_changed)}")
        for fact in tex.new_or_changed:
            print(f"  {fact.id} [{fact.kind}] {fact.text[:110]}")
    return 0


def _command_score(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.budget import BudgetGuard, UsageLedger
    from jobhunt.matching.gemini import GeminiError
    from jobhunt.matching.scoring import run_score

    connection = open_database(config)
    ledger = UsageLedger(connection, config.pricing)
    guard = BudgetGuard(ledger, config.budget)

    if arguments.submit_batch or arguments.collect_batch:
        from jobhunt.matching.scoring import collect_score_batch, submit_score_batch

        try:
            with _gemini_client(config) as client:
                if arguments.collect_batch:
                    collected = collect_score_batch(connection, config, client, ledger)
                    print(collected.message)
                if arguments.submit_batch:
                    submitted = submit_score_batch(connection, config, client, ledger, guard, k=arguments.k)
                    print(submitted.message)
        except GeminiError as error:
            logger.error("%s", error)
            connection.close()
            return 2
        connection.close()
        return 0

    if arguments.dry_run:
        stats = run_score(connection, config, None, ledger, guard, k=arguments.k, dry_run=True)
        connection.close()
        if stats.stopped_reason:
            print(stats.stopped_reason)
            return 2
        print(f"DRY RUN (no network). models: classify={config.models.classify} score={config.models.score}")
        print("note: maybe-jobs the classifier calls relevant join the score shortlist only after a real run.")
        for kind, (count, tokens, usd) in sorted(stats.planned_totals().items()):
            print(f"  {kind:<9} calls={count:<4} input_tokens~{tokens:<8,} est_cost=${usd:.4f}")
        print(f"  total     calls={stats.calls}  input_tokens~{stats.input_tokens:,}  est_cost=${stats.estimated_usd:.4f}"
              f"  (output tokens assumed; thinking tokens bill as output)")
        if stats.injection_flagged:
            print(f"  {stats.injection_flagged} posting(s) contain instruction-like text that would be removed before sending")
        print("would send (job, company):")
        for call in stats.planned[:40]:
            print(f"  [{call.kind:<8}] #{call.job_id} {call.title[:60]} | {call.company[:24]}  ~{call.input_tokens} tok")
        for call in stats.planned[: arguments.show_prompt]:
            print("\n" + "=" * 78 + f"\n#{call.job_id} {call.kind} -> {call.model}\n--- system ---\n{call.system}"
                  f"\n--- user ---\n{call.user}")
        return 0

    try:
        with _gemini_client(config) as client:
            stats = run_score(connection, config, client, ledger, guard, k=arguments.k,
                              workplace_only=arguments.workplace_only)
    except GeminiError as error:
        logger.error("%s", error)
        connection.close()
        return 2
    summary = ledger.summary()
    connection.close()
    print(f"classified={stats.classified} remote_resolved={stats.remote_resolved} "
          f"workplace_resolved={stats.workplace_resolved} scored={stats.scored} "
          f"failed={stats.failed} calls={stats.calls} final_scores={stats.final_scores}")
    print(f"tokens in={stats.input_tokens:,} out={stats.output_tokens:,} est_cost=${stats.estimated_usd:.4f}"
          f"  (month so far ${summary.usd_month:.4f} of ${config.budget.monthly_usd:.2f})")
    if stats.injection_flagged:
        print(f"{stats.injection_flagged} posting(s) had instruction-like text (removed; score capped)")
    if stats.stopped_reason:
        print(f"stopped early: {stats.stopped_reason}")
    for error in stats.errors[:5]:
        print(f"  ! {error}")
    return 0 if not stats.failed else 1


def _command_usage(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.budget import UsageLedger

    connection = open_database(config)
    summary = UsageLedger(connection, config.pricing).summary()
    models = connection.execute(
        """SELECT model, purpose, SUM(calls) AS calls, SUM(items) AS items, SUM(input_tokens) AS tin,
                  SUM(output_tokens) AS tout, SUM(estimated_usd) AS usd
           FROM llm_usage WHERE substr(day, 1, 7) = ? GROUP BY model, purpose ORDER BY usd DESC""",
        (summary.month,),
    ).fetchall()
    connection.close()
    budget = config.budget
    print(f"today ({summary.day}):  pipeline calls={summary.generate_calls_today}/{budget.daily_calls}  "
          f"interactive calls={summary.interactive_calls_today}/{budget.interactive_daily_calls}  "
          f"embedded texts={summary.embed_texts_today}/{budget.daily_embedding_texts}  est=${summary.usd_today:.4f}")
    print(f"month ({summary.month}): est=${summary.usd_month:.4f} of ${budget.monthly_usd:.2f}  "
          f"tokens in={summary.tokens_in_month:,} out={summary.tokens_out_month:,}")
    for row in models:
        print(f"  {row['model']:<24} {row['purpose']:<9} calls={row['calls']:<5} items={row['items']:<6} "
              f"in={row['tin']:<9,} out={row['tout']:<8,} ${row['usd']:.4f}")
    print(f"prices as of {config.pricing.as_of} ({config.pricing.source})")
    if arguments.project:
        from jobhunt.matching.budget import project_monthly
        from jobhunt.timeutil import utc_now

        connection = open_database(config)
        projection = project_monthly(connection, config, utc_now().date())
        connection.close()
        print("\nprojection at current settings (nightly scoring, 30 days):")
        print(f"  {projection.calls_per_day} jobs/day x {projection.model}: avg {projection.avg_input_tokens:,} input + "
              f"{projection.avg_output_tokens:,} output tokens per job (from the last {projection.based_on_calls} "
              f"scored calls)")
        print(f"  ${projection.usd_per_job:.4f} per job -> ${projection.usd_per_month:.2f} / month")
        if projection.price_change_on:
            print(f"  after {projection.price_change_on} (announced price change): "
                  f"${projection.usd_per_month_after_price_change:.2f} / month")
        print(f"  embeddings: {'free (local, on your GPU)' if config.embedding.provider == 'local' else 'Gemini, billed per new job'}")
    return 0


def _command_suggest_rules(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.feedback import format_suggestions, suggest_rules

    connection = open_database(config)
    print(format_suggestions(suggest_rules(connection)))
    connection.close()
    return 0


def connection_has_profiles(config: Config) -> bool:
    connection = open_database(config)
    try:
        return connection.execute("SELECT 1 FROM profiles LIMIT 1").fetchone() is not None
    finally:
        connection.close()


def _command_match(arguments: argparse.Namespace, config: Config) -> int:
    """The nightly entry point: offline steps always, paid steps only with a key and budget."""
    import time

    steps: list[tuple[str, int, float]] = []

    def run(command: str) -> int:
        started = time.perf_counter()
        args = command.split()
        namespace = build_parser().parse_args(args)
        cmd_name = args[0]
        code = _COMMANDS[cmd_name](namespace, config)
        steps.append((cmd_name, code, time.perf_counter() - started))
        return code

    run("backfill-remote")
    run("relevance-run")
    has_key = bool(load_secrets(config.project_root).get("GEMINI_API_KEY"))
    has_profiles = connection_has_profiles(config)
    if not has_profiles:
        logger.warning("no profiles yet: run `python -m jobhunt profile-build`; skipping embed, rank and score")
    else:
        local_embeddings = config.embedding.provider == "local"
        if local_embeddings or (has_key and not arguments.skip_paid):
            run("embed")                       # local: free, on the GPU; gemini: billed per new job
        run("retrain-ranker")                  # 10+ new labels and not worse than the old model, else keep the old one
        run("rank")                            # the trained ranker orders unscored jobs (no-op without a model)
        if has_key and not arguments.skip_paid and config.triage.enabled:
            run("triage-run --apply")          # 4-tier cascade: rules -> classifier -> local LLM -> Gemini
            run("triage-train")                # retrain cascade if >= 30 new labels and passes guard
        if config.local_llm.enabled and not arguments.no_local_llm:
            run("local-llm")                   # sequential after embeddings; unloads the model when done
        if not arguments.skip_paid:
            run("liveness-check")              # free: polite "still open?" checks, so a closed posting costs no Gemini call
        if has_key and not arguments.skip_paid:
            if config.scoring.batch:
                batch_args = build_parser().parse_args(["score", "--collect-batch", "--submit-batch"])
                started = time.perf_counter()
                code = _command_score(batch_args, config)
                steps.append(("score(batch)", code, time.perf_counter() - started))
            else:
                run("score")
        else:
            logger.info("Gemini scoring skipped (no GEMINI_API_KEY or --skip-paid)")
    print("match: " + "  ".join(
        f"{name}={'ok' if code == 0 else 'exit ' + str(code)}({seconds:.0f}s)" for name, code, seconds in steps))
    return 0 if all(code == 0 for _, code, _ in steps) else 1


def _triage_client(config: Config):  # noqa: ANN202
    """A Gemini client when a key exists, else None (triage then skips silently)."""
    return _gemini_client(config) if load_secrets(config.project_root).get("GEMINI_API_KEY") else None


def _command_triage(arguments: argparse.Namespace, config: Config) -> int:
    """Daily guard: judge the top un-judged jobs within the daily triage limit; skip silently when blocked."""
    from jobhunt.matching.budget import BudgetGuard, UsageLedger
    from jobhunt.matching.triage_run import run_daily

    connection = open_database(config)
    ledger = UsageLedger(connection, config.pricing)
    guard = BudgetGuard(ledger, config.budget)
    client = _triage_client(config)
    try:
        result = run_daily(connection, config, client, ledger, guard)
    finally:
        if client is not None:
            client.close()
    connection.commit()
    connection.close()
    print(result.line())
    if result.run.judged:
        _command_relevance_run(build_parser().parse_args(["relevance-run"]), config)
    return 0


def _command_triage_student(arguments: argparse.Namespace, config: Config) -> int:
    """Guarded retrain of the student (numpy only, no model call); re-runs the gate when a new model was written."""
    from jobhunt.matching import triage_student

    connection = open_database(config)
    try:
        decision = triage_student.retrain_if_needed(connection, config, force=arguments.force)
    finally:
        connection.close()
    print(triage_student.describe_decision(decision))
    if decision["action"] == "retrained":
        _command_relevance_run(build_parser().parse_args(["relevance-run"]), config)
    return 0


def _command_triage_backfill(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.budget import BudgetGuard, UsageLedger
    from jobhunt.matching.triage_run import run_backfill

    client = _triage_client(config)
    if client is None:
        print("triage-backfill needs GEMINI_API_KEY in .env")
        return 2
    connection = open_database(config)
    ledger = UsageLedger(connection, config.pricing)
    guard = BudgetGuard(ledger, config.budget)
    try:
        run = run_backfill(connection, config, client, ledger, guard, max_calls=arguments.max_calls, max_usd=arguments.max_usd)
    finally:
        client.close()
    connection.commit()
    connection.close()
    print(f"triage-backfill: judged={run.judged} failed={run.failed} calls={run.calls} tokens in={run.input_tokens:,} "
          f"out={run.output_tokens:,} est_cost=${run.estimated_usd:.4f} hallucinated_fields={run.hallucinated_fields}"
          f"/{run.claimed_fields}" + (f"; stopped: {run.stopped}" if run.stopped else ""))
    for error in run.errors[:5]:
        print(f"  ! {error}")
    if run.judged and not arguments.no_gate:
        _command_relevance_run(build_parser().parse_args(["relevance-run"]), config)
    return 0 if not run.failed else 1


def _gemini_client(config: Config):  # noqa: ANN202
    from jobhunt.matching.gemini import GeminiClient

    secrets = load_secrets(config.project_root)
    return GeminiClient(secrets.get("GEMINI_API_KEY", ""), config.gemini)


def _command_embed(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.budget import BudgetGuard, UsageLedger, estimate_cost
    from jobhunt.matching.embeddings import collect_pending, compute_similarity, embed_jobs, embed_profiles
    from jobhunt.matching.gemini import GeminiError
    from jobhunt.timeutil import utc_now

    connection = open_database(config)
    if not connection.execute("SELECT 1 FROM profiles LIMIT 1").fetchone():
        logger.error("no profiles yet: run `python -m jobhunt profile-build` first")
        return 2
    if not connection.execute("SELECT 1 FROM job_relevance LIMIT 1").fetchone():
        logger.error("no relevance verdicts yet: run `python -m jobhunt relevance-run` first")
        return 2

    if arguments.dry_run:
        pending, stats = collect_pending(connection, config, limit=arguments.limit)
        cost = estimate_cost(config.pricing, config.embedding.model, stats.tokens, 0, utc_now().date())
        print(f"DRY RUN (no network): model={config.embedding.model} dims={config.embedding.dimensions}")
        print(f"candidates={stats.candidates} already_embedded={stats.cached} would_embed={stats.to_embed} "
              f"batches={-(-stats.to_embed // config.embedding.batch_size)}")
        print(f"estimated input tokens={stats.tokens:,}  estimated cost=${cost:.4f}")
        connection.close()
        return 0

    import time

    from jobhunt.matching.embeddings import effective_config
    from jobhunt.matching.ollama import OllamaClient, OllamaError

    embed_started = time.perf_counter()

    config = effective_config(config)          # provider: local -> ollama:<model>, no daily text cap
    ledger = UsageLedger(connection, config.pricing)
    guard = BudgetGuard(ledger, config.budget)
    try:
        if config.embedding.provider == "local":
            with OllamaClient() as local_client:
                if not local_client.available():
                    logger.error("embedding.provider is local but Ollama is not running on 127.0.0.1:11434")
                    connection.close()
                    return 2
                profiles_done = embed_profiles(connection, config, local_client, ledger, guard)
                stats = embed_jobs(connection, config, local_client, ledger, guard, limit=arguments.limit)
                local_client.unload(config.embedding.model)
        else:
            with _gemini_client(config) as client:
                profiles_done = embed_profiles(connection, config, client, ledger, guard)
                stats = embed_jobs(connection, config, client, ledger, guard, limit=arguments.limit)
    except (GeminiError, OllamaError) as error:
        logger.error("%s", error)
        connection.close()
        return 2
    similarity = compute_similarity(connection, config)
    summary = ledger.summary()
    from jobhunt.matching.local_llm import record_stage

    record_stage(connection, "embed", time.perf_counter() - embed_started, stats.embedded,
                 config.embedding.model, status="ok" if not stats.failed else "failed",
                 detail=stats.stopped_reason)
    connection.close()
    print(f"profiles embedded={profiles_done}")
    print(f"jobs: candidates={stats.candidates} cached={stats.cached} embedded={stats.embedded} failed={stats.failed} "
          f"batches={stats.batches} est_cost=${stats.estimated_usd:.4f}")
    if stats.stopped_reason:
        print(f"stopped early: {stats.stopped_reason}")
    print(f"similarity: scored={similarity.jobs_scored} best_profile={similarity.best_profile_counts} "
          f"({similarity.seconds:.2f}s)")
    print(f"spend this month: ${summary.usd_month:.4f} of ${config.budget.monthly_usd:.2f}")
    return 0 if not stats.failed else 1


def _gold_path(config: Config, override: Path | None = None) -> Path:
    from jobhunt.matching.ranker import resolve_gold_path

    return resolve_gold_path(config, override)


def _command_eval_sample(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.evaluation import write_sample

    connection = open_database(config)
    path = _gold_path(config)
    counts = write_sample(connection, path, size=arguments.size, seed=arguments.seed, force=arguments.force)
    connection.close()
    print(f"wrote {sum(counts.values())} jobs to {path}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Fill the `relevant` column with yes / no, then run `{cmdhint.command('eval-run')}`.")
    return 0


def _command_eval_run(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.evaluation import format_report, run_eval

    connection = open_database(config)
    report = run_eval(connection, _gold_path(config, arguments.file))
    print(format_report(report))
    print("\nNote: gold.csv labels judge ROLE fit only (location ignored); the ranker is not trained on location.")
    for row in connection.execute(
            """SELECT COALESCE(r.location_fit, 'not computed') AS fit, COUNT(*) AS n FROM job_relevance r
               JOIN jobs j ON j.id = r.job_id WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
               GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  location_fit {row['fit']:<26} {row['n']}")
    connection.close()
    return 0


def _command_relevance_run(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.gate import run_relevance

    from jobhunt.matching.company_rules import COMPANY_RULES_FILE, ensure_template

    if ensure_template(config.resolve_path(COMPANY_RULES_FILE)):      # commented template + the Barclays rule, once
        print(f"created {COMPANY_RULES_FILE} (your company grade rules; git-ignored)")
    connection = open_database(config)
    stats = run_relevance(connection, config, force=arguments.force)
    from jobhunt.matching.scoring import compute_final_scores

    compute_final_scores(connection, config)          # applies eligibility caps / learned penalties (no model call)
    from jobhunt.matching.location import run_location

    location = run_location(connection, config)
    from jobhunt.freshness import run_freshness

    fresh = run_freshness(connection, config)         # age bands + Auto-hidden (stale jobs never reach the scoring pool)
    totals = {row["gate_status"]: row["n"] for row in connection.execute(
        """SELECT r.gate_status, COUNT(*) AS n FROM job_relevance r JOIN jobs j ON j.id = r.job_id
           WHERE j.status = 'open' GROUP BY 1""")}
    connection.close()
    print(f"examined={stats.examined} judged={stats.computed} unchanged={stats.unchanged} rules={stats.rules_hash}")
    print("open jobs now: " + "  ".join(f"{key}={totals.get(key, 0)}" for key in ("target", "maybe", "excluded")))
    if config.freshness.enabled:
        print("freshness (open jobs): " + "  ".join(f"{key}={value}" for key, value in sorted(fresh.by_band.items()))
              + f"  | auto-hidden now +{fresh.newly_hidden} -{fresh.unhidden}")
    print("location fit (all open jobs): " + "  ".join(f"{key}={value}" for key, value in sorted(location.by_fit.items())))
    if stats.computed:
        print("this run, by rule:")
        for rule, count in sorted(stats.by_rule.items(), key=lambda item: -item[1]):
            print(f"  {rule:<34} {count:>6}")
    return 0


def _command_freshness_run(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.freshness import run_freshness

    connection = open_database(config)
    stats = run_freshness(connection, config)
    connection.close()
    print(f"examined={stats.examined} bands: " + "  ".join(f"{key}={value}" for key, value in sorted(stats.by_band.items())))
    print(f"auto-hidden: +{stats.newly_hidden} new, -{stats.unhidden} released, {stats.already_hidden} already hidden, "
          f"{stats.protected} stale but protected (saved / applied / tailored)")
    for source, count in stats.hidden_by_source.items():
        print(f"  {source:<22} {count:>6}")
    return 0


def _command_liveness_check(arguments: argparse.Namespace, config: Config) -> int:
    """Network trouble never fails the run: every answer is open / closed / uncertain."""
    from jobhunt import liveness

    connection = open_database(config)
    fetcher = liveness.make_fetcher(config)
    try:
        stats = liveness.run_shortlist_liveness(connection, config, fetcher)
    except Exception as error:  # noqa: BLE001 - this step must never fail the nightly run
        logger.warning("liveness check skipped: %s", error)
        return 0
    finally:
        fetcher.close()
        connection.close()
    print(f"liveness: candidates={stats.candidates} checked={stats.checked} cached={stats.cached} open={stats.open} "
          f"closed={stats.closed} uncertain={stats.uncertain} requests={stats.requests}"
          f"{' (request cap reached)' if stats.capped else ''}")
    return 0


def _find_available_port(requested_port: int, host: str = "127.0.0.1", max_tries: int = 100) -> int:
    """Return requested_port if free; otherwise find the next available port.

    If requested_port <= 0, binds to port 0 to let the OS assign any free ephemeral port.
    """
    import socket

    if requested_port <= 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return s.getsockname()[1]

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, requested_port))
            return requested_port
        except OSError:
            pass

    for port in range(requested_port + 1, requested_port + 1 + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _command_serve(arguments: argparse.Namespace, config: Config) -> int:
    try:
        import uvicorn

        from jobhunt.api.app import create_app
    except ImportError as error:
        logger.error('web dependencies are not installed (%s): pip install -e ".[api]"', error.name)
        return 2

    allow_remote_arg = getattr(arguments, "allow_remote", None)
    explicit_host = getattr(arguments, "host", None)

    if explicit_host and explicit_host not in ("127.0.0.1", "localhost") and not allow_remote_arg:
        logger.error("Refusing to bind to non-local interface '%s' without --allow-remote. This app accepts secrets and has no user login.", explicit_host)
        return 2

    if allow_remote_arg:
        host = allow_remote_arg if isinstance(allow_remote_arg, str) else "0.0.0.0"
        logger.warning("WARNING: Remote access is enabled on '%s'. Secrets and settings may be exposed over HTTP.", host)
    elif explicit_host:
        host = explicit_host
    else:
        host = "127.0.0.1"

    extra_hosts = (host,) if host not in ("127.0.0.1", "localhost") else ()
    app = create_app(config, extra_allowed_hosts=extra_hosts)
    dist = config.project_root / "web" / "dist"
    if not (dist / "index.html").is_file():
        logger.warning("web/dist is not built: only the API is served. Run `npm install && npm run build` in web/.")
    requested_port = arguments.port
    port = _find_available_port(requested_port, host=host if host in ("127.0.0.1", "localhost") else "127.0.0.1")
    if requested_port > 0 and port != requested_port:
        print(f"Port {requested_port} is already in use; listening on port {port} instead.")
    if host in ("127.0.0.1", "localhost"):
        print(f"jobhunt is running at http://127.0.0.1:{port}  (local only; Ctrl+C to stop)")
    else:
        print(f"jobhunt is running at http://{host}:{port}  (REMOTE ACCESS ENABLED; Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def _command_triage_train(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.ollama import OllamaClient
    from jobhunt.matching.triage_apply import protected_ids
    from jobhunt.matching.triage_features import extract_features_for_jobs
    from jobhunt.matching.triage_labels import build_unified_labels
    from jobhunt.matching.triage_train import (
        read_meta,
        save_models_and_meta,
        train_and_eval_head,
    )
    from jobhunt.timeutil import to_iso, utc_now

    connection = open_database(config)
    labels_by_head = build_unified_labels(connection, config)
    all_job_ids = list(dict.fromkeys(u.job_id for labels in labels_by_head.values() for u in labels))

    client = OllamaClient()
    now_iso = to_iso(utc_now())
    print(f"Extracting features for {len(all_job_ids)} labelled jobs...")
    dataset = extract_features_for_jobs(connection, all_job_ids, client=client, now_iso=now_iso)
    prot_ids = protected_ids(connection, config)

    new_models = {}
    eval_results = {}
    print("\n--- Training Triage Cascade Heads ---")
    for head in ("seniority", "location", "eligibility"):
        labels = labels_by_head.get(head, [])
        model, res = train_and_eval_head(connection, config, head, dataset, labels, prot_ids)
        new_models[head] = model
        eval_results[head] = res
        print(f"\n[{head.upper()}] (samples={res.n_samples}, pos={res.n_positives})")
        print(f"  Model: {res.chosen_model}")
        print(f"  Grouped CV AUC: {res.grouped_cv_auc:.3f} | Precision: {res.grouped_cv_precision:.3f} | Recall: {res.grouped_cv_recall:.3f} | ECE: {res.grouped_cv_ece:.3f}")
        print(f"  Time Split AUC: {res.time_split_auc:.3f} | Precision: {res.time_split_precision:.3f} | Recall: {res.time_split_recall:.3f}")
        print(f"  Thresholds: act p>={res.act_threshold:.2f} (precision {res.precision_at_act*100:.1f}%, coverage {res.coverage_at_act*100:.1f}%) | abstain p<={res.abstain_low:.2f}")
        print(f"  Mode: [{res.mode.upper()}] - {res.reason}")

    save_models_and_meta(config, new_models, eval_results)
    connection.close()
    print("\nModels and metadata saved to data/models/")
    return 0


def _command_triage_eval(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.ollama import OllamaClient
    from jobhunt.matching.triage_apply import protected_ids
    from jobhunt.matching.triage_features import extract_features_for_jobs
    from jobhunt.matching.triage_labels import build_unified_labels
    from jobhunt.matching.triage_train import (
        export_eval_markdown,
        train_and_eval_head,
    )
    from jobhunt.timeutil import to_iso, utc_now

    connection = open_database(config)
    labels_by_head = build_unified_labels(connection, config)
    all_job_ids = list(dict.fromkeys(u.job_id for labels in labels_by_head.values() for u in labels))

    client = OllamaClient()
    now_iso = to_iso(utc_now())
    dataset = extract_features_for_jobs(connection, all_job_ids, client=client, now_iso=now_iso)
    prot_ids = protected_ids(connection, config)

    eval_results = {}
    for head in ("seniority", "location", "eligibility"):
        labels = labels_by_head.get(head, [])
        _, res = train_and_eval_head(connection, config, head, dataset, labels, prot_ids)
        eval_results[head] = res

    out_path = export_eval_markdown(eval_results, config, now_iso=now_iso)
    connection.close()
    print(f"Triage evaluation written to {out_path}")
    return 0


def _command_triage_run(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.ollama import OllamaClient
    from jobhunt.matching.triage_cascade import CascadeRouter

    dry_run = not arguments.apply
    limit = arguments.limit
    connection = open_database(config)
    rows = connection.execute(
        """SELECT j.id FROM jobs j JOIN job_relevance r ON r.job_id = j.id
           WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
           AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
           ORDER BY COALESCE(r.final_score, r.local_rank_score, 50.0) DESC
           LIMIT ?""",
        (limit,)
    ).fetchall()
    job_ids = [int(r["id"]) for r in rows]

    client = OllamaClient()
    router = CascadeRouter(connection, config, client=client)
    res = router.route_jobs(job_ids, dry_run=dry_run)

    print(f"\n--- Cascade Triage {'(DRY RUN)' if dry_run else '(APPLIED)'} ---")
    print(f"Examined: {res.examined} candidate jobs")
    print("Tier counts: " + ", ".join(f"{k}={v}" for k, v in res.tier_counts.items()))
    print(f"Demotions: active={res.active_demotions}, shadow={res.shadow_demotions}")
    print("Head demotions: " + ", ".join(f"{k}={v}" for k, v in res.head_demotions.items()))
    print(f"Gemini calls made: {res.gemini_calls} | Saved vs all-Gemini baseline: {res.gemini_calls_saved}")
    if dry_run:
        print("\nPass --apply to persist decisions to database.")
    connection.close()
    return 0


def _command_triage_shadow_report(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.triage_cascade import run_shadow_report

    connection = open_database(config)
    report = run_shadow_report(connection, config)
    connection.close()

    print("\n--- Triage Shadow Report ---")
    print(f"Total shadow demotions recorded: {report['total_shadow_demotions']}")
    print(f"Agreed with subsequent rejections: {report['agreed_human_rejections']}")
    print(f"Disagreed with saved/applied jobs: {report['disagreed_applied_saved']}")
    print(f"Unreviewed / pending jobs: {report['unreviewed']}")

    if report["items"]:
        print("\nRecent shadow demotions:")
        for it in report["items"][:20]:
            print(f"  [Job {it['job_id']}] {it['title'][:30]} @ {it['company'][:20]} | {it['head']} p={it['prob']:.2f} | agreement: {it['agreement']} ({it['human_action']})")
    return 0


def _command_triage_llm_run(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.ollama import OllamaClient
    from jobhunt.matching.triage_queue import run_triage_llm_pipeline
    from jobhunt.timeutil import to_iso, utc_now

    connection = open_database(config)
    ollama_client = OllamaClient()
    gemini_client = None
    try:
        gemini_client = _gemini_client(config)
    except Exception:
        gemini_client = None

    dry_run = not arguments.apply
    max_minutes = arguments.max_minutes
    limit = arguments.limit

    print(f"\n--- Local-LLM Triage Pipeline {'(DRY RUN)' if dry_run else '(APPLYING)'} ---")
    start_time = utc_now()
    res = run_triage_llm_pipeline(
        connection,
        config,
        max_minutes=max_minutes,
        dry_run=dry_run,
        limit=limit,
        client=ollama_client,
        gemini_client=gemini_client,
    )
    import os
    if os.environ.get("JOBHUNT_RUNNER_TASK") != "triage":
        status_str = "skipped" if res.get("status") == "skipped" else "ok"
        dur = round((utc_now() - start_time).total_seconds(), 1)
        detail_msg = f"processed {res.get('newly_judged', 0)} jobs; cached {res.get('cached', 0)}; queue {res.get('queue_total', 0)}"
        try:
            connection.execute(
                """INSERT INTO scheduled_runs (task, trigger, started_at, finished_at, status, duration_seconds, detail)
                   VALUES ('triage', 'manual', ?, ?, ?, ?, ?)""",
                (to_iso(start_time), to_iso(utc_now()), status_str, dur, detail_msg)
            )
            connection.commit()
        except sqlite3.OperationalError:
            pass
    connection.close()

    if res.get("status") == "skipped":
        print(f"Skipped: {res.get('reason')}")
        return 0

    cov = res.get("coverage", {})
    app = res.get("application", {})
    print(f"Duration: {res.get('duration_seconds')}s | Queue total: {res.get('queue_total')}")
    print(f"Judged newly: {res.get('newly_judged')} | Cached: {res.get('cached')} | Failed: {res.get('failed')}")
    print(f"Coverage: judged {cov.get('judged_relevant', 0)} of {cov.get('total_relevant', 0)} visible Relevant jobs ({cov.get('relevant_coverage_pct', 0)}%)")
    print(f"Application: excluded_seniority={app.get('excluded_seniority')}, caution_seniority={app.get('caution_seniority')}, "
          f"hidden_location={app.get('hidden_location')}, blockers_applied={app.get('blockers_applied')}, "
          f"advisory_only={app.get('advisory_only')}, exempt_protected={app.get('exempt_protected_count')}")
    if app.get("blast_radius_triggered"):
        print(f"BLAST RADIUS GUARD TRIGGERED: {app.get('blast_radius_detail')}")
    if app.get("high_stakes_gemini_called"):
        print(f"High-stakes Gemini checks: {app.get('high_stakes_gemini_called')} (disagreements: {app.get('high_stakes_disagreements')})")
    if dry_run:
        print("\nPass --apply to persist triage decisions to database.")
    return 0


def _command_triage_llm_benchmark(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.ollama import OllamaClient
    from jobhunt.matching.triage_benchmark import run_local_llm_benchmark

    connection = open_database(config)
    client = OllamaClient()
    max_minutes = arguments.max_minutes if arguments.max_minutes is not None else float(config.triage.local_llm_max_minutes)
    limit = arguments.limit

    print("\n--- Running Local LLM Benchmark ---")
    results = run_local_llm_benchmark(
        connection,
        config,
        client=client,
        max_minutes=max_minutes,
        limit=limit,
    )
    connection.close()

    print(f"Evaluated jobs: {results.total_evaluated} (median {results.median_seconds_per_job:.2f}s/job)")
    print(f"Quotes: verified={results.verified_quote_rate*100:.1f}%, hallucinated={results.hallucinated_quote_rate*100:.1f}%")
    print(f"Gemini agreement: level={results.overall_agreement_gemini.get('level_agreement', 0)*100:.1f}%, location={results.overall_agreement_gemini.get('india_agreement', 0)*100:.1f}%, years_ge2={results.overall_agreement_gemini.get('years_ge2_agreement', 0)*100:.1f}%")
    print(f"Human agreement: seniority={results.agreement_human.get('seniority_agreement', 0)*100:.1f}%, location={results.agreement_human.get('location_agreement', 0)*100:.1f}%")
    print("\nActing Rules Pass/Fail:")
    for rule in results.acting_rules:
        status_str = "PASS (ACTS)" if rule.can_act else "FAIL (ADVISORY)"
        print(f"  [{status_str}] {rule.field}.{rule.target_class}: precision={rule.precision_val*100:.1f}% (high_conf N={rule.high_conf_count}, total N={rule.sample_count})")
    return 0


def _command_triage_acting_rules(arguments: argparse.Namespace, config: Config) -> int:
    connection = open_database(config)
    rows = connection.execute(
        """SELECT field, target_class, can_act, mode, sample_count, high_conf_count,
                  ROUND(precision_val * 100, 1) as prec_pct,
                  ROUND(recall_val * 100, 1) as rec_pct,
                  reason, evaluated_at
           FROM job_triage_acting_rules
           ORDER BY field ASC, target_class ASC"""
    ).fetchall()
    connection.close()

    if not rows:
        print("No acting rules found in database. Run 'triage-llm-benchmark' first.")
        return 0

    print(f"\n{'Field':<15} {'Class':<18} {'Mode':<10} {'N (Total)':<10} {'High-Conf':<10} {'Prec %':<8} {'Rec %':<8} Reason")
    print("-" * 100)
    for r in rows:
        st = "CAN ACT" if r["can_act"] else "ADVISORY"
        print(f"{r['field']:<15} {r['target_class']:<18} {st:<10} {r['sample_count']:<10} {r['high_conf_count']:<10} {r['prec_pct'] or 0.0:<8} {r['rec_pct'] or 0.0:<8} {r['reason']}")
    return 0


def _command_triage_import_audit(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.matching.triage_audit import validate_and_import_audit_csv

    connection = open_database(config)
    try:
        res = validate_and_import_audit_csv(connection, config, arguments.csv_file)
    finally:
        connection.close()

    if res.errors:
        print("\nErrors encountered:")
        for err in res.errors[:10]:
            print(f"  ! {err}")

    print(f"\n--- Audit Import Summary ---")
    print(f"Total rows in CSV:    {res.total_rows}")
    print(f"Answered rows:        {res.answered_rows}")
    print(f"Imported labels:      {res.imported_labels}")
    print(f"Skipped unsure:       {res.skipped_unsure}")
    print(f"Empty rows (pending): {res.empty_rows}")
    print(f"Invalid rows:         {res.invalid_rows}")

    if res.answered_rows > 0:
        decisive = res.gemini_correct + res.local_correct
        print(f"\nModel Accuracy against your answers ({decisive} decisive):")
        print(f"  Gemini accuracy:    {res.gemini_accuracy:.1%} ({res.gemini_correct} correct)")
        print(f"  Local LLM accuracy: {res.local_accuracy:.1%} ({res.local_correct} correct)")

    if res.acting_rules:
        print(f"\nUpdated Acting Rules (Wilson 95% intervals):")
        print(f"{'Field':<15} {'Class':<18} {'Mode':<10} {'N':<6} {'Prec':<8} {'95% CI':<18} Rule Desc")
        print("-" * 90)
        for r in res.acting_rules:
            ci_str = f"[{r.ci_lower:.1%}, {r.ci_upper:.1%}]"
            print(f"{r.field:<15} {r.target_class:<18} {r.mode:<10} {r.sample_count:<6} {r.precision_val:.1%}   {ci_str:<18} {r.rule_desc}")

    return 0 if res.invalid_rows == 0 else 1


def _command_golden_snapshot(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.db import connect
    from jobhunt.matching.golden import capture_golden_snapshot

    connection = connect(config.database_file)
    print(f"Capturing golden snapshot into {arguments.out}...")
    stats = capture_golden_snapshot(connection, config, arguments.out)
    print(f"Golden snapshot captured successfully: {stats}")
    return 0


def _command_golden_compare(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.db import connect
    from jobhunt.matching.golden import compare_golden_snapshots

    connection = connect(config.database_file)
    print(f"Comparing golden snapshots: {arguments.before} vs {arguments.after}...")
    passed, report, diffs = compare_golden_snapshots(arguments.before, arguments.after, connection)
    print(report)
    return 0 if passed else 1


def _command_profile_import_legacy(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.profile import profile_import_legacy

    path = profile_import_legacy(config)
    print(f"Legacy profile imported successfully to {path}")
    return 0


def _command_doctor(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.doctor import SystemDoctor, generate_doctor_report

    online = getattr(arguments, "online", False)
    doc = SystemDoctor(config, online=online)
    checks = doc.run_all_checks()
    print("=" * 65)
    print("jobhunt system doctor")
    print("=" * 65)
    failed = 0
    warned = 0
    for c in checks:
        symbol = "[OK]  " if c.status == "pass" else ("[WARN]" if c.status == "warn" else "[FAIL]")
        print(f"{symbol} {c.name:<30} {c.message}")
        if c.hint:
            print(f"       -> Hint: {c.hint}")
        if c.status == "fail":
            failed += 1
        elif c.status == "warn":
            warned += 1
    print("=" * 65)

    if getattr(arguments, "report", False):
        report_path = generate_doctor_report(config, online=online)
        print(f"\n[REPORT] Diagnostic report written to: {report_path.name}")
        print("         The report is scrubbed of all secrets, emails, phone numbers, and candidate PII.")
        print("         It is safe to attach to GitHub issues or send to maintainers.")

    if failed:
        print(f"Result: {failed} check(s) failed. Please resolve the issues above.")
        return 1
    if warned:
        print(f"Result: All required checks passed with {warned} warning(s). Ready to hunt!")
        return 0
    print("Result: All checks passed. System is in peak condition!")
    return 0


def _command_feedback_template(arguments: argparse.Namespace, config: Config) -> int:
    template_text = """=== Jobhunt Tester Feedback Form ===
Thank you for trying out Jobhunt! Please copy this questionnaire and fill in your observations:

1. Environment & Setup:
   - Operating System & Version (e.g. macOS Sonoma Apple Silicon, Windows 11 x64):
   - Did `setup.ps1` / `setup.sh` complete on the first attempt without manual intervention? (Yes/No)
   - If not, what error occurred and what command fixed it?

2. Setup Wizard Experience:
   - Did the setup wizard open in your browser automatically?
   - How clear were the questions (Gemini key, Budget presets, Resume templates)?
   - Did you select pure offline mode ($0) or provide a Gemini key?

3. Resume & LaTeX Compilation:
   - What template style did you upload (Custom macros, Jake's Resume, Awesome-CV, Moderncv, Plain itemize, or other)?
   - Did Tectonic compile your resume into a 1-page PDF successfully?
   - Any layout issues, font errors, or missing sections?

4. Job Ingestion & Discovery:
   - Did the first ingest run cleanly in the background?
   - How many jobs were found / ingested?

5. Relevance Gating & Location:
   - Did the location filter correctly identify your home country / city?
   - Were seniority / student levels correctly screened?

6. Match Scoring & Ranking:
   - Did you use Ollama, Gemini embeddings, or Pure Offline ($0) mode?
   - How relevant were the top-ranked jobs to your target roles?

7. Tailoring & Letters:
   - Did you generate a tailored resume or cover letter?
   - How was the quality of bullets and language?

8. Schedule & Automation:
   - Which scheduler backend did you test (in_app, windows_task_scheduler, macos_launchd)?
   - Did scheduled runs trigger at the expected times?

9. What was confusing, unexpected, or missing?
   - 

10. Overall Impression & Rating (1 to 10):
   - Rating:
   - What would make this a daily essential tool for your job search?

Attach `doctor_report.txt` (generated via `jobhunt doctor --report`) if reporting an issue!
"""
    print(template_text)
    return 0


def _command_upgrade(arguments: argparse.Namespace, config: Config) -> int:
    """Run upgrade workflow:
    1. Print git pull notice (never runs git pull automatically)
    2. Back up database
    3. Run migrations
    4. Check and install dependencies if requirements.lock changed
    5. Check and rebuild frontend if web/ changed
    """
    print("=== Jobhunt Upgrade ===")
    print("Notice: To fetch the latest repo changes first, run: git pull\n")

    data_dir = config.data_directory
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. Back up database if it exists and has content
    db_file = config.database_file
    if db_file.is_file() and db_file.stat().st_size > 0:
        backups_dir = data_dir / "backups"
        print(f"Backing up database to {backups_dir} ...")
        backup_path = backup_database(db_file, backups_dir, label="upgrade")
        print(f"  Database backed up to: {backup_path.name}")
    else:
        print("No existing database to back up.")

    # 2. Run migrations
    print("\nRunning database migrations...")
    connection = connect(config.database_file)
    try:
        applied = migrate_with_backup(connection, config.database_file, config.migrations_directory, data_dir / "backups")
        if applied:
            print(f"  Applied migrations: {applied}")
        else:
            print("  Database schema is up to date.")
    finally:
        connection.close()

    # 3. Check and update dependencies if lock changed
    lock_file = config.project_root / "requirements.lock"
    state_file = data_dir / ".installed_lock_hash"
    if lock_file.is_file():
        current_hash = hashlib.sha256(lock_file.read_bytes()).hexdigest()
        prev_hash = state_file.read_text(encoding="utf-8").strip() if state_file.is_file() else None
        if current_hash != prev_hash:
            print("\nUpdating Python dependencies from requirements.lock ...")
            py_exe = get_python_executable(config.project_root)
            res = subprocess.run([str(py_exe), "-m", "pip", "install", "-r", str(lock_file), "--quiet"], check=False)
            if res.returncode == 0:
                state_file.write_text(current_hash, encoding="utf-8")
                print("  Dependencies updated successfully.")
            else:
                print("  Warning: pip install returned non-zero code.", file=sys.stderr)
        else:
            print("\nPython dependencies are already up to date.")

    # 4. Check and rebuild frontend if web/ changed
    web_dir = config.project_root / "web"
    if web_dir.is_dir() and shutil.which("npm"):
        web_dist = web_dir / "dist"
        web_state = data_dir / ".web_build_hash"
        # Compute quick hash of package.json and src files
        h = hashlib.sha256()
        pkg_json = web_dir / "package.json"
        if pkg_json.is_file():
            h.update(pkg_json.read_bytes())
        for src_file in sorted((web_dir / "src").glob("**/*")):
            if src_file.is_file():
                h.update(src_file.read_bytes())
        web_hash = h.hexdigest()
        prev_web_hash = web_state.read_text(encoding="utf-8").strip() if web_state.is_file() else None

        if not web_dist.is_dir() or web_hash != prev_web_hash:
            print("\nRebuilding frontend web assets ...")
            run_cmd = "npm.cmd" if os.name == "nt" else "npm"
            res = subprocess.run([run_cmd, "run", "build"], cwd=web_dir, capture_output=True, text=True, check=False)
            if res.returncode == 0:
                web_state.write_text(web_hash, encoding="utf-8")
                print("  Frontend built successfully.")
            else:
                print(f"  Warning: Frontend build failed:\n{res.stderr}", file=sys.stderr)
        else:
            print("\nFrontend assets are already up to date.")

    print("\n=== Upgrade Complete ===")
    print("Start the app with: jobhunt serve")
    return 0


def _command_morning_check(arguments: argparse.Namespace, config: Config) -> int:
    from jobhunt.schedule.morning_check import run_morning_check

    connection = open_database(config)
    try:
        code, text = run_morning_check(connection, config)
        print(text)
        return code
    finally:
        connection.close()


_COMMANDS = {
    "init-db": _command_init_db,
    "ingest": _command_ingest,
    "discover": _command_discover,
    "stats": _command_stats,
    "schedule-install": _command_schedule_install,
    "schedule-remove": _command_schedule_remove,
    "schedule-status": _command_schedule_status,
    "morning-check": _command_morning_check,
    "scheduled-run": _command_scheduled_run,
    "gmail-auth": _command_gmail_auth,
    "ingest-email": _command_ingest_email,
    "parse-email": _command_parse_email,
    "serve": _command_serve,
    "backfill-remote": _command_backfill_remote,
    "detect-ats": _command_detect_ats,
    "train-ranker": _command_train_ranker,
    "rank": _command_rank,
    "retrain-ranker": _command_retrain_ranker,
    "tailor": _command_tailor,
    "tailor-top": _command_tailor_top,
    "local-llm": _command_local_llm,
    "relevance-run": _command_relevance_run,
    "freshness-run": _command_freshness_run,
    "liveness-check": _command_liveness_check,
    "profile-build": _command_profile_build,
    "embed": _command_embed,
    "score": _command_score,
    "triage": _command_triage,
    "triage-student": _command_triage_student,
    "triage-backfill": _command_triage_backfill,
    "triage-train": _command_triage_train,
    "triage-eval": _command_triage_eval,
    "triage-run": _command_triage_run,
    "triage-shadow-report": _command_triage_shadow_report,
    "triage-llm-run": _command_triage_llm_run,
    "triage-llm-benchmark": _command_triage_llm_benchmark,
    "triage-acting-rules": _command_triage_acting_rules,
    "triage-import-audit": _command_triage_import_audit,
    "usage": _command_usage,
    "suggest-rules": _command_suggest_rules,
    "match": _command_match,
    "eval-sample": _command_eval_sample,
    "eval-run": _command_eval_run,
    "golden-snapshot": _command_golden_snapshot,
    "golden-compare": _command_golden_compare,
    "profile-import-legacy": _command_profile_import_legacy,
    "doctor": _command_doctor,
    "feedback-template": _command_feedback_template,
    "upgrade": _command_upgrade,
}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
