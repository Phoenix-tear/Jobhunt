"""Structured facts from the LaTeX resume sources, with stable ids and a tick file (`facts_tex.md`).

A fact is one claim: a bullet, a project / experience header, a skill item, a certification, a contact or
education value, the summary. Its id is a hash of its kind and normalized text, so it survives re-parsing and
unrelated edits; an edited fact gets a NEW id and starts unticked. Only ticked `[x]` facts may be used for
tailoring. Ticks you made in `facts_tex.md` are preserved; facts that existed in the earlier PDF-based review
(`review_facts.md`, whose boxes you ticked) or in the authoritative PDF text are carried over automatically.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from jobhunt.tailoring.texdoc import Entry, ResumeDoc, plain, split_top_level

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_TICK = re.compile(r"^- \[(?P<mark>[ xX])\] \*\*(?P<id>T-[0-9a-f]{8})\*\*")


def normalize(text: str) -> str:
    return " ".join(_WORD.findall(text.lower().replace("~", " ")))


def fact_id(kind: str, text: str) -> str:
    digest = hashlib.sha1(f"{kind}|{normalize(text)}".encode("utf-8")).hexdigest()
    return f"T-{digest[:8]}"


def entry_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", plain(name).lower()).strip("-")[:48]


@dataclass(frozen=True)
class TexFact:
    id: str
    kind: str                 # contact | headline | summary | education | experience | experience_bullet |
                              # project | project_bullet | skill_group | skill | certification | achievement | language
    text: str                 # plain text
    raw: str                  # the LaTeX it came from (inserted verbatim when reproduced)
    sources: tuple[str, ...]  # which resume files state it
    entry: str = ""           # parent entry id (bullets), or group name (skills)
    order: int = 0
    group: str = ""


def _make(kind: str, raw: str, source: str, *, entry: str = "", order: int = 0, group: str = "",
          text: str | None = None) -> TexFact:
    value = text if text is not None else plain(raw)
    return TexFact(fact_id(kind + ("|" + group if group and kind == "skill" else ""), value), kind, value, raw,
                   (source,), entry, order, group)


def extract(name: str, doc: ResumeDoc) -> list[TexFact]:
    facts: list[TexFact] = []
    header = doc.header
    for email in re.findall(r"\\href\{mailto:([^}]*)\}", header):
        facts.append(_make("contact", email, name, text=f"email: {email}"))
    phone = re.search(r"\+\d[\d ]{8,}", header)
    if phone:
        facts.append(_make("contact", phone.group(0).strip(), name, text=f"phone: {phone.group(0).strip()}"))
    for url in re.findall(r"\\href\{(https?://[^}]*linkedin[^}]*)\}", header):
        facts.append(_make("contact", url, name, text=f"linkedin: {url}"))
    headline = re.search(r"\\textcolor\{mediumgray\}\{([^}]*)\}", header)
    if headline:
        facts.append(_make("headline", headline.group(1), name))
    if doc.summary:
        facts.append(_make("summary", doc.summary, name))
    # education: school / degree / dates / cgpa / coursework / school marks
    edu = doc.education
    school = re.search(r"\\textbf\{([^}]*)\}\s*\\hfill\s*\\textit\{([^}]*)\}", edu)
    if school:
        facts.append(_make("education", school.group(1), name, text=f"school: {plain(school.group(1))}"))
        facts.append(_make("education", school.group(2), name, text=f"dates: {plain(school.group(2))}"))
    degree = re.search(r"\\textit\{(B\.Tech[^}]*)\}\s*\\hfill\s*\\textit\{([^}]*)\}", edu)
    if degree:
        facts.append(_make("education", degree.group(1), name, text=f"degree: {plain(degree.group(1))}"))
        facts.append(_make("education", degree.group(2), name, text=f"{plain(degree.group(2))}"))
    course = re.search(r"Selected Coursework:\}?\s*(.*?)(?:\\\\|$)", edu, flags=re.DOTALL)
    if course:
        for item in split_top_level(plain(course.group(1))):
            facts.append(_make("education", item, name, text=f"coursework: {item}"))
    marks = re.search(r"Class XII:\}\s*([\d.]+\\%)\s*\\,\|\\,\s*\\textit\{Class X:\}\s*([\d.]+\\%)", edu)
    if marks:
        facts.append(_make("education", marks.group(1), name, text=f"Class XII: {plain(marks.group(1))}"))
        facts.append(_make("education", marks.group(2), name, text=f"Class X: {plain(marks.group(2))}"))
    # experience and projects
    for kind, entries in (("experience", doc.experience), ("project", doc.projects)):
        for entry in entries:
            header_text = _entry_header(entry)
            key = fact_id(kind, entry_slug(entry.args[0]))
            facts.append(TexFact(key, kind, header_text, "|".join(entry.args), (name,), "", 0, ""))
            for position, bullet in enumerate(entry.bullets):
                facts.append(_make(f"{kind}_bullet", bullet, name, entry=key, order=position))
    # skills
    for line in doc.skills:
        group_text = plain(line.group)
        facts.append(_make("skill_group", line.group, name, text=group_text))
        for position, item in enumerate(line.items):
            facts.append(_make("skill", item, name, entry=group_text, order=position, group=group_text))
    # additional information
    for raw in re.findall(r"\\href\{[^}]*\}\{([^}]*)\}", doc.additional):
        facts.append(_make("certification", raw, name))
    achievements = re.search(r"\\textbf\{Achievements:\}\s*(.*?)\s*\\textbf\{Languages", doc.additional, flags=re.DOTALL)
    if achievements:
        facts.append(_make("achievement", achievements.group(1).strip(), name))
    languages = re.search(r"\\textbf\{Languages:\}\s*([^\\]*)", doc.additional)
    if languages:
        for item in split_top_level(plain(languages.group(1)).rstrip(".")):
            facts.append(_make("language", item, name))
    return facts


def _entry_header(entry: Entry) -> str:
    return " | ".join(part for part in (plain(arg) for arg in entry.args) if part)


def merge(per_resume: dict[str, list[TexFact]]) -> list[TexFact]:
    """One fact per id, remembering every resume that states it (first resume = authoritative order)."""
    merged: dict[str, TexFact] = {}
    for facts in per_resume.values():
        for fact in facts:
            existing = merged.get(fact.id)
            if existing is None:
                merged[fact.id] = fact
            else:
                merged[fact.id] = TexFact(existing.id, existing.kind, existing.text, existing.raw,
                                          tuple(dict.fromkeys(existing.sources + fact.sources)), existing.entry,
                                          existing.order, existing.group)
    return list(merged.values())


# --- ticks ---------------------------------------------------------------------------------------------------------


@dataclass
class Reconciliation:
    facts: list[TexFact]
    ticked: dict[str, bool]
    new_or_changed: list[TexFact] = field(default_factory=list)
    carried_over: int = 0
    preserved: int = 0


def read_ticks(path: Path) -> dict[str, bool]:
    ticks: dict[str, bool] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _TICK.match(line)
            if match:
                ticks[match["id"]] = match["mark"].lower() == "x"
    return ticks


def _review_confirmed_texts(review_path: Path) -> set[str]:
    """Normalized texts the user ticked in the older PDF-based review_facts.md."""
    texts: set[str] = set()
    if not review_path.is_file():
        return texts
    for line in review_path.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^- \[[xX]\] ", line):
            continue
        for quoted in re.findall(r'"([^"]{8,})"', line):
            texts.add(normalize(quoted))
        skill = re.search(r"Skill only in \w+: (.+)$", line)
        if skill:
            texts.add(normalize(skill.group(1)))
        project = re.search(r"Project only in \w+: ([^(]+)\(", line)
        if project:
            texts.add(normalize(project.group(1)))
    return texts


def _baseline_sentences(profile_text: str) -> set[str]:
    texts = {normalize(line) for line in profile_text.splitlines() if line.strip()}
    joined = re.sub(r"\n- ", "\n@@", profile_text)
    for chunk in re.split(r"\n@@|\n\n", joined):
        texts.add(normalize(chunk.replace("\n", " ")))
    return {t for t in texts if t}


def _covered(text: str, baseline: set[str], tokens_of: dict[str, set[str]]) -> bool:
    normalized = normalize(text)
    if not normalized:
        return False
    if normalized in baseline:
        return True
    words = set(normalized.split())
    if len(words) < 4:
        return any(normalized in sentence for sentence in baseline)
    for sentence in baseline:
        sentence_words = tokens_of.setdefault(sentence, set(sentence.split()))
        if len(words & sentence_words) / len(words | sentence_words) >= 0.92:
            return True
    return False


def reconcile(facts: list[TexFact], facts_path: Path, review_path: Path, baseline_texts: dict[str, str],
              authoritative: str) -> Reconciliation:
    """Decide the tick of every fact. Order: your own tick in facts_tex.md > unchanged since the confirmed PDFs >
    ticked in the old review. Everything else is new/changed and stays unticked."""
    previous = read_ticks(facts_path)
    confirmed = _review_confirmed_texts(review_path)
    authoritative_baseline = _baseline_sentences(baseline_texts.get(authoritative, ""))
    cache: dict[str, set[str]] = {}
    result = Reconciliation(facts=facts, ticked={})
    for fact in facts:
        if fact.id in previous:
            result.ticked[fact.id] = previous[fact.id]
            result.preserved += 1
            continue
        carried = False
        if fact.kind in {"experience", "project"}:
            carried = _covered(fact.text.split(" | ")[0], authoritative_baseline, cache) \
                or normalize(fact.text.split(" | ")[0]) in confirmed
        else:
            carried = _covered(fact.text, authoritative_baseline, cache) or normalize(fact.text) in confirmed
            if not carried and fact.kind == "skill":
                carried = any(re.search(rf"(?<![a-z0-9]){re.escape(normalize(fact.text))}(?![a-z0-9])", s)
                              for s in authoritative_baseline) or normalize(fact.text) in confirmed
        result.ticked[fact.id] = carried
        if carried:
            result.carried_over += 1
        else:
            result.new_or_changed.append(fact)
    return result


def render_facts_md(result: Reconciliation) -> str:
    out = [
        "# Resume facts parsed from the .tex sources",
        "",
        "Tailoring may use a fact only when its box is ticked `[x]`. Edit the .tex, re-run `profile-build`: ticks you",
        "made here are kept (ids are stable); anything new or changed arrives unticked. Local file (gitignored).",
        "",
        f"New or changed since the last confirmed version: **{len(result.new_or_changed)}** (unticked).",
        "",
    ]
    order = ["contact", "headline", "summary", "education", "experience", "experience_bullet", "project",
             "project_bullet", "skill_group", "skill", "certification", "achievement", "language"]
    titles = {k: k.replace("_", " ") for k in order}
    for kind in order:
        group = [f for f in result.facts if f.kind == kind]
        if not group:
            continue
        out.append(f"## {titles[kind]} ({len(group)})")
        out.append("")
        for fact in group:
            box = "x" if result.ticked.get(fact.id) else " "
            where = ", ".join(fact.sources)
            tail = f"  _[{where}]_"
            text = fact.text if len(fact.text) <= 400 else fact.text[:397] + "..."
            out.append(f"- [{box}] **{fact.id}** `{fact.kind}` {text}{tail}")
        out.append("")
    return "\n".join(out)


def facts_to_json(result: Reconciliation) -> str:
    return json.dumps([
        {"id": f.id, "kind": f.kind, "text": f.text, "raw": f.raw, "sources": list(f.sources), "entry": f.entry,
         "order": f.order, "group": f.group, "ticked": bool(result.ticked.get(f.id))}
        for f in result.facts
    ], indent=1, ensure_ascii=False)
