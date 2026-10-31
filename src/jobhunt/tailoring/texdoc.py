"""Parse a resume `.tex` into a block model and render it back (same preamble, same macros, same layout).

The resume sources use two entry macros (`\\experienceitem{title}{dates}{extra}{body}` and
`\\projectitem{name}{stack}{dates}{body}`), `\\section{...}` headings, `itemize` bullets and `\\noindent\\textbf{Group:}`
skill lines. The parser never touches the preamble. Constructs it cannot round-trip are kept verbatim ("marker
mode") and listed in `ResumeDoc.unparsed`, so rendering can still reproduce the source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

_ITEMIZE_BEGIN = re.compile(r"\\begin\{itemize\}(\[[^\]]*\])?")
_SECTION = re.compile(r"\\section\{([^}]*)\}")
_COMMENT_LINE = re.compile(r"^\s*%.*$", re.MULTILINE)

LATEX_SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def escape_latex(text: str) -> str:
    """Escape a plain string for insertion into the document body."""
    return "".join(LATEX_SPECIALS.get(char, char) for char in text)


def read_group(source: str, index: int) -> tuple[str, int]:
    """The balanced `{...}` group starting at `index`; returns (inner text, index after the closing brace)."""
    if index >= len(source) or source[index] != "{":
        raise ValueError(f"expected '{{' at {index}")
    depth = 0
    start = index + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index], index + 1
        index += 1
    raise ValueError("unbalanced braces")


def split_top_level(text: str, separator: str = ",") -> list[str]:
    """Split on `separator` outside parentheses and braces."""
    parts: list[str] = []
    depth = 0
    buffer: list[str] = []
    skip = False
    for char in text:
        if skip:
            buffer.append(char)
            skip = False
            continue
        if char == "\\":
            buffer.append(char)
            skip = True
            continue
        if char in "({":
            depth += 1
        elif char in ")}":
            depth = max(0, depth - 1)
        if char == separator and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    if "".join(buffer).strip():
        parts.append("".join(buffer).strip())
    return parts


@dataclass(frozen=True)
class Entry:
    """One experience / project entry: macro arguments (raw LaTeX) and its bullets (raw LaTeX)."""

    macro: str                       # experienceitem | projectitem
    args: tuple[str, str, str]       # experience: (title, dates, extra)   project: (name, stack, dates)
    bullets: tuple[str, ...]
    itemize_options: str = "[leftmargin=12pt, itemsep=0pt, parsep=0pt, topsep=0pt]"
    key: str = ""                    # stable id of the entry (hash of name) filled by the facts layer

    @property
    def name(self) -> str:
        return self.args[0]


@dataclass(frozen=True)
class SkillLine:
    group: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class ResumeDoc:
    preamble: str                    # everything before \begin{document}, untouched
    header: str                      # the centered header block, verbatim
    summary: str
    education: str                   # verbatim
    experience: tuple[Entry, ...]
    projects: tuple[Entry, ...]
    skills: tuple[SkillLine, ...]
    additional: str                  # verbatim
    section_order: tuple[str, ...] = ()
    unparsed: tuple[str, ...] = field(default=())
    achievements: str = ""

    def with_(self, **changes: object) -> "ResumeDoc":
        return replace(self, **changes)  # type: ignore[arg-type]


_SECTION_KEYS = {
    "summary": "summary", "profile": "summary", "about me": "summary", "objective": "summary",
    "education": "education", "academic background": "education", "academics": "education",
    "professional experience": "experience", "experience": "experience", "work experience": "experience",
    "employment": "experience", "employment history": "experience", "work history": "experience",
    "projects": "projects", "personal projects": "projects", "academic projects": "projects", "key projects": "projects",
    "skills": "skills", "technical skills": "skills", "core competencies": "skills", "skills & tools": "skills",
    "achievements": "achievements", "honors": "achievements", "honors & awards": "achievements", "awards": "achievements",
    "additional information": "additional", "additional": "additional", "certifications": "additional",
    "leadership": "additional", "extracurricular": "additional", "involvement": "additional",
}


def parse(source: str) -> ResumeDoc:
    begin = source.index("\\begin{document}") if "\\begin{document}" in source else 0
    end = source.index("\\end{document}") if "\\end{document}" in source else len(source)
    preamble = source[:begin]
    body = source[begin + (len("\\begin{document}") if "\\begin{document}" in source else 0):end]
    first_section = _SECTION.search(body)
    header = body[: first_section.start()] if first_section else body
    matches = list(_SECTION.finditer(body))
    unparsed: list[str] = []
    chunks: dict[str, str] = {}
    order: list[str] = []
    for position, match in enumerate(matches):
        stop = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        raw_title = match.group(1).strip()
        key = _SECTION_KEYS.get(raw_title.lower())
        raw = body[match.end():stop]
        if key is None:
            unparsed.append(f"unknown section {raw_title!r} (expected Education, Experience, Projects, Skills, Achievements, Summary, or Additional)")
            key = f"raw:{raw_title}"
        chunks[key] = raw
        order.append(key)

    def clean(text: str) -> str:
        return _COMMENT_LINE.sub("", text).strip("\n")

    experience, projects = [], []
    if "experience" in chunks:
        experience = _entries(chunks["experience"], "experienceitem", unparsed)
    if "projects" in chunks:
        projects = _entries(chunks["projects"], "projectitem", unparsed)
    skills = _skills(chunks.get("skills", ""), unparsed)
    return ResumeDoc(
        preamble=preamble, header=header, summary=clean(chunks.get("summary", "")).strip(),
        education=clean(chunks.get("education", "")).strip("\n"), experience=tuple(experience),
        projects=tuple(projects), skills=tuple(skills),
        achievements=clean(chunks.get("achievements", "")).strip("\n"),
        additional=clean(chunks.get("additional", "")).strip("\n"),
        section_order=tuple(order), unparsed=tuple(unparsed),
    )


def _extract_bullets(body: str) -> list[str]:
    """Extract bullets from body containing \\resumeItem, \\item, cvitems, cvlistitem, or plain itemize."""
    bullets: list[str] = []
    # 1. Check for \resumeItem{...} macros
    if "\\resumeItem" in body:
        idx = 0
        while True:
            start = body.find("\\resumeItem", idx)
            if start < 0:
                break
            cursor = start + len("\\resumeItem")
            while cursor < len(body) and body[cursor] in " \n\t":
                cursor += 1
            if cursor < len(body) and body[cursor] == "{":
                try:
                    item_text, cursor = read_group(body, cursor)
                    bullets.append(item_text.strip())
                except ValueError:
                    break
            idx = cursor
        if bullets:
            return bullets

    # 2. Check for \cvlistitem{...}
    if "\\cvlistitem" in body:
        idx = 0
        while True:
            start = body.find("\\cvlistitem", idx)
            if start < 0:
                break
            cursor = start + len("\\cvlistitem")
            while cursor < len(body) and body[cursor] in " \n\t":
                cursor += 1
            if cursor < len(body) and body[cursor] == "{":
                try:
                    item_text, cursor = read_group(body, cursor)
                    bullets.append(item_text.strip())
                except ValueError:
                    break
            idx = cursor
        if bullets:
            return bullets

    # 3. Check for \begin{cvitems} ... \item ... \end{cvitems}
    if "\\begin{cvitems}" in body:
        start_idx = body.find("\\begin{cvitems}") + len("\\begin{cvitems}")
        end_idx = body.rfind("\\end{cvitems}")
        inner = body[start_idx:end_idx] if end_idx >= 0 else body[start_idx:]
        bullets = [part.strip() for part in re.split(r"\\item\b", inner)[1:] if part.strip()]
        if bullets:
            return bullets

    # 4. Check for \item in itemize
    begin = _ITEMIZE_BEGIN.search(body)
    if begin:
        end_idx = body.rfind("\\end{itemize}")
        inner = body[begin.end(): end_idx] if end_idx >= 0 else body[begin.end():]
        bullets = [part.strip() for part in re.split(r"\\item\b", inner)[1:] if part.strip()]
        if bullets:
            return bullets

    # 5. Check for any bare \item
    if "\\item" in body:
        bullets = [part.strip() for part in re.split(r"\\item\b", body)[1:] if part.strip()]

    return bullets


def _entries(raw: str, macro: str, unparsed: list[str]) -> list[Entry]:
    # Check if raw uses custom macros (\experienceitem, \projectitem)
    marker = f"\\{macro}"
    if marker in raw:
        entries: list[Entry] = []
        index = 0
        while True:
            start = raw.find(marker, index)
            if start < 0:
                break
            cursor = start + len(marker)
            args: list[str] = []
            try:
                for _ in range(4):
                    while cursor < len(raw) and raw[cursor] in " \n\t":
                        cursor += 1
                    group, cursor = read_group(raw, cursor)
                    args.append(group)
            except ValueError:
                unparsed.append(f"{macro} at offset {start} could not be parsed (unbalanced braces)")
                index = start + len(marker)
                continue
            body = args[3]
            bullets = _extract_bullets(body)
            options = "[leftmargin=12pt, itemsep=0pt, parsep=0pt, topsep=0pt]"
            begin = _ITEMIZE_BEGIN.search(body)
            if begin:
                options = begin.group(1) or options
            if not bullets:
                unparsed.append(f"{macro} {args[0][:40]!r} has no itemize bullets")
            entries.append(Entry(macro, (args[0], args[1], args[2]), tuple(bullets), options))
            index = cursor
        return entries

    # Check for \cventry (Awesome-CV with 5 args, or Moderncv with 6 args)
    if "\\cventry" in raw:
        entries = []
        index = 0
        cv_marker = "\\cventry"
        while True:
            start = raw.find(cv_marker, index)
            if start < 0:
                break
            cursor = start + len(cv_marker)
            args: list[str] = []
            try:
                # Try reading up to 6 braced arguments
                for _ in range(6):
                    while cursor < len(raw) and raw[cursor] in " \n\t":
                        cursor += 1
                    if cursor < len(raw) and raw[cursor] == "{":
                        group, cursor = read_group(raw, cursor)
                        args.append(group)
                    else:
                        break
            except ValueError:
                unparsed.append(f"\\cventry at offset {start} could not be parsed (unbalanced braces)")
                index = start + len(cv_marker)
                continue

            if len(args) == 5:
                # Awesome-CV: {role}{title}{location}{dates}{description}
                body = args[4]
                bullets = _extract_bullets(body)
                entries.append(Entry("awesome_cventry", (args[1], args[0], f"{args[2]}, {args[3]}"), tuple(bullets)))
            elif len(args) >= 6:
                # Moderncv: {dates}{job_title}{company}{location}{grade}{comment}
                body = args[5]
                bullets = _extract_bullets(body)
                if not bullets and body.strip():
                    bullets = [body.strip()]
                extra = f"{args[3]}, {args[4]}".strip(", ")
                entries.append(Entry("modern_cventry", (args[1], args[2], f"{args[0]} | {extra}".strip(" |")), tuple(bullets)))
            elif len(args) >= 3:
                # Generic cventry
                body = args[-1]
                bullets = _extract_bullets(body)
                entries.append(Entry("plain_entry", (args[0], args[1] if len(args) > 1 else "", ""), tuple(bullets)))
            index = cursor
        if entries:
            return entries

    # Check for Jake's resume macros (\resumeSubheading, \resumeProjectHeading)
    if "\\resumeSubheading" in raw:
        entries = []
        index = 0
        sub_marker = "\\resumeSubheading"
        while True:
            start = raw.find(sub_marker, index)
            if start < 0:
                break
            cursor = start + len(sub_marker)
            args: list[str] = []
            try:
                for _ in range(4):
                    while cursor < len(raw) and raw[cursor] in " \n\t":
                        cursor += 1
                    group, cursor = read_group(raw, cursor)
                    args.append(group)
            except ValueError:
                unparsed.append(f"\\resumeSubheading at offset {start} could not be parsed (unbalanced braces)")
                index = start + len(sub_marker)
                continue
            next_start = raw.find(sub_marker, cursor)
            body = raw[cursor:next_start] if next_start >= 0 else raw[cursor:]
            bullets = _extract_bullets(body)
            entries.append(Entry("resumeSubheading", (args[0], args[1], f"{args[2]}, {args[3]}"), tuple(bullets)))
            index = cursor
        return entries

    if "\\resumeProjectHeading" in raw:
        entries = []
        index = 0
        proj_marker = "\\resumeProjectHeading"
        while True:
            start = raw.find(proj_marker, index)
            if start < 0:
                break
            cursor = start + len(proj_marker)
            args: list[str] = []
            try:
                for _ in range(2):
                    while cursor < len(raw) and raw[cursor] in " \n\t":
                        cursor += 1
                    group, cursor = read_group(raw, cursor)
                    args.append(group)
            except ValueError:
                unparsed.append(f"\\resumeProjectHeading at offset {start} could not be parsed (unbalanced braces)")
                index = start + len(proj_marker)
                continue
            heading_raw = args[0]
            parts = re.split(r"\s*(?:\$\|\$|\|)\s*", heading_raw)
            name = parts[0].strip() if parts else heading_raw
            stack = parts[1].strip() if len(parts) > 1 else ""
            dates = args[1].strip()
            next_start = raw.find(proj_marker, cursor)
            body = raw[cursor:next_start] if next_start >= 0 else raw[cursor:]
            bullets = _extract_bullets(body)
            entries.append(Entry("resumeProjectHeading", (name, stack, dates), tuple(bullets)))
            index = cursor
        return entries

    # Check for plain itemize / heading entries
    if "\\begin{itemize}" in raw:
        entries = []
        pos = 0
        for match in _ITEMIZE_BEGIN.finditer(raw):
            start = match.start()
            header_text = raw[pos:start].strip()
            header_text = re.sub(r"\\resumeSubHeadingList(?:Start|End)", "", header_text).strip()
            end_idx = raw.find(r"\end{itemize}", match.end())
            if end_idx >= 0:
                inner = raw[match.end():end_idx]
                pos = end_idx + len(r"\end{itemize}")
            else:
                inner = raw[match.end():]
                pos = len(raw)
            bullets = [part.strip() for part in re.split(r"\\item\b", inner)[1:] if part.strip()]
            header_lines = [l.strip() for l in header_text.splitlines() if l.strip()]
            title = header_lines[0] if header_lines else "Experience"
            dates = header_lines[1] if len(header_lines) > 1 else ""
            entries.append(Entry("plain_entry", (title, dates, ""), tuple(bullets)))
        if entries:
            return entries

    # Fallback: if raw has content but no entries matched, warn with guidance
    cleaned_raw = _COMMENT_LINE.sub("", raw).strip()
    if cleaned_raw:
        unparsed.append(f"could not parse entries in section: {cleaned_raw[:50]!r} (expected \\resumeSubheading, \\{macro}, or \\begin{{itemize}})")
    return []


def _skills(raw: str, unparsed: list[str]) -> list[SkillLine]:
    lines: list[SkillLine] = []
    cleaned = _COMMENT_LINE.sub("", raw).replace("\\noindent", "").strip()
    cleaned = re.sub(r"\\(?:begin|end)\{itemize\}(?:\[[^\]]*\])?", "", cleaned)
    cleaned = re.sub(r"\\resumeSubHeadingList(?:Start|End)", "", cleaned)
    chunks = re.split(r"(?:\\\\\s*|\\item\s+)", cleaned)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or chunk in ("\\resumeItemListStart", "\\resumeItemListEnd"):
            continue
        chunk_clean = re.sub(r"^\\item\s*", "", chunk).strip()
        match = re.search(r"\\textbf\{([^}:]+):?\}\s*(?:\{:?|:\s*)?\s*(.*?)(?:\})?$", chunk_clean, flags=re.DOTALL)
        if match:
            group_name = match.group(1).strip()
            items_str = match.group(2).strip().rstrip("}").strip()
            items = tuple(split_top_level(items_str)) if items_str else ()
            if items:
                lines.append(SkillLine(group_name, items))
                continue
        parts = split_top_level(chunk_clean)
        if len(parts) >= 2:
            lines.append(SkillLine("General", tuple(parts)))
        else:
            unparsed.append(f"skills line not recognised: {chunk[:40]!r} (expected \\textbf{{Category:}} item1, item2)")
    return lines


# --- rendering -----------------------------------------------------------------------------------------------


def render(doc: ResumeDoc) -> str:
    """Rebuild the document: original preamble and header, sections in the original order."""
    parts = [doc.preamble.rstrip("\n"), "\\begin{document}", doc.header.rstrip("\n")]
    names = {
        "summary": "Summary", "education": "Education", "experience": "Professional Experience",
        "projects": "Projects", "skills": "Skills", "achievements": "Achievements", "additional": "Additional Information",
    }
    for key in doc.section_order or tuple(names):
        if key not in names:
            continue
        parts.append(f"\\section{{{names[key]}}}")
        if key == "summary":
            parts.append(doc.summary)
        elif key == "education":
            parts.append(doc.education)
        elif key == "experience":
            parts.append("\n".join(_render_entry(entry) for entry in doc.experience))
        elif key == "projects":
            parts.append("\n".join(_render_entry(entry) for entry in doc.projects))
        elif key == "skills":
            parts.append(_render_skills(doc.skills))
        elif key == "achievements":
            parts.append(doc.achievements)
        elif key == "additional":
            parts.append(doc.additional)
    parts.append("\\end{document}\n")
    return "\n".join(parts)


def _render_entry(entry: Entry) -> str:
    if entry.macro == "resumeSubheading":
        items = "\n".join(f"    \\resumeItem{{{bullet}}}" for bullet in entry.bullets)
        company = entry.args[2] if len(entry.args) > 2 else ""
        location = ""
        if "," in company:
            company, location = [x.strip() for x in company.split(",", 1)]
        return (
            f"  \\resumeSubheading\n"
            f"    {{{entry.args[0]}}}{{{entry.args[1]}}}\n"
            f"    {{{company}}}{{{location}}}\n"
            f"    \\resumeItemListStart\n{items}\n    \\resumeItemListEnd\n"
        )
    if entry.macro == "resumeProjectHeading":
        items = "\n".join(f"    \\resumeItem{{{bullet}}}" for bullet in entry.bullets)
        heading = f"\\textbf{{{entry.args[0]}}}"
        if len(entry.args) > 1 and entry.args[1]:
            heading += f" $|$ \\emph{{{entry.args[1]}}}"
        dates = entry.args[2] if len(entry.args) > 2 else ""
        return (
            f"  \\resumeProjectHeading\n"
            f"    {{{heading}}}{{{dates}}}\n"
            f"    \\resumeItemListStart\n{items}\n    \\resumeItemListEnd\n"
        )
    if entry.macro == "plain_entry":
        items = "\n".join(f"  \\item {bullet}" for bullet in entry.bullets)
        header = f"\\textbf{{{entry.args[0]}}}"
        if len(entry.args) > 1 and entry.args[1]:
            header += f" \\hfill {entry.args[1]}"
        return f"{header}\n\\begin{{itemize}}\n{items}\n\\end{{itemize}}\n"
    if entry.macro == "awesome_cventry":
        items = "\n".join(f"      \\item {bullet}" for bullet in entry.bullets)
        body = f"\\begin{{cvitems}}\n{items}\n    \\end{{cvitems}}"
        return (
            f"  \\cventry\n"
            f"    {{{entry.args[1]}}}\n"
            f"    {{{entry.args[0]}}}\n"
            f"    {{{entry.args[2]}}}\n"
            f"    {{}}\n"
            f"    {{{body}}}\n"
        )
    if entry.macro == "modern_cventry":
        items = "\n".join(f"  \\item {bullet}" for bullet in entry.bullets)
        body = f"\\begin{{itemize}}\n{items}\n\\end{{itemize}}" if entry.bullets else ""
        return (
            f"  \\cventry{{{entry.args[2]}}}{{{entry.args[0]}}}{{{entry.args[1]}}}{{}}{{}}{{{body}}}\n"
        )

    items = "\n".join(f"  \\item {bullet}" for bullet in entry.bullets)
    body = f"\n\\begin{{itemize}}{entry.itemize_options}\n{items}\n\\end{{itemize}}"
    args = "".join(f"{{{arg}}}" for arg in entry.args)
    return f"\\{entry.macro}{args}{{{body}}}\n"


def _render_skills(lines: tuple[SkillLine, ...]) -> str:
    rendered = [f"\\textbf{{{line.group}:}} {', '.join(line.items)}" for line in lines]
    if not rendered:
        return ""
    return "\\noindent" + "\\\\\n".join(rendered)


# --- plain text (for facts, diffs and the validator) -----------------------------------------------------------

_TEXT_SUBSTITUTIONS = (
    (re.compile(r"\\href\{[^}]*\}\{([^}]*)\}"), r"\1"),
    (re.compile(r"(?<!\\)~"), " "),                 # a bare ~ is a non-breaking space
    (re.compile(r"\\textasciitilde\{?\}?"), "~"),
    (re.compile(r"\\(?:textbf|textit|textcolor\{[^}]*\})\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\&"), "&"),
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\\$"), "$"),
    (re.compile(r"\\#"), "#"),
    (re.compile(r"\\_"), "_"),
    (re.compile(r"\\,"), " "),
    (re.compile(r"\\ "), " "),
    (re.compile(r"\\\\"), " "),
    (re.compile(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?"), ""),
    (re.compile(r"[{}]"), ""),
)


def plain(latex: str) -> str:
    """Readable text of a LaTeX fragment (links keep their label, escapes are undone)."""
    text = latex
    for pattern, replacement in _TEXT_SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    text = text.replace("\\'", "'")
    return re.sub(r"\s+", " ", text).strip()
