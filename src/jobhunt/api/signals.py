"""Posting signals: facts about a listing computed from data jobhunt already stores (no model, no network).

A signal is `{id, level, text}` with level `info` or `caution`. Signals never change `final_score`, the gate or
any ordering; the wording states facts ("listed 83 days ago on Greenhouse"), it never claims a posting is fake.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from jobhunt.timeutil import parse_iso

REPOST_MIN_SPAN_DAYS = 14
AGE_CAUTION_DAYS = 60
AGE_MIXED_DAYS = 30

# Sources whose sighting means the employer's own ATS or career site listed the job.
EMPLOYER_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "workday", "oracle", "amazon_jobs",
    "phenom", "eightfold", "icims",
})
# Aggregators, curated lists and alert emails: someone else reported the posting.
AGGREGATOR_SOURCES = frozenset({
    "simplify_internships", "simplify_newgrad", "arbeitnow", "hackernews", "himalayas", "jobicy", "remoteok",
    "weworkremotely", "remotive", "adzuna",
})
_DISPLAY = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workable": "Workable",
    "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee", "workday": "Workday", "oracle": "Oracle Recruiting",
    "amazon_jobs": "Amazon Jobs", "phenom": "Phenom", "eightfold": "Eightfold", "icims": "iCIMS",
    "simplify_internships": "the Simplify internships list", "simplify_newgrad": "the Simplify new-grad list",
    "arbeitnow": "Arbeitnow", "hackernews": "Hacker News", "himalayas": "Himalayas", "jobicy": "Jobicy",
    "remoteok": "RemoteOK", "weworkremotely": "We Work Remotely", "remotive": "Remotive", "adzuna": "Adzuna",
    "email_linkedin": "a LinkedIn alert email", "email_indeed": "an Indeed alert email", "manual": "a manual entry",
}

# Source -> payload keys that hold the real publication date; when none is present, `posted_at` is a fallback.
_REAL_DATE_KEYS = {
    "greenhouse": ("first_published",),
    "recruitee": ("published_at",),
    "workable": ("published_on",),
}

URL_SHORTENERS = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly", "lnkd.in",
    "shorturl.at", "tiny.cc", "rb.gy", "s.id", "t.ly", "bl.ink", "short.io",
})
FREE_MAIL_HOSTS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "outlook.com", "hotmail.com", "live.com", "aol.com",
    "proton.me", "protonmail.com", "icloud.com", "rediffmail.com", "mail.com", "gmx.com", "yandex.com",
})
# Job boards and ATS hosts are a normal place for an apply link even though the host is not the company's.
NEUTRAL_HOSTS = frozenset({
    "linkedin.com", "indeed.com", "glassdoor.com", "naukri.com", "monster.com", "ziprecruiter.com", "wellfound.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "myworkdayjobs.com",
    "icims.com", "jobvite.com", "taleo.net", "oraclecloud.com", "bamboohr.com", "recruitee.com", "breezy.hr",
    "simplify.jobs", "eightfold.ai", "phenompeople.com", "successfactors.com", "successfactors.eu",
})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "plc", "pvt", "private",
                     "technologies", "technology", "solutions", "group", "the"}


def _registrable(host: str) -> str:
    parts = [p for p in host.lower().strip(".").split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _in_set(host: str, names: frozenset[str]) -> bool:
    return _registrable(host) in names or host in names


def _company_tokens(company_name: str) -> list[str]:
    words = [w for w in _NON_ALNUM.split(company_name.lower()) if w and w not in _COMPANY_SUFFIXES]
    tokens = [w for w in words if len(w) >= 3]
    if len(words) > 1:
        tokens.append("".join(words))
    return tokens


def apply_url_shape(url: str | None, company_name: str) -> tuple[str, str] | None:
    """`(kind, text)` when an apply URL host is a shortener, a free-mail host or unrelated to the company."""
    host = _host(url)
    if not host:
        return None
    if _in_set(host, URL_SHORTENERS):
        return "shortener", f"apply link goes through a URL shortener ({host})"
    if _in_set(host, FREE_MAIL_HOSTS):
        return "free_mail", f"apply link points to a free-mail host ({host})"
    if _in_set(host, NEUTRAL_HOSTS):
        return None
    label = _registrable(host).split(".")[0]
    haystack = _NON_ALNUM.sub("", host)
    for token in _company_tokens(company_name):
        token = _NON_ALNUM.sub("", token)
        if len(token) >= 3 and (token in haystack or (len(label) >= 4 and label in token)):
            return None
    return "mismatch", f"apply link host ({_registrable(host)}) does not match the company name"


def display_source(source: str) -> str:
    return _DISPLAY.get(source, source.replace("_", " ").title())


def _date_from_fallback(source: str, raw_json: str | None) -> bool:
    """True when `posted_at` for this source may be a last-updated / received date, not a publication date."""
    if source.startswith("email_"):
        return True
    keys = _REAL_DATE_KEYS.get(source)
    if keys is None:
        return False
    if not raw_json:
        return True                       # payload not stored: cannot verify, say so
    try:
        data = json.loads(raw_json)
    except ValueError:
        return True
    return not (isinstance(data, dict) and any(data.get(key) for key in keys))


def _early_career(employment_type: str | None, level: str | None) -> bool:
    return employment_type == "internship" or level in {"intern", "new_grad"}


def _window_end(row: sqlite3.Row | dict[str, Any]) -> str:
    return str(row["closed_at"] or row["last_seen_at"] or row["first_seen_at"])


def _repost_signal(group: list[Any], me: int) -> dict[str, str] | None:
    if len(group) < 2 or not any(int(r["id"]) == me for r in group):
        return None
    ordered = sorted(group, key=lambda r: str(r["first_seen_at"]))
    disjoint = any(str(later["first_seen_at"]) > _window_end(earlier)
                   for earlier, later in zip(ordered, ordered[1:]))
    if not disjoint:
        return None
    span = (parse_iso(str(ordered[-1]["first_seen_at"])) - parse_iso(str(ordered[0]["first_seen_at"]))).days
    if span < REPOST_MIN_SPAN_DAYS:
        return None
    return {"id": "repost", "level": "caution",
            "text": f"same title, company and location was listed {len(ordered)} separate times over {span} days"}


def compute_signals(connection: sqlite3.Connection, job_ids: list[int], now: datetime) -> dict[int, list[dict[str, str]]]:
    """Signals for each job id (batch: a handful of queries regardless of how many ids)."""
    if not job_ids:
        return {}
    marks = ",".join("?" for _ in job_ids)
    jobs = {int(r["id"]): r for r in connection.execute(
        f"""SELECT id, fingerprint, company_name, posted_at, first_seen_at, last_seen_at, closed_at, missed_runs,
                   status, employment_type, experience_level, canonical_apply_url
            FROM jobs WHERE id IN ({marks})""", job_ids)}
    sightings: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
            f"SELECT job_id, source, source_url FROM job_sightings WHERE job_id IN ({marks}) ORDER BY id", job_ids):
        sightings[int(row["job_id"])].append(row)

    fallback_candidates = [
        job_id for job_id, rows in sightings.items()
        if any(s["source"] in _REAL_DATE_KEYS for s in rows) and jobs.get(job_id) is not None
        and jobs[job_id]["posted_at"]]
    raw: dict[int, str | None] = {}
    if fallback_candidates:
        fmarks = ",".join("?" for _ in fallback_candidates)
        raw = {int(r["id"]): r["raw_json"] for r in connection.execute(
            f"SELECT id, raw_json FROM jobs WHERE id IN ({fmarks})", fallback_candidates)}

    fingerprints = sorted({str(j["fingerprint"]) for j in jobs.values()})
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    if fingerprints:
        fmarks = ",".join("?" for _ in fingerprints)
        for row in connection.execute(
                f"""SELECT id, fingerprint, first_seen_at, last_seen_at, closed_at FROM jobs
                    WHERE fingerprint IN ({fmarks})""", fingerprints):
            groups[str(row["fingerprint"])].append(row)

    out: dict[int, list[dict[str, str]]] = {}
    for job_id in job_ids:
        job = jobs.get(job_id)
        if job is None:
            continue
        found = sightings.get(job_id, [])
        sources = [str(s["source"]) for s in found]
        employer = [s for s in sources if s in EMPLOYER_SOURCES]
        primary = employer[0] if employer else (sources[0] if sources else None)
        early = _early_career(job["employment_type"], job["experience_level"])
        signals: list[dict[str, str]] = []

        # posting age
        if not job["posted_at"]:
            signals.append({"id": "posting_age", "level": "info", "text": "posting date unknown"})
        else:
            try:
                days = max(0, (now - parse_iso(str(job["posted_at"]))).days)
            except ValueError:
                days = None
            if days is None:
                signals.append({"id": "posting_age", "level": "info", "text": "posting date unknown"})
            else:
                when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
                where = f" on {display_source(primary)}" if primary else ""
                level = "caution" if days >= AGE_CAUTION_DAYS and not early else "info"
                signals.append({"id": "posting_age", "level": level, "text": f"listed {when}{where}"})
                if any(_date_from_fallback(s, raw.get(job_id)) for s in sources):
                    signals.append({"id": "date_quality", "level": "info",
                                    "text": "date from source (may be last-updated)"})

        # employer-confirmed or aggregator only
        if employer:
            signals.append({"id": "employer_confirmed", "level": "info",
                            "text": f"listed on the employer's site ({display_source(employer[0])})"})
        elif sources and all(s in AGGREGATOR_SOURCES or s.startswith("email_") for s in sources):
            seen_on = ", ".join(dict.fromkeys(display_source(s) for s in sources))
            signals.append({"id": "aggregator_only", "level": "caution",
                            "text": f"not confirmed on the employer's site yet (seen on {seen_on})"})

        if job["status"] == "open" and int(job["missed_runs"] or 0) > 0:
            count = int(job["missed_runs"])
            signals.append({"id": "flickering", "level": "info",
                            "text": f"missed in the last {count} fetch{'es' if count != 1 else ''}"})

        repost = _repost_signal(groups.get(str(job["fingerprint"]), []), job_id)
        if repost:
            signals.append(repost)

        if any(s.startswith("email_") for s in sources):
            urls = [job["canonical_apply_url"]] + [s["source_url"] for s in found if s["source"].startswith("email_")]
            for url in urls:
                shape = apply_url_shape(url, str(job["company_name"] or ""))
                if shape:
                    signals.append({"id": f"apply_url_{shape[0]}", "level": "caution", "text": shape[1]})
                    break
        out[job_id] = signals
    return out


def has_caution(signals: list[dict[str, str]]) -> bool:
    return any(item["level"] == "caution" for item in signals)
