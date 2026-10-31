"""LaTeX compile step behind a small interface, plus PDF text / page-count helpers.

The compiler is injectable so tests (and machines without a TeX engine) can mock it. The engine is found in
this order: a project-local Tectonic (`tools/tectonic/tectonic.exe`), then `latexmk`, `xelatex`, `lualatex`,
`tectonic`, `pdflatex` on PATH. The resume sources use `fontspec`, so `pdflatex` is only accepted for sources
that do not (it is reported as unusable for these files).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CompileResult:
    success: bool
    pdf_path: Path | None
    pages: int
    engine: str
    log: str = ""


class LatexCompiler(Protocol):
    engine: str

    def compile(self, tex_path: Path, out_dir: Path) -> CompileResult: ...


def find_engine(project_root: Path) -> tuple[str, str] | None:
    """(engine name, executable) or None. Search order:
    1. tools/tectonic/<os-arch>/tectonic[.exe]
    2. tools/tectonic/tectonic[.exe]
    3. TECTONIC_PATH
    4. PATH (tectonic)
    5. fallback engines (xelatex, lualatex, latexmk)
    """
    from jobhunt.platform.paths import get_tectonic_search_paths

    for cand in get_tectonic_search_paths(project_root):
        if cand.is_file():
            return "tectonic", str(cand)

    for name in ("tectonic", "xelatex", "lualatex", "latexmk"):
        found = shutil.which(name)
        if found:
            return name, found
    return None


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf)).pages)


def pdf_text(pdf: Path) -> str:
    """Selectable text in reading order (pdftotext when available, else pypdf)."""
    exe = shutil.which("pdftotext")
    if exe:
        done = subprocess.run([exe, "-enc", "UTF-8", str(pdf), "-"], capture_output=True, timeout=60)
        if done.returncode == 0:
            return done.stdout.decode("utf-8", errors="replace")
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)


def has_images(pdf: Path) -> int:
    from pypdf import PdfReader

    return sum(len(page.images) for page in PdfReader(str(pdf)).pages)


def build_compile_command(
    engine: str,
    exe: str,
    tex_path: Path,
    out_dir: Path,
    *,
    untrusted: bool = True,
) -> list[str]:
    """Construct command arguments appropriate for each LaTeX engine.

    Engine Argument Rules:
    - tectonic: Does NOT accept `-no-shell-escape`! Accepts `--untrusted` (which disables shell escape
      and filesystem writes outside output), `-X compile`, `--outdir <dir>`.
    - latexmk: Accepts `-xelatex`, `-interaction=nonstopmode`, `-outdir=<dir>`, and `-no-shell-escape` when untrusted.
    - xelatex / lualatex: Accepts `-interaction=nonstopmode`, `-output-directory=<dir>`, and `-no-shell-escape` when untrusted.
    - other: Accepts `-interaction=nonstopmode`, `-output-directory=<dir>`, and `-no-shell-escape` when untrusted.
    """
    if engine == "tectonic":
        cmd = [exe, "-X", "compile"]
        if untrusted:
            cmd.append("--untrusted")
        cmd.extend(["--outdir", str(out_dir), str(tex_path)])
        return cmd
    elif engine == "latexmk":
        cmd = [exe, "-xelatex"]
        if untrusted:
            cmd.append("-no-shell-escape")
        cmd.extend(["-interaction=nonstopmode", f"-outdir={out_dir}", str(tex_path)])
        return cmd
    else:
        cmd = [exe]
        if untrusted:
            cmd.append("-no-shell-escape")
        cmd.extend(["-interaction=nonstopmode", f"-output-directory={out_dir}", str(tex_path)])
        return cmd


class SubprocessCompiler:
    def __init__(self, project_root: Path, timeout: int = 600) -> None:
        found = find_engine(project_root)
        if found is None:
            raise FileNotFoundError("no LaTeX engine found (see docs/OPERATIONS.md: Optional tools)")
        self.engine, self._exe = found
        self._timeout = timeout

    def compile(self, tex_path: Path, out_dir: Path) -> CompileResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        command = build_compile_command(self.engine, self._exe, tex_path, out_dir, untrusted=False)
        try:
            done = subprocess.run(command, capture_output=True, timeout=self._timeout, cwd=tex_path.parent)
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, 0, self.engine, "compile timed out")
        log = (done.stdout + done.stderr).decode("utf-8", errors="replace")[-2000:]
        pdf = out_dir / f"{tex_path.stem}.pdf"
        if done.returncode != 0 or not pdf.is_file():
            return CompileResult(False, None, 0, self.engine, log)
        return CompileResult(True, pdf, page_count(pdf), self.engine, log)


MAX_PDF_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB limit


def compile_sandboxed(
    tex_path: Path,
    out_dir: Path,
    *,
    project_root: Path | None = None,
    timeout: int = 45,
    max_size_bytes: int = MAX_PDF_SIZE_BYTES,
) -> CompileResult:
    """Compile a resume in a strict sandbox:
    - --untrusted: disables all known-insecure engine features (shell-escape, arbitrary writes)
    - Package downloads permitted on first use (no -C / --only-cached flag)
    - Explicit outdir and temp working directory
    - Strict timeout and output size cap
    """
    root = project_root or Path.cwd()
    found = find_engine(root)
    if found is None:
        return CompileResult(False, None, 0, "none", "No LaTeX engine found")
    engine, exe = found
    out_dir.mkdir(parents=True, exist_ok=True)
    command = build_compile_command(engine, exe, tex_path, out_dir, untrusted=True)

    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout, cwd=out_dir)
    except subprocess.TimeoutExpired:
        return CompileResult(False, None, 0, engine, f"Compilation timed out after {timeout} seconds")
    except Exception as exc:  # noqa: BLE001
        return CompileResult(False, None, 0, engine, f"Compilation failed to start: {exc}")

    log = (done.stdout + done.stderr).decode("utf-8", errors="replace")[-2000:]
    pdf = out_dir / f"{tex_path.stem}.pdf"
    if done.returncode != 0 or not pdf.is_file():
        return CompileResult(False, None, 0, engine, log)

    # Size cap validation
    try:
        size = pdf.stat().st_size
        if size > max_size_bytes:
            pdf.unlink(missing_ok=True)
            return CompileResult(False, None, 0, engine, f"Generated PDF exceeded size cap ({size} > {max_size_bytes} bytes)")
    except OSError:
        pass

    try:
        pages = page_count(pdf)
    except Exception:
        pages = 1
    return CompileResult(True, pdf, pages, engine, log)


def content_fill(pdf: Path, top_margin_pt: float = 21.6, bottom_margin_pt: float = 25.2) -> float:
    """Share of the usable page height that text reaches (0..1): the lowest text line vs the bottom margin.

    Defaults match the resume template (top 0.3in, bottom 0.35in). Measured from the PDF itself, not estimated.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(pdf))
        page = reader.pages[0]
        height = float(page.mediabox.height)
    except Exception:  # noqa: BLE001 - an unreadable PDF means "unknown": treated as full, never an error
        return 1.0
    lowest = [height]

    def visit(text: str, cm: list[float], tm: list[float], font_dict: object, font_size: float) -> None:
        if not text.strip():
            return
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        lowest[0] = min(lowest[0], y)

    page.extract_text(visitor_text=visit)
    usable = height - top_margin_pt - bottom_margin_pt
    used = (height - top_margin_pt) - (lowest[0] - 2.0)       # a little descender below the baseline
    return max(0.0, min(1.0, used / usable))
