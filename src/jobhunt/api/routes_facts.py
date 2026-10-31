"""Facts review and editing API (Brief v12).

Provides endpoints to review, tick/untick, edit in-place, and bulk-toggle facts
extracted from resumes and stored in `data/profile/facts_tex.md`.
The file remains the persistent storage format.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jobhunt.api.database import ConfigDep
from jobhunt.tailoring.facts import fact_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/facts", tags=["facts"])

_FACT_LINE = re.compile(
    r"^- \[(?P<box>[ xX])\] \*\*(?P<id>T-[0-9a-f]{8})\*\* `(?P<kind>[^`]+)` (?P<text>.*?)"
    r"(?:  _\[(?P<sources>[^\]]*)\]_)?(?:\s*\((?P<edited>edited by you)\))?$"
)


class FactItem(BaseModel):
    id: str
    kind: str
    text: str
    sources: list[str]
    ticked: bool
    edited: bool = False


class TickRequest(BaseModel):
    fact_ids: list[str]
    ticked: bool


class EditFactRequest(BaseModel):
    fact_id: str
    text: str


class BulkTickRequest(BaseModel):
    ticked: bool
    kind: str | None = None


def get_facts_file(config: ConfigDep) -> Path:
    profile_dir = config.resolve_path(config.profiles.profile_dir)
    return profile_dir / "facts_tex.md"


def parse_facts_lines(content: str) -> list[dict[str, Any]]:
    facts = []
    for line in content.splitlines():
        m = _FACT_LINE.match(line.strip())
        if m:
            sources_str = m.group("sources") or ""
            sources = [s.strip() for s in sources_str.split(",") if s.strip()]
            facts.append({
                "id": m.group("id"),
                "kind": m.group("kind"),
                "text": m.group("text").strip(),
                "sources": sources,
                "ticked": m.group("box").lower() == "x",
                "edited": bool(m.group("edited")),
            })
    return facts


import hashlib
import json


def compute_facts_hash(lines: list[str]) -> str:
    """Deterministic hash of facts and their tick statuses."""
    content = "\n".join(lines).strip()
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def sync_facts_json(config: ConfigDep, facts: list[dict[str, Any]]) -> None:
    profile_dir = config.resolve_path(config.profiles.profile_dir)
    json_path = profile_dir / "facts.json"
    try:
        json_path.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    except Exception:
        pass


@router.get("")
def list_facts(config: ConfigDep) -> dict[str, Any]:
    """Read facts_tex.md and return facts grouped by section with usage counts and facts hash."""
    facts_file = get_facts_file(config)
    if not facts_file.is_file():
        return {
            "facts": [],
            "grouped": {},
            "total_count": 0,
            "ticked_count": 0,
            "unticked_count": 0,
            "used_count": 0,
            "unused_count": 0,
            "facts_hash": "0000000000000000",
        }

    content = facts_file.read_text(encoding="utf-8")
    facts = parse_facts_lines(content)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for f in facts:
        grouped.setdefault(f["kind"], []).append(f)

    ticked_count = sum(1 for f in facts if f["ticked"])
    f_hash = compute_facts_hash(content.splitlines())
    return {
        "facts": facts,
        "grouped": grouped,
        "total_count": len(facts),
        "ticked_count": ticked_count,
        "unticked_count": len(facts) - ticked_count,
        "used_count": ticked_count,
        "unused_count": len(facts) - ticked_count,
        "facts_hash": f_hash,
    }


@router.post("/tick")
def update_fact_ticks(body: TickRequest, config: ConfigDep) -> dict[str, Any]:
    """Tick or untick specific facts by ID and update facts hash."""
    facts_file = get_facts_file(config)
    if not facts_file.is_file():
        raise HTTPException(status_code=404, detail="facts_tex.md does not exist")

    target_ids = set(body.fact_ids)
    target_box = "x" if body.ticked else " "
    lines = facts_file.read_text(encoding="utf-8").splitlines()
    updated_lines = []

    for line in lines:
        m = _FACT_LINE.match(line.strip())
        if m and m.group("id") in target_ids:
            updated_line = re.sub(r"^- \[[ xX]\]", f"- [{target_box}]", line)
            updated_lines.append(updated_line)
        else:
            updated_lines.append(line)

    text_content = "\n".join(updated_lines) + "\n"
    facts_file.write_text(text_content, encoding="utf-8")
    parsed = parse_facts_lines(text_content)
    sync_facts_json(config, parsed)

    f_hash = compute_facts_hash(updated_lines)
    ticked = sum(1 for f in parsed if f["ticked"])
    return {
        "status": "ok",
        "updated_count": len(target_ids),
        "facts_hash": f_hash,
        "used_count": ticked,
        "unused_count": len(parsed) - ticked,
    }


@router.post("/bulk-tick")
def bulk_tick_facts(body: BulkTickRequest, config: ConfigDep) -> dict[str, Any]:
    """Tick or untick all facts, or all facts of a specific kind."""
    facts_file = get_facts_file(config)
    if not facts_file.is_file():
        raise HTTPException(status_code=404, detail="facts_tex.md does not exist")

    target_box = "x" if body.ticked else " "
    lines = facts_file.read_text(encoding="utf-8").splitlines()
    updated_lines = []
    affected = 0

    for line in lines:
        m = _FACT_LINE.match(line.strip())
        if m and (body.kind is None or m.group("kind") == body.kind):
            updated_line = re.sub(r"^- \[[ xX]\]", f"- [{target_box}]", line)
            updated_lines.append(updated_line)
            affected += 1
        else:
            updated_lines.append(line)

    text_content = "\n".join(updated_lines) + "\n"
    facts_file.write_text(text_content, encoding="utf-8")
    parsed = parse_facts_lines(text_content)
    sync_facts_json(config, parsed)

    f_hash = compute_facts_hash(updated_lines)
    ticked = sum(1 for f in parsed if f["ticked"])
    return {
        "status": "ok",
        "affected_count": affected,
        "facts_hash": f_hash,
        "used_count": ticked,
        "unused_count": len(parsed) - ticked,
    }


@router.post("/edit")
def edit_fact(body: EditFactRequest, config: ConfigDep) -> dict[str, Any]:
    """Edit a fact in-place, generating a new stable hash and marking as edited by user."""
    facts_file = get_facts_file(config)
    if not facts_file.is_file():
        raise HTTPException(status_code=404, detail="facts_tex.md does not exist")

    lines = facts_file.read_text(encoding="utf-8").splitlines()
    new_text = body.text.strip()
    old_id = body.fact_id

    found = False
    new_id = ""
    updated_lines = []

    for line in lines:
        m = _FACT_LINE.match(line.strip())
        if m and m.group("id") == old_id:
            found = True
            kind = m.group("kind")
            new_id = fact_id(kind, new_text)
            sources = m.group("sources") or ""
            sources_part = f"  _[{sources}]_" if sources else ""
            new_line = f"- [x] **{new_id}** `{kind}` {new_text}{sources_part} (edited by you)"
            updated_lines.append(new_line)
        else:
            updated_lines.append(line)

    if not found:
        raise HTTPException(status_code=404, detail=f"Fact with ID '{old_id}' not found")

    text_content = "\n".join(updated_lines) + "\n"
    facts_file.write_text(text_content, encoding="utf-8")
    parsed = parse_facts_lines(text_content)
    sync_facts_json(config, parsed)

    f_hash = compute_facts_hash(updated_lines)
    ticked = sum(1 for f in parsed if f["ticked"])
    return {
        "status": "ok",
        "old_id": old_id,
        "new_id": new_id,
        "text": new_text,
        "facts_hash": f_hash,
        "used_count": ticked,
        "unused_count": len(parsed) - ticked,
    }
