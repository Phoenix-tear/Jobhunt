"""Plain profile text of a LaTeX resume: section headings on their own line, `- ` bullets."""

from __future__ import annotations

from jobhunt.tailoring.texdoc import ResumeDoc, plain


def doc_to_text(doc: ResumeDoc) -> str:
    import re

    lines: list[str] = []
    headline = re.search(r"\\textcolor\{mediumgray\}\{([^}]*)\}", doc.header)
    name = re.search(r"\\textbf\{([^}]*)\}", doc.header)
    if name:
        lines.append(plain(name.group(1)))
    if headline:
        lines.append(plain(headline.group(1)))
    lines += ["SUMMARY", doc.summary and plain(doc.summary)]
    edu = doc.education.replace("\\\\", "\n").replace("\\hfill", " ")
    lines += ["EDUCATION", *[plain(part) for part in edu.splitlines() if plain(part)]]
    lines.append("PROFESSIONAL EXPERIENCE")
    for entry in doc.experience:
        lines.append(" ".join(part for part in (plain(entry.args[0]), plain(entry.args[1])) if part))
        lines += [f"- {plain(bullet)}" for bullet in entry.bullets]
    lines.append("PROJECTS")
    for entry in doc.projects:
        lines.append(f"{plain(entry.args[0])} | {plain(entry.args[1])}  {plain(entry.args[2])}")
        lines += [f"- {plain(bullet)}" for bullet in entry.bullets]
    lines.append("SKILLS")
    for skill in doc.skills:
        lines.append(f"{plain(skill.group)}: " + ", ".join(plain(item) for item in skill.items))
    if getattr(doc, "achievements", None):
        lines += ["ACHIEVEMENTS", plain(doc.achievements.replace("\\\\", " "))]
    lines += ["ADDITIONAL INFORMATION", plain(doc.additional.replace("\\\\", " "))]
    return "\n".join(line for line in lines if line)
