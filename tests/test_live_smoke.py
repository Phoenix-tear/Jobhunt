"""Optional smoke test against REAL public endpoints. Skipped unless run with --live-smoke.

    pytest --live-smoke tests/test_live_smoke.py -v

It fetches one well-known public board per ATS, checks the real shapes still parse, and (when
JOBHUNT_SAVE_FIXTURES=1) saves a trimmed normalized sample under tests/fixtures/live/.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from jobhunt.config import Config
from jobhunt.http import HttpClient
from jobhunt.models import CompanyRef
from jobhunt.sources import ATS_SOURCE_CLASSES
from jobhunt.sources.base import FetchContext

pytestmark = pytest.mark.live

KNOWN_BOARDS = {
    "greenhouse": "stripe",
    "lever": "leverdemo",
    "ashby": "ashby",
    "workable": "huggingface",
    "smartrecruiters": "BoschGroup",
    "recruitee": "bunq",
}
LIVE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "live"


@pytest.mark.parametrize("source_class", ATS_SOURCE_CLASSES, ids=lambda cls: cls.name)
def test_real_board_parses(source_class, config: Config) -> None:
    slug = KNOWN_BOARDS[source_class.name]
    company = CompanyRef(id=1, name=slug, ats_type=source_class.name, slug=slug, status="active")
    with HttpClient(config.http, config.user_agent) as http:
        result = source_class().fetch_company(company, http, FetchContext(wants_detail=lambda job: False))

    assert result.jobs, f"{source_class.name}/{slug} returned no jobs; the API shape may have changed"
    for job in result.jobs[:20]:
        assert job.source == source_class.name and job.source_job_id and job.title
        assert job.apply_url and job.apply_url.startswith("http")
        assert job.posted_at is None or job.posted_at.endswith("Z")

    if os.environ.get("JOBHUNT_SAVE_FIXTURES") == "1":
        LIVE_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        sample = [
            {**dataclasses.asdict(job), "description_text": (job.description_text or "")[:500], "raw": None}
            for job in result.jobs[:3]
        ]
        (LIVE_FIXTURE_DIR / f"{source_class.name}.json").write_text(
            json.dumps(sample, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
