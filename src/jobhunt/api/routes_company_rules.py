"""Company grade rules (data/profile/company_rules.yaml): list, preview a "Remember for this company" line, write it
only after an explicit confirmed request, and Remove an entry. The relevance gate itself never edits the file."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from jobhunt.api.database import ConfigDep, ReadDb
from jobhunt.matching import company_rules
from jobhunt.timeutil import utc_now

router = APIRouter(prefix="/api/company-rules", tags=["company-rules"])


class RuleOut(BaseModel):
    index: int                          # position of the first entry of this company in the file (what Remove deletes)
    entry_indexes: list[int]
    company: str
    aliases: list[str]
    allow_title_patterns: list[str]
    deny_title_patterns: list[str]
    allow_level_codes: list[str]
    deny_level_codes: list[str]
    deny_text_patterns: list[str]
    note: str


class RuleList(BaseModel):
    path: str
    rules: list[RuleOut]


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int = Field(ge=1)
    pattern: str | None = Field(default=None, max_length=company_rules.MAX_PATTERN_CHARS)


class Preview(BaseModel):
    company: str
    pattern: str
    text: str                           # the exact text that would be appended
    path: str
    already_denied: bool


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int = Field(ge=1)
    pattern: str = Field(max_length=company_rules.MAX_PATTERN_CHARS)
    text: str = Field(max_length=2000)  # must equal the preview text
    confirmed: bool = False


def _out(rule: company_rules.CompanyRule) -> RuleOut:
    return RuleOut(index=rule.entry_indexes[0] if rule.entry_indexes else rule.index,
                   entry_indexes=list(rule.entry_indexes), company=rule.company, aliases=list(rule.aliases),
                   allow_title_patterns=list(rule.allow_title_patterns), deny_title_patterns=list(rule.deny_title_patterns),
                   allow_level_codes=list(rule.allow_level_codes), deny_level_codes=list(rule.deny_level_codes),
                   deny_text_patterns=list(rule.deny_text_patterns), note=rule.note)


def _job(db: ReadDb, job_id: int) -> tuple[str, str]:
    row = db.execute("SELECT title, company_name FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return str(row["title"]), str(row["company_name"])


@router.get("", response_model=RuleList, summary="My company grade rules")
def list_rules(config: ConfigDep) -> RuleList:
    path = config.resolve_path(company_rules.COMPANY_RULES_FILE)
    return RuleList(path=company_rules.COMPANY_RULES_FILE, rules=[_out(rule) for rule in company_rules.load_company_rules(path)])


@router.post("/preview", response_model=Preview, summary="The exact text 'Remember for this company' would write")
def preview(payload: PreviewRequest, db: ReadDb, config: ConfigDep) -> Preview:
    title, company = _job(db, payload.job_id)
    pattern = (payload.pattern or company_rules.level_word_in_title(title) or "").strip()
    if not pattern:
        raise HTTPException(status_code=422, detail="no level word (Vice President, Director, ...) in this title")
    path = config.resolve_path(company_rules.COMPANY_RULES_FILE)
    rules = company_rules.load_company_rules(path)
    return Preview(company=company, pattern=pattern, path=company_rules.COMPANY_RULES_FILE,
                   text=company_rules.render_deny_entry(company, pattern, today=utc_now().date()),
                   already_denied=company_rules.already_denied(rules, company, pattern))


@router.post("", response_model=RuleList, summary="Write the previewed line (only with confirmed=true)")
def write_rule(payload: WriteRequest, db: ReadDb, config: ConfigDep) -> RuleList:
    if not payload.confirmed:
        raise HTTPException(status_code=422, detail="the file is only written after an explicit confirmation")
    _, company = _job(db, payload.job_id)
    pattern = payload.pattern.strip()
    expected = company_rules.render_deny_entry(company, pattern, today=utc_now().date())
    if payload.text != expected:
        raise HTTPException(status_code=409, detail="the text to write changed since the preview; preview again")
    path = config.resolve_path(company_rules.COMPANY_RULES_FILE)
    if not company_rules.already_denied(company_rules.load_company_rules(path), company, pattern):
        company_rules.append_deny_entry(path, expected)
    return RuleList(path=company_rules.COMPANY_RULES_FILE, rules=[_out(rule) for rule in company_rules.load_company_rules(path)])


@router.delete("/{entry_index}", response_model=RuleList, summary="Remove one entry of the rules file")
def remove_rule(entry_index: int, config: ConfigDep) -> RuleList:
    path = config.resolve_path(company_rules.COMPANY_RULES_FILE)
    if not path.is_file() or not company_rules.remove_entry(path, entry_index):
        raise HTTPException(status_code=404, detail="no such rule")
    return RuleList(path=company_rules.COMPANY_RULES_FILE, rules=[_out(rule) for rule in company_rules.load_company_rules(path)])
