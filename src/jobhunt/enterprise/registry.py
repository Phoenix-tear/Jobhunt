"""enterprise_companies.yaml <-> the `enterprise_companies` table, plus platform detection."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from jobhunt.config import Config
from jobhunt.discovery.patterns import extract_ats_reference
from jobhunt.timeutil import to_iso, utc_now

ENTERPRISE_FILE = "enterprise_companies.yaml"

_WORKDAY_HOST = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)
_LOCALE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")
_ORACLE_HOST = re.compile(r"^[a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com$", re.IGNORECASE)

# (platform, substring that proves it in a URL host/path or in the page text)
_MARKERS: tuple[tuple[str, str], ...] = (
    ("workday", "myworkdayjobs.com"),
    ("workday", "myworkdaysite.com"),
    ("oracle", "oraclecloud.com/hcmui/candidateexperience"),
    ("oracle", ".fa.") ,
    ("eightfold", "eightfold.ai"),
    ("successfactors", "successfactors.com"),
    ("successfactors", "successfactors.eu"),
    ("successfactors", "jobs.sap.com"),
    ("icims", "icims.com"),
    ("taleo", "taleo.net"),
    ("phenom", "phenompeople.com"),
    ("phenom", "phenom.com"),
    ("avature", "avature.net"),
    ("smartrecruiters", "smartrecruiters.com"),
    ("greenhouse", "greenhouse.io"),
    ("lever", "lever.co"),
    ("ashby", "ashbyhq.com"),
    ("amazon_jobs", "amazon.jobs"),
)


@dataclass(frozen=True)
class Detection:
    platform: str                # workday | oracle | ... | unknown
    params: dict[str, Any]
    evidence: str


def parse_workday_url(url: str) -> dict[str, str] | None:
    """tenant / dc / site / host / locale from any public myworkdayjobs.com URL, or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    match = _WORKDAY_HOST.match(host)
    if not match:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    locale = ""
    if segments and _LOCALE.match(segments[0]):
        locale = segments.pop(0)
    if not segments or segments[0].lower() in {"wday", "job", "login", "userhome"}:
        return None
    return {"tenant": match["tenant"], "dc": match["dc"], "host": host, "site": segments[0], "locale": locale or "en-US"}


def detect_from_urls(urls: list[str], page_text: str = "") -> Detection:
    """First conclusive platform among the redirect chain / final URL, then links found in the page."""
    for url in urls:
        workday = parse_workday_url(url)
        if workday:
            return Detection("workday", workday, f"URL {url}")
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if _ORACLE_HOST.match(host) or "hcmui/candidateexperience" in url.lower():
            return Detection("oracle", {"host": host}, f"URL {url}")
    lowered = page_text.lower()
    for match in re.finditer(r"https?://[a-z0-9.-]+\.myworkdayjobs\.com/[^\s\"'<>)]*", page_text, re.IGNORECASE):
        workday = parse_workday_url(match.group(0))
        if workday:
            return Detection("workday", workday, f"link in page: {match.group(0)[:160]}")
    for match in re.finditer(r"https?://[^\s\"'<>)]+", page_text):
        reference = extract_ats_reference(match.group(0))
        if reference is not None:
            return Detection(str(reference.ats_type.value), {"slug": reference.slug, "region": reference.region},
                             f"link in page: {match.group(0)[:160]}")
    for platform, marker in _MARKERS:
        if marker == ".fa.":
            continue
        for url in urls:
            if marker in url.lower():
                return Detection(platform, {}, f"URL {url}")
        if marker in lowered:
            return Detection(platform, {}, f"marker '{marker}' in page")
    if re.search(r"[a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com", lowered):
        return Detection("oracle", {}, "oraclecloud.com candidate-experience host in page")
    return Detection("unknown", {}, "no known platform host or marker found")


# --- yaml <-> table ----------------------------------------------------------------------------


def yaml_path(config: Config) -> Path:
    return config.project_root / ENTERPRISE_FILE


def load_yaml_companies(config: Config) -> list[dict[str, Any]]:
    path = yaml_path(config)
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("companies", []) if isinstance(data, dict) else []
    cleaned: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") and entry.get("careers_url"):
            cleaned.append(entry)
    return cleaned


def sync_from_yaml(connection: sqlite3.Connection, config: Config) -> int:
    """Insert companies from the yaml that are not in the table yet; never overwrites detection results."""
    now = to_iso(utc_now())
    added = 0
    for entry in load_yaml_companies(config):
        before = connection.total_changes
        connection.execute(
            "INSERT OR IGNORE INTO enterprise_companies (name, careers_url, created_at) VALUES (?, ?, ?)",
            (str(entry["name"]).strip(), str(entry["careers_url"]).strip(), now),
        )
        added += connection.total_changes - before
        override = entry.get("adapter") or entry.get("params")
        if isinstance(entry.get("adapter"), str) and isinstance(entry.get("params"), dict):
            connection.execute(
                """UPDATE enterprise_companies SET detected_ats = ?, adapter_params = ?, status = 'detected',
                          evidence = 'set by hand in enterprise_companies.yaml' WHERE name = ?""",
                (entry["adapter"], json.dumps(entry["params"]), str(entry["name"]).strip()),
            )
        elif override and not isinstance(override, str):
            continue
    connection.commit()
    return added


def detected_companies(connection: sqlite3.Connection, platform: str) -> list[dict[str, Any]]:
    """Companies detected on `platform` with usable adapter parameters."""
    try:
        rows = connection.execute(
            "SELECT name, adapter_params FROM enterprise_companies WHERE detected_ats = ? AND status = 'detected'",
            (platform,),
        ).fetchall()
    except sqlite3.OperationalError:    # migration 007 not applied yet
        return []
    companies = []
    for row in rows:
        try:
            params = json.loads(row["adapter_params"] or "{}")
        except ValueError:
            continue
        if isinstance(params, dict) and params:
            companies.append({"name": row["name"], "params": params})
    return companies
