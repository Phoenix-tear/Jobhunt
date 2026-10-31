"""Resume validation, upload security, and parsing diagnostics (Brief v12).

Upload security:
- 2 MB size cap
- Sanitized filenames under data/resumes/
- Extension and content sniffing (.tex with LaTeX syntax)
- Sandboxed Tectonic compile (timeout, untrusted, offline)
- Diagnostics reporting recognized sections and actionable guidance for unparsed lines.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from jobhunt.errors import JobHuntError
from jobhunt.tailoring.latex import CompileResult, compile_sandboxed
from jobhunt.tailoring.texdoc import parse

MAX_UPLOAD_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB

_SAFE_FILENAME = re.compile(r"^[a-zA-Z0-9_\-.]+$")
_TEX_INDICATORS = (
    r"\documentclass",
    r"\begin{document}",
    r"\section",
    r"\resume",
    r"\experienceitem",
    r"\projectitem",
    r"\textbf",
    r"\textit",
)


class ResumeUploadError(JobHuntError):
    """Uploaded resume file failed validation."""


def sanitize_filename(filename: str, default_stem: str = "resume_custom") -> str:
    """Sanitize filename to prevent path traversal and restrict characters."""
    clean_name = Path(filename).name
    if not clean_name.lower().endswith(".tex"):
        clean_name = f"{clean_name}.tex"
    stem = Path(clean_name).stem
    sanitized_stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem).strip("._-")
    if not sanitized_stem:
        sanitized_stem = default_stem
    return f"{sanitized_stem}.tex"


def sniff_tex_content(file_bytes: bytes) -> str:
    """Validate that bytes represent legitimate LaTeX text."""
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = file_bytes.decode("latin-1")
        except Exception as exc:
            raise ResumeUploadError("File is not valid text (expected UTF-8 LaTeX source)") from exc

    # Check for null bytes (binary content)
    if "\x00" in text:
        raise ResumeUploadError("Binary content detected; only plain .tex files are accepted")

    # Check for TeX indicators
    lower = text.lower()
    if not any(indicator.lower() in lower for indicator in _TEX_INDICATORS):
        raise ResumeUploadError("File does not contain valid LaTeX commands or structure")

    return text


def validate_and_save_upload(
    file_bytes: bytes,
    original_filename: str,
    resume_dir: Path,
) -> tuple[Path, str]:
    """Validate upload size, content, and filename, then persist to data/resumes/."""
    if len(file_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise ResumeUploadError(f"File size ({len(file_bytes)} bytes) exceeds the 2 MB limit")

    if not original_filename.lower().endswith(".tex"):
        raise ResumeUploadError("Only .tex files are accepted")

    text = sniff_tex_content(file_bytes)
    resume_dir = Path(resume_dir)
    resume_dir.mkdir(parents=True, exist_ok=True)

    filename = sanitize_filename(original_filename)
    dest_path = resume_dir / filename
    dest_path.write_text(text, encoding="utf-8")
    return dest_path, text


def validate_resume_text(
    tex_text: str,
    *,
    tex_path: Path | None = None,
    project_root: Path | None = None,
    run_compile: bool = True,
) -> dict[str, Any]:
    """Parse resume LaTeX and optionally run sandboxed Tectonic compilation."""
    from jobhunt.resumes.templates import analyze_template, extract_template_facts

    cap, doc = analyze_template(tex_text)
    if not cap.compiles:
        return {
            "valid": False,
            "error": "Failed to parse LaTeX structure",
            "template": cap.as_dict(),
            "can_tailor": False,
            "parses_facts": False,
            "facts_count": 0,
            "sections_recognized": {},
            "unparsed": [
                {"message": w["message"], "guidance": w["hint"], "line": w.get("line", 1)}
                for w in cap.warnings
            ],
            "compile": {"success": False, "pages": 0, "engine": "none", "log": cap.warnings[0]["message"] if cap.warnings else ""},
        }

    sections = {
        "education": bool(doc.education),
        "experience": len(doc.experience),
        "projects": len(doc.projects),
        "skills": len(doc.skills),
        "achievements": bool(getattr(doc, "achievements", None)),
        "summary": bool(doc.summary),
        "additional": bool(doc.additional),
    }

    unparsed_items = [
        {"message": w["message"], "guidance": w["hint"], "line": w.get("line", 1)}
        for w in cap.warnings
    ]

    compile_info: dict[str, Any] = {"success": True, "pages": 1, "engine": "none", "log": ""}
    if run_compile:
        with tempfile.TemporaryDirectory(prefix="res_val_") as tmpdir:
            temp_out = Path(tmpdir)
            if tex_path and tex_path.is_file():
                compile_target = tex_path
            else:
                compile_target = temp_out / "resume_validate.tex"
                compile_target.write_text(tex_text, encoding="utf-8")
            res: CompileResult = compile_sandboxed(compile_target, temp_out, project_root=project_root)
            compile_info = {
                "success": res.success,
                "pages": res.pages,
                "engine": res.engine,
                "log": res.log,
            }

    facts = extract_template_facts("custom", doc) if cap.parses_facts else []
    valid = bool(doc.experience or doc.projects or doc.skills or doc.education)

    return {
        "valid": valid,
        "template": cap.as_dict(),
        "can_tailor": cap.can_tailor,
        "parses_facts": cap.parses_facts,
        "facts_count": len(facts),
        "sections_recognized": sections,
        "unparsed": unparsed_items,
        "compile": compile_info,
    }
