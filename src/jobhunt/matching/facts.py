"""`data/profile/review_facts.md`: every resume claim that needs a human yes before it is reused.

Step 4 (resume tailoring) may only use CONFIRMED facts. The authoritative resume (`ai`) is the
source of truth; the older one can only disagree with it (a conflict, resolved to the
authoritative value) or say something the authoritative one never says (unconfirmed). Every such
item gets a checkbox and a stable id, so re-running `profile-build` keeps the ticks you made.

Deterministic text comparison only: no model is involved and no resume text leaves the machine.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from jobhunt.profile_terms import EVENT_NAME, TEAM_NAME

_SECTION_HEADINGS = (
    "SUMMARY", "EDUCATION", "PROFESSIONAL EXPERIENCE", "EXPERIENCE", "PROJECTS", "SKILLS",
    "ADDITIONAL INFORMATION", "CERTIFICATIONS", "ACHIEVEMENTS",
)
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_STOPWORDS = frozenset(
    "a an and the of for to in on with by from as at is are was were be been into over per via using use used".split()
)
# a number with a unit or "+" ("15k+", "99.9%"), or a long number ("1000+"); bare "2.1" is a version
_METRIC = re.compile(r"\d[\d,.]*\s?(?:k|m|%)\+?|\d[\d,]{2,}\+?")
_EDITION = re.compile(re.escape(EVENT_NAME) + r"\s*'\s*(\d{2})")
_ROLE_AT = re.compile(r"(?:^|\n)(?:-\s*)?([A-Z][A-Za-z ]{2,40}?),\s+(" + re.escape(TEAM_NAME) + r")")
_ACTIVITY_ROLE = re.compile(r"([A-Z][a-z]+(?: [A-Z][a-z]+)?),\s+(" + re.escape(TEAM_NAME) + r")")
_DATE_TAIL = re.compile(r"\s+((?:[A-Z][a-z]{2}\.?\s*)?\d{4}\s*[-–]\s*(?:[A-Z][a-z]{2}\.?\s*\d{4}|Present).*)$")


@dataclass(frozen=True)
class FactItem:
    category: str      # conflict | unconfirmed
    kind: str          # edition | role | stack | bullet | skill | metric | project | coursework
    text: str          # what to confirm
    detail: str = ""   # context shown under it

    @property
    def item_id(self) -> str:
        digest = hashlib.sha1(f"{self.category}|{self.kind}|{self.text}".encode("utf-8")).hexdigest()
        return f"{'C' if self.category == 'conflict' else 'F'}-{digest[:6]}"


def sections(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"HEADER": []}
    current = "HEADER"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper() in _SECTION_HEADINGS and stripped == stripped.upper():
            current = stripped.upper()
            result.setdefault(current, [])
            continue
        result.setdefault(current, []).append(stripped)
    return result


def tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if len(word) >= 3 and word not in _STOPWORDS}


def jaccard(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def bullets(lines: list[str]) -> list[str]:
    """Join wrapped lines: a bullet starts with '- ' and runs until the next bullet / blank / heading-ish line."""
    items: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("- "):
            if current:
                items.append(" ".join(current))
            current = [line[2:].strip()]
        elif line and current and not _looks_like_header(line):
            current.append(line)
        else:
            if current:
                items.append(" ".join(current))
            current = []
    if current:
        items.append(" ".join(current))
    return items


_MONTH_RANGE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4}\s*[-–]\s*(?:[A-Z][a-z]+\.?\s*\d{4}|Present)"
)


def _looks_like_header(line: str) -> bool:
    """A project or role header line ("Name | stack  Aug 2025 - Present", "Role, Org  Dec 2025 - Feb 2026")."""
    return ("|" in line and bool(re.search(r"\d{4}", line))) or bool(_MONTH_RANGE.search(line))


@dataclass(frozen=True)
class ProjectBlock:
    name: str
    stack: tuple[str, ...]
    dates: str
    bullets: tuple[str, ...]


def project_blocks(lines: list[str]) -> list[ProjectBlock]:
    blocks: list[ProjectBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if "|" in line and not line.startswith("- "):
            name, _, rest = line.partition("|")
            date_match = _DATE_TAIL.search(rest)
            dates = date_match.group(1) if date_match else ""
            stack_text = rest[: date_match.start()] if date_match else rest
            stack = tuple(part.strip() for part in stack_text.split(",") if part.strip())
            body: list[str] = []
            index += 1
            while index < len(lines) and not ("|" in lines[index] and not lines[index].startswith("- ")):
                body.append(lines[index])
                index += 1
            blocks.append(ProjectBlock(name.strip(), stack, dates, tuple(bullets(body))))
            continue
        index += 1
    return blocks


def _pair_blocks(authoritative: list[ProjectBlock], other: list[ProjectBlock]) -> list[tuple[ProjectBlock, ProjectBlock]]:
    pairs: list[tuple[ProjectBlock, ProjectBlock]] = []
    for block in other:
        best = max(authoritative, key=lambda candidate: jaccard(tokens(candidate.name), tokens(block.name)), default=None)
        if best is not None and jaccard(tokens(best.name), tokens(block.name)) >= 0.5:
            pairs.append((best, block))
    return pairs


def _skill_tokens(lines: list[str]) -> list[str]:
    """Individual skills from a SKILLS section ("Backend: FastAPI, NestJS (SQLAlchemy), AWS (EC2, S3)")."""
    skills: list[str] = []
    for line in lines:
        _, _, rest = line.partition(":")
        rest = rest or line
        depth = 0
        buffer = ""
        for char in rest:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            if char == "," and depth == 0:
                if buffer.strip():
                    skills.append(buffer.strip())
                buffer = ""
            else:
                buffer += char
        if buffer.strip():
            skills.append(buffer.strip())
    return skills


def _contains(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(needle.lower())}(?![A-Za-z0-9])", haystack.lower()) is not None


def compare_resumes(authoritative_name: str, texts: dict[str, str]) -> list[FactItem]:
    """Conflicts and unconfirmed claims of every non-authoritative resume vs. the authoritative one."""
    base_text = texts[authoritative_name]
    base_sections = sections(base_text)
    base_blocks = project_blocks(base_sections.get("PROJECTS", []))
    base_all_bullets = [
        *bullets(base_sections.get("PROFESSIONAL EXPERIENCE", base_sections.get("EXPERIENCE", []))),
        *(item for block in base_blocks for item in block.bullets),
    ]
    items: list[FactItem] = []

    for name, text in texts.items():
        if name == authoritative_name:
            continue
        other_sections = sections(text)
        other_blocks = project_blocks(other_sections.get("PROJECTS", []))
        experience_lines = other_sections.get("PROFESSIONAL EXPERIENCE", other_sections.get("EXPERIENCE", []))
        other_bullets = [*bullets(experience_lines), *(item for block in other_blocks for item in block.bullets)]
        label = f"resume_{name}"

        # -- conflicts ---------------------------------------------------------------------------
        base_editions, other_editions = set(_EDITION.findall(base_text)), set(_EDITION.findall(text))
        if base_editions and other_editions and base_editions != other_editions:
            items.append(FactItem(
                "conflict", "edition",
                f"{EVENT_NAME} edition label: authoritative resume says '{', '.join(sorted(base_editions))}', "
                f"{label} says '{', '.join(sorted(other_editions))}'",
                "Resolved to the authoritative value.",
            ))

        base_roles = {m.group(1).strip() for m in _ACTIVITY_ROLE.finditer(base_text)}
        other_roles = {m.group(1).strip() for m in _ACTIVITY_ROLE.finditer(text)}
        stale_roles = other_roles - base_roles
        if base_roles and stale_roles:
            items.append(FactItem(
                "conflict", "role",
                f"Role at {TEAM_NAME}: authoritative '{', '.join(sorted(base_roles))}' vs {label} "
                f"'{', '.join(sorted(stale_roles))}'",
                "Resolved to the authoritative value.",
            ))

        for base_block, other_block in _pair_blocks(base_blocks, other_blocks):
            base_stack = {part.lower() for part in base_block.stack}
            other_stack = {part.lower() for part in other_block.stack}
            if base_stack != other_stack:
                items.append(FactItem(
                    "conflict", "stack",
                    f"Stack of '{base_block.name}': authoritative [{', '.join(base_block.stack)}] vs "
                    f"{label} [{', '.join(other_block.stack)}]",
                    "Resolved to the authoritative stack.",
                ))

        used_base: set[int] = set()
        for bullet in other_bullets:
            scores = [jaccard(tokens(bullet), tokens(candidate)) for candidate in base_all_bullets]
            if not scores:
                continue
            best_index = max(range(len(scores)), key=scores.__getitem__)
            best = scores[best_index]
            if 0.30 <= best < 0.97:
                used_base.add(best_index)
                other_only = sorted(tokens(bullet) - tokens(base_all_bullets[best_index]))[:12]
                items.append(FactItem(
                    "conflict", "bullet",
                    f"Reworded claim: {label} says \"{bullet}\"",
                    f"Authoritative version: \"{base_all_bullets[best_index]}\" "
                    f"(words only in {label}: {', '.join(other_only) or 'none'}). Resolved to the authoritative text.",
                ))
            elif best < 0.30:
                items.append(FactItem("unconfirmed", "bullet", f"Only in {label}: \"{bullet}\""))

        # -- unconfirmed -------------------------------------------------------------------------
        for block in other_blocks:
            if not any(block.name == pair[1].name for pair in _pair_blocks(base_blocks, other_blocks)):
                items.append(FactItem("unconfirmed", "project",
                                      f"Project only in {label}: {block.name} ({', '.join(block.stack)}; {block.dates})"))

        base_skill_text = " ".join(base_sections.get("SKILLS", []))
        for skill in _skill_tokens(other_sections.get("SKILLS", [])):
            core = re.sub(r"\s*\(.*?\)\s*", " ", skill).strip()
            base_words = {word.rstrip("s") for word in _WORD.findall(base_text.lower())}
            skill_words = {word.rstrip("s") for word in _WORD.findall(core.lower())}
            variant_present = bool(skill_words) and skill_words <= base_words   # "OpenAI API" ~ "OpenAI & Gemini APIs"
            if core and not variant_present and not _contains(base_text, core) and not _contains(base_skill_text, core):
                items.append(FactItem("unconfirmed", "skill", f"Skill only in {label}: {skill}"))

        base_metrics = {m.group(0).strip().rstrip("+").lower() for m in _METRIC.finditer(base_text)}
        for match in _METRIC.finditer(text):
            metric = match.group(0).strip()
            core = metric.rstrip("+").lower()
            if len(re.sub(r"\D", "", core)) < 2 or core in base_metrics:
                continue
            if re.fullmatch(r"(19|20)\d{2}", core):
                continue
            window = text[max(0, match.start() - 40): match.end() + 40].replace("\n", " ")
            items.append(FactItem("unconfirmed", "metric", f"Number only in {label}: {metric}", f"...{window}..."))

        base_course = " ".join(base_sections.get("EDUCATION", []))
        for line in other_sections.get("EDUCATION", []):
            if line.lower().startswith("selected coursework"):
                for course in re.split(r",\s*", line.partition(":")[2]):
                    course = course.strip(" .")
                    if course and not _contains(base_course, course):
                        items.append(FactItem("unconfirmed", "coursework", f"Coursework only in {label}: {course}"))

    seen: set[str] = set()
    unique: list[FactItem] = []
    for item in items:
        if item.item_id not in seen:
            seen.add(item.item_id)
            unique.append(item)
    return unique


_TICK = re.compile(r"^- \[(?P<mark>[xX ])\] \*\*(?P<id>[CF]-[0-9a-f]{6})\*\*", re.MULTILINE)


def existing_ticks(markdown: str) -> dict[str, bool]:
    return {match.group("id"): match.group("mark").lower() == "x" for match in _TICK.finditer(markdown)}


def render_review_facts(items: list[FactItem], authoritative: str, ticks: dict[str, bool] | None = None) -> str:
    ticks = ticks or {}
    conflicts = [item for item in items if item.category == "conflict"]
    unconfirmed = [item for item in items if item.category == "unconfirmed"]

    def line(item: FactItem) -> str:
        mark = "x" if ticks.get(item.item_id) else " "
        detail = f"\n  - {item.detail}" if item.detail else ""
        return f"- [{mark}] **{item.item_id}** `{item.kind}` {item.text}{detail}"

    parts = [
        "# Facts to review before resume tailoring (step 4)",
        "",
        f"Source of truth: `resume_{authoritative}`. Step 4 uses a fact only when its box is ticked `[x]`.",
        "Edit a line to correct it, tick it to confirm it, delete it to drop it. Re-running `profile-build`",
        "keeps your ticks (ids are stable). This file is local only (`data/profile/` is gitignored).",
        "",
        f"## Conflicts ({len(conflicts)}): the older resume disagrees; resolved to the authoritative value",
        "",
        *([line(item) for item in conflicts] or ["_None found._"]),
        "",
        f"## Unconfirmed ({len(unconfirmed)}): only the older resume says this (not contradicted)",
        "",
        *([line(item) for item in unconfirmed] or ["_None found._"]),
        "",
    ]
    return "\n".join(parts)
