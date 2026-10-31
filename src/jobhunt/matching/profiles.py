"""Resume -> profile text that is allowed to leave this machine.

`profile-build` extracts text from the resumes, removes personal contact details with
deterministic rules (never an LLM: the whole point is that nothing PII-bearing is ever sent
anywhere to be "cleaned"), verifies the result fails closed, and stores the text with its hash.
The stripped text is the only resume content Gemini ever sees.

Removed: the person's name, e-mail addresses, phone numbers, URLs / profile handles, date of
birth lines. The report keeps counts per category, never the removed values.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from jobhunt.config import Config
from jobhunt.errors import JobHuntError
from jobhunt.timeutil import Clock, to_iso, utc_now


class ProfileError(JobHuntError):
    """A resume could not be turned into a safe profile."""


# --- extraction -------------------------------------------------------------------------------

# "T ailwindCSS" -> "TailwindCSS" (a kerned capital). "A" and "I" are real words ("A unified portal").
_PDF_KERN_ARTIFACT = re.compile(r"\b([B-HJ-Z]) (?=[a-z])")
_HYPHEN_LINEBREAK = re.compile(r"(\w)-\n(\w)")             # "ser-\nvices" -> "services"
_LATEX_COMMAND = re.compile(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?")
_LATEX_BRACES = re.compile(r"[{}]")
_MULTISPACE = re.compile(r"[ \t]+")


def extract_text(path: Path) -> str:
    """Text of a .pdf (pypdf), .tex (commands stripped) or plain text resume."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            import pypdf
        except ImportError as error:  # pragma: no cover - environment dependent
            raise ProfileError('PDF extraction needs pypdf: pip install -e ".[matching]"') from error
        reader = pypdf.PdfReader(str(path))
        raw = "\n".join((page.extract_text() or "") for page in reader.pages)
    elif suffix == ".tex":
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:       # structured: the same block model tailoring uses
            from jobhunt.tailoring.profile_text import doc_to_text
            from jobhunt.tailoring.texdoc import parse

            raw = doc_to_text(parse(raw))
        except (ValueError, IndexError):
            raw = _LATEX_BRACES.sub(" ", _LATEX_COMMAND.sub(" ", raw.replace("\\\\", "\n")))
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
    return clean_extracted(raw)


