"""Supported resume template contract, template detection, section extraction, and capability diagnostics (Brief v12b/v13b).

Templates supported:
(a) custom_macros: Custom two-style LaTeX resume with \\experienceitem / \\projectitem macros
(b) jakes_resume: Standard Jake's Resume macros (\\resumeSubheading, \\resumeProjectHeading, \\resumeItem)
(c) plain_itemize: Standard LaTeX \\section + \\begin{itemize} + \\item with \\textbf / \\href
(d) awesome_cv: Awesome-CV style (\\cventry with 5 args, \\cvskills, \\begin{cvitems})
(e) moderncv: Moderncv style (\\cventry with 6 args, \\cvitem, \\cvlistitem) - facts & letters only
(f) unsupported: Odd or unrecognized structure (tailoring disabled, facts & letters only)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.tailoring.facts import TexFact, fact_id
from jobhunt.tailoring.texdoc import Entry, ResumeDoc, parse, plain

# Regex for detecting templates
_RE_JAKE_MACROS = re.compile(r"\\resume(Subheading|ProjectHeading|Item|SubHeadingListStart|ItemListStart)\b")
_RE_CUSTOM_MACROS = re.compile(r"\\(experienceitem|projectitem)\b")
_RE_AWESOME_CV = re.compile(r"\\documentclass(\[[^\]]*\])?\{awesome-cv\}|\\begin\{cvitems\}|\\cvskills\b|\\cvskill\b")
_RE_MODERNCV = re.compile(r"\\documentclass(\[[^\]]*\])?\{moderncv\}|\\moderncvstyle|\\moderncvcolor")

# Detection for email and URL to exclude them from facts
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_URL = re.compile(r"(?:https?://|www\.)\S+|\b(?:[\w-]+\.)+(?:com|in|io|dev|app|org|net|me|co)(?:/\S*)?", re.IGNORECASE)


@dataclass(frozen=True)
class TemplateCapability:
    template_id: str             # custom_macros | jakes_resume | plain_itemize | awesome_cv | moderncv | unsupported
    template_name: str           # User-facing label
    compiles: bool               # TeX structure is syntactically coherent
    parses_facts: bool           # Bullets, skills, and sections can be extracted
    can_tailor: bool             # Engine can safely select and reorder entries and bullets
    reasons: tuple[str, ...]     # Plain language explanations
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)  # [{"line": int, "message": str, "hint": str}]
    doc: ResumeDoc | None = field(default=None, repr=False, compare=False)

    @property
    def template(self) -> str:
        return self.template_id

    def __iter__(self):
        return iter((self, self.doc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template": self.template_id,
            "template_name": self.template_name,
            "compiles": self.compiles,
            "parses_facts": self.parses_facts,
            "can_tailor": self.can_tailor,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


TemplateAnalysis = TemplateCapability


def detect_template(tex_content: str) -> str:
    """Identify which template contract a LaTeX resume conforms to."""
    if _RE_CUSTOM_MACROS.search(tex_content):
        return "custom_macros"
    if _RE_JAKE_MACROS.search(tex_content):
        return "jakes_resume"
    if _RE_AWESOME_CV.search(tex_content):
        return "awesome_cv"
    if _RE_MODERNCV.search(tex_content):
        return "moderncv"
    if "\\begin{itemize}" in tex_content and "\\item" in tex_content and "\\section" in tex_content:
        return "plain_itemize"
    return "unsupported"


def line_number_at_offset(source: str, offset: int) -> int:
    """Return 1-indexed line number corresponding to byte/char offset."""
    return source.count("\n", 0, offset) + 1


def clean_bullet_text(raw_bullet: str) -> str:
    """Clean LaTeX markup from bullet text and ensure bare URLs/emails are stripped."""
    # Strip comments (% until end of line, unless escaped)
    raw_no_comments = re.sub(r"(?<!\\)%.*$", "", raw_bullet, flags=re.MULTILINE)
    cleaned = plain(raw_no_comments)
    # Remove bare URLs and emails so they are never stored as factual claims
    cleaned = _RE_EMAIL.sub("", cleaned)
    cleaned = _RE_URL.sub("", cleaned)
    # Normalize leftover whitespace and punctuation
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def analyze_template(tex_content: str, *, filename: str = "resume.tex") -> tuple[TemplateCapability, ResumeDoc]:
    """Analyze a LaTeX resume, extract structure via ResumeDoc, and produce capability flags."""
    tid = detect_template(tex_content)
    warnings: list[dict[str, Any]] = []

    try:
        doc = parse(tex_content)
    except Exception as exc:
        msg = str(exc)
        warnings.append({
            "line": 1,
            "message": f"Syntax error in LaTeX: {msg}",
            "hint": "Check for unclosed braces or missing \\begin{document} / \\end{document}.",
        })
        empty_doc = ResumeDoc(preamble="", header="", summary="", education="", experience=(), projects=(), skills=(), additional="")
        cap = TemplateCapability(
            template_id="unsupported",
            template_name="Unparseable LaTeX",
            compiles=False,
            parses_facts=False,
            can_tailor=False,
            reasons=("The file could not be parsed into LaTeX sections due to a syntax error.",),
            warnings=tuple(warnings),
            doc=empty_doc,
        )
        return cap

    # Check unparsed items from doc and compute line numbers
    for unp in doc.unparsed:
        # Try to locate the snippet in tex_content
        snippet = unp.split(":")[1].strip() if ":" in unp else unp
        snippet_clean = snippet.strip("'\"")[:30]
        offset = tex_content.find(snippet_clean) if snippet_clean else -1
        line_num = line_number_at_offset(tex_content, offset) if offset >= 0 else 1
        hint = "Review formatting for this section."
        if "unknown section" in unp.lower():
            hint = "Supported sections: Summary, Education, Experience, Projects, Skills, Achievements, Additional."
        elif "skills line not recognised" in unp.lower():
            hint = "Format skills as '\\textbf{Category:} item1, item2 \\\\' or '\\item \\textbf{Category}{: item1, item2}'."
        elif "no itemize" in unp.lower() or "could not parse entries" in unp.lower():
            hint = "Format experience/project items with \\begin{itemize} and \\item bullets."

        warnings.append({
            "line": line_num,
            "message": unp,
            "hint": hint,
        })

    entries_count = len(doc.experience) + len(doc.projects)
    bullets_count = sum(len(e.bullets) for e in doc.experience) + sum(len(e.bullets) for e in doc.projects)
    skills_count = sum(len(s.items) for s in doc.skills)
    has_facts = (bullets_count > 0 or skills_count > 0 or bool(doc.education))

    reasons: list[str] = []
    compiles = True

    if tid == "custom_macros":
        name = "Custom Macros"
        can_tailor = True
        reasons.append("Native custom template: full bullet selection, reordering, and one-page fit supported.")
    elif tid == "jakes_resume":
        name = "Jake's Resume"
        can_tailor = True
        reasons.append("Jake's Resume detected: \\resumeSubheading and \\resumeProjectHeading macros supported.")
    elif tid == "plain_itemize":
        name = "Standard Itemize"
        can_tailor = True
        reasons.append("Standard LaTeX itemize resume: section entries and bullets reordered cleanly.")
    elif tid == "awesome_cv":
        name = "Awesome-CV"
        can_tailor = True
        reasons.append("Awesome-CV detected: \\cventry and \\cvskills supported for selection and tailoring.")
    elif tid == "moderncv":
        name = "Moderncv"
        can_tailor = False
        reasons.append("Moderncv detected: facts and cover letters only (tailoring disabled to prevent corrupted layout).")
    else:
        name = "Unsupported / Custom Format"
        can_tailor = False
        reasons.append("Template structure does not match a known modular pattern: facts and cover letters only (tailoring disabled).")

    if not has_facts:
        reasons.append("No experience, project bullets, or skills could be recognized from the text.")
        can_tailor = False

    parses_facts = has_facts

    cap = TemplateCapability(
        template_id=tid,
        template_name=name,
        compiles=compiles,
        parses_facts=parses_facts,
        can_tailor=can_tailor,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        doc=doc,
    )
    return cap


def extract_template_facts(name: str, doc: ResumeDoc) -> list[TexFact]:
    """Extract structured facts with stable IDs across any supported template.
    
    Invariants:
    - Bullet text is cleaned of LaTeX markup.
    - URLs and emails are NEVER stored as facts.
    - Stable hash IDs: identical cleaned text and kind always produces identical fact ID.
    """
    facts: list[TexFact] = []

    # 1. Header & Summary (contact details like emails and URLs are excluded)
    if doc.summary:
        cleaned_summary = clean_bullet_text(doc.summary)
        if cleaned_summary:
            facts.append(TexFact(fact_id("summary", cleaned_summary), "summary", cleaned_summary, doc.summary, (name,)))

    # 2. Education
    if doc.education:
        edu_lines = [l.strip() for l in doc.education.splitlines() if l.strip() and not l.strip().startswith("%")]
        for idx, el in enumerate(edu_lines):
            cleaned_edu = clean_bullet_text(el)
            if cleaned_edu and len(cleaned_edu) > 2:
                fid = fact_id("education", cleaned_edu)
                facts.append(TexFact(fid, "education", cleaned_edu, el, (name,), order=idx))

    # 3. Experience and Projects
    for kind, entries in (("experience", doc.experience), ("project", doc.projects)):
        for entry in entries:
            header_text = clean_bullet_text(" | ".join(part for part in (plain(arg) for arg in entry.args) if part))
            if not header_text:
                header_text = f"{kind.capitalize()} Entry"
            entry_key = fact_id(kind, header_text[:48])
            facts.append(TexFact(entry_key, kind, header_text, "|".join(entry.args), (name,), "", 0, ""))

            for position, raw_bullet in enumerate(entry.bullets):
                cleaned_b = clean_bullet_text(raw_bullet)
                if not cleaned_b:
                    continue
                fid = fact_id(f"{kind}_bullet", cleaned_b)
                facts.append(TexFact(fid, f"{kind}_bullet", cleaned_b, raw_bullet, (name,), entry=entry_key, order=position))

    # 4. Skills
    for line in doc.skills:
        group_text = clean_bullet_text(line.group)
        if not group_text:
            group_text = "General"
        g_fid = fact_id("skill_group", group_text)
        facts.append(TexFact(g_fid, "skill_group", group_text, line.group, (name,)))

        for position, raw_item in enumerate(line.items):
            cleaned_item = clean_bullet_text(raw_item)
            if not cleaned_item:
                continue
            fid = fact_id(f"skill|{group_text}", cleaned_item)
            facts.append(TexFact(fid, "skill", cleaned_item, raw_item, (name,), entry=group_text, order=position, group=group_text))

    # 5. Achievements
    if doc.achievements:
        for idx, line in enumerate(doc.achievements.splitlines()):
            cl = clean_bullet_text(line)
            if cl and len(cl) > 3:
                fid = fact_id("achievement", cl)
                facts.append(TexFact(fid, "achievement", cl, line, (name,), order=idx))

    return facts