def clean_extracted(raw: str) -> str:
    """Undo common PDF text-extraction artifacts and normalize whitespace."""
    text = raw.replace("\u2019", "'").replace("\u2018", "'").replace("\u00a0", " ")
    text = _HYPHEN_LINEBREAK.sub(r"\1\2", text)
    text = _PDF_KERN_ARTIFACT.sub(r"\1", text)
    text = text.replace("\u2022", "-")
    text = "\n".join(_MULTISPACE.sub(" ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- PII stripping ----------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b(?:[\w-]+\.)+(?:com|in|io|dev|app|org|net|me|co)(?:/\S*)?",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d(?![\w])")
_HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_.]{3,}")
_DOB_LINE = re.compile(r"^.*\b(?:date of birth|d\.?o\.?b\.?)\b.*$", re.IGNORECASE | re.MULTILINE)
_MIN_PHONE_DIGITS = 9
_SEPARATOR_RUNS = re.compile(r"(?:\s*[|\u00b7,;]\s*){2,}")


@dataclass(frozen=True)
class PiiResult:
    text: str
    removed: dict[str, int] = field(default_factory=dict)


def guess_person_name(text: str) -> str | None:
    """The header name: the first non-empty line, when it looks like 2-4 plain name words."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        words = line.split()
        if 2 <= len(words) <= 4 and all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", word) for word in words):
            if not any(char.isdigit() for char in line):
                return line
        return None
    return None


def strip_pii(text: str, *, person_name: str | None = None) -> PiiResult:
    """Deterministically remove contact details; the returned text is verified by `assert_clean`."""
    removed: dict[str, int] = {}

    def remove(pattern: re.Pattern[str], category: str, text: str) -> str:
        text, count = pattern.subn(" ", text)
        if count:
            removed[category] = removed.get(category, 0) + count
        return text

    def drop_phone(match: re.Match[str]) -> str:
        # "2025-2026" has 8 digits and is a year range; a phone number has 9 or more.
        if sum(char.isdigit() for char in match.group(0)) < _MIN_PHONE_DIGITS:
            return match.group(0)
        removed["phone"] = removed.get("phone", 0) + 1
        return " "

    cleaned = remove(_DOB_LINE, "date_of_birth", text)
    cleaned = remove(_EMAIL, "email", cleaned)
    cleaned = remove(_URL, "url", cleaned)
    cleaned = remove(_HANDLE, "handle", cleaned)
    cleaned = _PHONE.sub(drop_phone, cleaned)

    name = person_name if person_name is not None else guess_person_name(text)
    if name:
        for token in sorted({name, *re.split(r"\s+", name)}, key=len, reverse=True):
            token = token.strip(".")
            if len(token) < 3:
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(token)}(?!\w)", re.IGNORECASE)
            cleaned, count = pattern.subn(" ", cleaned)
            if count:
                removed["name"] = removed.get("name", 0) + count

    lines = []
    for line in cleaned.splitlines():
        line = _SEPARATOR_RUNS.sub(" ", line).strip(" |\u00b7").lstrip(",; ")
        line = _MULTISPACE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return PiiResult("\n".join(lines), removed)


def assert_clean(text: str, *, person_name: str | None = None) -> None:
    """Fail closed: raise if any contact-detail pattern or name token survived."""
    for label, pattern in (("e-mail", _EMAIL), ("url", _URL), ("handle", _HANDLE)):
        if pattern.search(text):
            raise ProfileError(f"PII check failed: a {label} survived stripping; refusing to build the profile")
    for match in _PHONE.finditer(text):
        if sum(char.isdigit() for char in match.group(0)) >= _MIN_PHONE_DIGITS:
            raise ProfileError("PII check failed: a phone number survived stripping; refusing to build the profile")
    if person_name:
        for token in re.split(r"\s+", person_name):
            if len(token.strip(".")) >= 3 and re.search(rf"(?<!\w){re.escape(token.strip('.'))}(?!\w)", text, re.IGNORECASE):
                raise ProfileError("PII check failed: the person's name survived stripping; refusing to build the profile")


# --- building ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltProfile:
    name: str
    source_file: str
    source_hash: str
    text_hash: str
    text: str
    removed: dict[str, int]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_profile(name: str, path: Path) -> BuiltProfile:
    raw = extract_text(path)
    if len(raw) < 200:
        raise ProfileError(f"{path.name}: extracted only {len(raw)} characters; is the PDF scanned or empty?")
    person = guess_person_name(raw)
    result = strip_pii(raw, person_name=person)
    assert_clean(result.text, person_name=person)
    return BuiltProfile(
        name=name, source_file=path.name, source_hash=sha256_text(raw), text_hash=sha256_text(result.text),
        text=result.text, removed=result.removed,
    )


def store_profiles(
    connection: sqlite3.Connection, profiles: list[BuiltProfile], profile_dir: Path | None, *, clock: Clock = utc_now
) -> None:
    now = to_iso(clock())
    connection.execute("BEGIN IMMEDIATE")
    try:
        for profile in profiles:
            try:
                connection.execute(
                    """INSERT INTO profiles_v2 (base_id, source_file, source_hash, text_hash, stripped_text, pii_removed, built_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(base_id) DO UPDATE SET source_file = excluded.source_file,
                           source_hash = excluded.source_hash, text_hash = excluded.text_hash,
                           stripped_text = excluded.stripped_text, pii_removed = excluded.pii_removed,
                           built_at = excluded.built_at""",
                    (profile.name, profile.source_file, profile.source_hash, profile.text_hash, profile.text,
                     json.dumps(profile.removed, sort_keys=True), now),
                )
            except sqlite3.OperationalError:
                pass
            if profile.name in ("ai", "fullstack"):
                connection.execute(
                    """INSERT INTO profiles (name, source_file, source_hash, text_hash, stripped_text, pii_removed, built_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET source_file = excluded.source_file,
                           source_hash = excluded.source_hash, text_hash = excluded.text_hash,
                           stripped_text = excluded.stripped_text, pii_removed = excluded.pii_removed,
                           built_at = excluded.built_at""",
                    (profile.name, profile.source_file, profile.source_hash, profile.text_hash, profile.text,
                     json.dumps(profile.removed, sort_keys=True), now),
                )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    if profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        for profile in profiles:
            (profile_dir / f"{profile.name}.txt").write_text(profile.text + "\n", encoding="utf-8")


# --- the command ------------------------------------------------------------------------------


@dataclass
class ProfileBuildSummary:
    profiles: list[BuiltProfile]
    conflicts: int
    unconfirmed: int
    facts_path: Path | None
    tex_facts: object | None = None          # TexFactsSummary when .tex sources exist
    skipped_optional: list[str] = field(default_factory=list)      # optional bases whose resume file is absent


def run_profile_build(connection: sqlite3.Connection, config: Config, *, clock: Clock = utc_now) -> ProfileBuildSummary:
    """Build every configured profile, store them, and (re)write data/profile/review_facts.md."""
    from jobhunt.matching.facts import compare_resumes, existing_ticks, render_review_facts
    from jobhunt.profile import get_profile

    p = get_profile(config)
    settings = config.profiles
    resume_dir = config.resolve_path(settings.resume_dir)
    profile_dir = config.resolve_path(settings.profile_dir)
    built: list[BuiltProfile] = []
    skipped: list[str] = []
    if settings.files and not any(config.resolve_path(b.tex_path).is_file() for b in (p.bases if p else ())):
        bases_to_build = [(name, str(resume_dir / filename), name in settings.optional) for name, filename in settings.files.items()]
    elif p and p.bases:
        bases_to_build = [(b.id, b.tex_path, b.optional) for b in p.bases]
    else:
        bases_to_build = [(name, str(resume_dir / filename), name in settings.optional) for name, filename in settings.files.items()]
    for name, filepath, is_optional in bases_to_build:
        path = config.resolve_path(filepath)
        if not path.is_file() and is_optional:
            skipped.append(name)                  # an optional base (for example resume_backend.tex): skip silently
            continue
        if not path.is_file():
            raise ProfileError(f"resume not found: {path} (put it in {settings.resume_dir}/ or edit profile)")
        built.append(build_profile(name, path))
        if settings.person_name:
            assert_clean(built[-1].text, person_name=settings.person_name)

    # keep the last accepted profile text as the "confirmed baseline" before it is replaced (never overwritten)
    for name, _, _ in bases_to_build:
        old = profile_dir / f"{name}.txt"
        pinned = profile_dir / f"baseline_{name}.txt"
        if old.is_file() and not pinned.is_file():
            pinned.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")

    # the matching tables know two profiles; an optional base is used by tailoring and the fact list only
    optional_names = {b[0] for b in bases_to_build if b[2]}
    store_profiles(connection, [profile for profile in built if profile.name not in optional_names], profile_dir,
                   clock=clock)

    facts_path: Path | None = None
    conflicts = unconfirmed = 0
    uses_tex = any(str(filename).lower().endswith(".tex") for filename in settings.files.values())
    if not uses_tex and settings.authoritative in {profile.name for profile in built} and len(built) > 1:
        items = compare_resumes(settings.authoritative, {profile.name: profile.text for profile in built})
        facts_path = profile_dir / "review_facts.md"
        ticks = existing_ticks(facts_path.read_text(encoding="utf-8")) if facts_path.is_file() else {}
        facts_path.write_text(render_review_facts(items, settings.authoritative, ticks), encoding="utf-8")
        conflicts = sum(1 for item in items if item.category == "conflict")
        unconfirmed = sum(1 for item in items if item.category == "unconfirmed")
    tex_summary = build_tex_facts(config, built)
    return ProfileBuildSummary(built, conflicts, unconfirmed, facts_path, tex_summary, skipped)


@dataclass
class TexFactsSummary:
    facts: int = 0
    ticked: int = 0
    new_or_changed: list = field(default_factory=list)      # list[TexFact], unticked until you tick them
    preserved: int = 0
    carried_over: int = 0
    path: Path | None = None


def build_tex_facts(config: Config, built: list[BuiltProfile]) -> TexFactsSummary:
    """Parse the .tex sources into facts and (re)write data/profile/facts_tex.md + facts.json (ticks preserved)."""
    from jobhunt.tailoring import facts as fact_module
    from jobhunt.tailoring.texdoc import parse

    settings = config.profiles
    resume_dir = config.resolve_path(settings.resume_dir)
    profile_dir = config.resolve_path(settings.profile_dir)
    per_resume: dict[str, list] = {}
    order = [settings.authoritative] + [name for name in settings.files if name != settings.authoritative]
    for name in order:
        filename = settings.files.get(name, f"resume_{name}.tex")
        tex = resume_dir / filename
        if not tex.is_file():
            tex = resume_dir / f"resume_{name}.tex"
        if not tex.is_file():
            continue
        per_resume[name] = fact_module.extract(name, parse(tex.read_text(encoding="utf-8")))
    summary = TexFactsSummary()
    if not per_resume:
        return summary
    merged = fact_module.merge(per_resume)
    baselines = {profile.name: profile.text for profile in built}
    previous_pdf_text = {}
    for name in settings.files:
        old = profile_dir / f"{name}.txt"
        if old.is_file():
            previous_pdf_text[name] = old.read_text(encoding="utf-8")
    # confirmed baseline = the last accepted profile text (before this build overwrote it is not available any
    # more), so prefer the text stored from the PDF era when present in data/profile/baseline_*.txt
    for name in settings.files:
        pinned = profile_dir / f"baseline_{name}.txt"
        if pinned.is_file():
            previous_pdf_text[name] = pinned.read_text(encoding="utf-8")
    result = fact_module.reconcile(
        merged, profile_dir / "facts_tex.md", profile_dir / "review_facts.md",
        previous_pdf_text or baselines, settings.authoritative,
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "facts_tex.md").write_text(fact_module.render_facts_md(result), encoding="utf-8")
    (profile_dir / "facts.json").write_text(fact_module.facts_to_json(result), encoding="utf-8")
    summary.facts, summary.ticked = len(result.facts), sum(1 for v in result.ticked.values() if v)
    summary.new_or_changed, summary.preserved = result.new_or_changed, result.preserved
    summary.carried_over, summary.path = result.carried_over, profile_dir / "facts_tex.md"
    return summary
