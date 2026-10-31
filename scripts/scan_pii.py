"""Secret and PII Scanner for tracked repository files (Brief v14 Section 5 & Cleanup Brief).

Scans repository files for:
1. Candidate/personal identifiers loaded dynamically at run time from git-ignored profile files.
2. Generic patterns (API keys, private keys, personal email addresses, GitHub tokens).

The script source contains zero candidate names, institutions, or personal markers.
Prints term counts per source file and per category (name, university, city, event names, handles/project identifiers).
Never prints secret or personal values in findings or logs (path + line + kind only).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Generic patterns that do not depend on personal profiles
GENERIC_PATTERNS = [
    (re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"), "Google / Gemini API Key"),
    (re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"), "Private Key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b"), "GitHub Token"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "sk- style API Key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack Token"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
]

# Historical evaluation reports documenting past migrations
ALLOWED_PATHS = {
    "data/eval/v10b_report.md",
    "data/eval/v11_report.md",
    "data/eval/v12_report.md",
    "data/eval/v12b_report.md",
    "data/eval/v13_report.md",
    "data/eval/v13b_report.md",
    "data/eval/v14_report.md",
}

CATEGORIES = ("name", "university", "city", "event_names", "handles_projects", "contact")

# Generic words and phrases that must never become personal terms (placeholder profiles, institution
# boilerplate, job-title words). Derived terms need at least one token that is NOT in this set.
GENERIC_WORDS = frozenset(
    """
    a an and any as at by for from in is it of on or the to with without
    university college institute institution school academy faculty department campus
    science sciences technology technologies engineering computer computing software hardware
    information systems data electronics electrical mechanical civil
    student candidate engineer developer intern fresher graduate undergraduate bachelor master masters
    example sample test demo placeholder default unknown none null name your my
    india remote
    """.split()
)
GENERIC_PHRASES = frozenset(
    {
        "the candidate", "candidate", "student", "university", "college", "institute of science and technology",
        "institute of technology", "example institute of science and technology", "example university",
    }
)
PLACEHOLDER_NAMES = frozenset({"the candidate", "candidate", "student", "your name", "full name", "name", "example", "alex chen"})
MIN_TOKEN_LENGTH = 4
GENERIC_OS_USERNAMES = frozenset({"user", "users", "admin", "administrator", "runner", "root", "default", "public", "guest", "vagrant", "ubuntu"})

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def is_specific_term(term: str) -> bool:
    """A derived term is specific enough to flag: not a stop phrase and has a non-generic word."""
    cleaned = " ".join(term.lower().split())
    if len(cleaned) < 3 or cleaned in GENERIC_PHRASES:
        return False
    if cleaned.isdigit() and len(cleaned) >= 8:  # phone-like number
        return True
    words = _WORD_RE.findall(cleaned)
    return any(word not in GENERIC_WORDS and not word.isdigit() for word in words)


def _name_tokens(name: str) -> set[str]:
    return {
        token
        for token in _WORD_RE.findall(name)
        if len(token) >= MIN_TOKEN_LENGTH and token.lower() not in GENERIC_WORDS and not token.isdigit()
    }


def _add_derived(terms_by_cat: dict[str, set[str]], category: str, term: str) -> int:
    """Add `term` when it is specific enough and new; returns 1 if added."""
    term = term.strip()
    if not is_specific_term(term) or term in terms_by_cat[category]:
        return 0
    terms_by_cat[category].add(term)
    return 1


def _add_name(terms_by_cat: dict[str, set[str]], name: str) -> int:
    if name.strip().lower() in PLACEHOLDER_NAMES:
        return 0
    count = _add_derived(terms_by_cat, "name", name)
    if count or name.strip() in terms_by_cat["name"]:
        for token in _name_tokens(name):
            count += _add_derived(terms_by_cat, "name", token)
    return count


def _add_email(terms_by_cat: dict[str, set[str]], email: str) -> int:
    """Whole address and its local part (noreply addresses are public by design and skipped)."""
    email = email.strip()
    if "@" not in email or "noreply" in email.lower():
        return 0
    count = _add_derived(terms_by_cat, "contact", email)
    local = email.split("@", 1)[0]
    if len(local) >= MIN_TOKEN_LENGTH:
        count += _add_derived(terms_by_cat, "contact", local)
    return count


def _add_phone(terms_by_cat: dict[str, set[str]], phone: str) -> int:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 8:
        return 0
    # A tolerant pattern would be needed for separators; the plain digits and the original text both count.
    return _add_derived(terms_by_cat, "contact", phone.strip()) + _add_derived(terms_by_cat, "contact", digits)


def _walk_contact_fields(data: Any, terms_by_cat: dict[str, set[str]]) -> int:
    """Pick e-mail / phone / handle-looking fields out of a loaded YAML mapping, by key name."""
    count = 0
    if isinstance(data, dict):
        for key, value in data.items():
            key_l = str(key).lower()
            if isinstance(value, str):
                if "email" in key_l or "e_mail" in key_l or _EMAIL_RE.fullmatch(value.strip() or " "):
                    count += _add_email(terms_by_cat, value)
                elif key_l in {"phone", "mobile", "phone_number", "telephone"} or key_l.endswith("_phone"):
                    count += _add_phone(terms_by_cat, value)
                elif key_l in {"github", "github_handle", "github_username", "linkedin", "linkedin_url", "handle", "username"}:
                    handle = value.strip().rstrip("/").rsplit("/", 1)[-1]
                    count += _add_derived(terms_by_cat, "handles_projects", handle)
            else:
                count += _walk_contact_fields(value, terms_by_cat)
    elif isinstance(data, list):
        for item in data:
            count += _walk_contact_fields(item, terms_by_cat)
    return count


def _runtime_identity_terms(terms_by_cat: dict[str, set[str]]) -> int:
    """Terms read from the machine that runs the scan (never stored in the repo): git e-mail, OS login name."""
    count = 0
    for key in ("user.email",):
        try:
            result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        count += _add_email(terms_by_cat, result.stdout.strip())
    login = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(login) >= MIN_TOKEN_LENGTH and login.lower() not in GENERIC_OS_USERNAMES:
        count += _add_derived(terms_by_cat, "handles_projects", login)
    return count


def load_profile_terms(
    project_root: Path,
    terms_dir: Path | None = None,
    include_runtime: bool = False,
) -> tuple[dict[str, int], dict[str, set[str]]]:
    """Load personal terms categorized by source and category.

    With `terms_dir` ONLY that directory is searched (so a test or an export scan never falls back to the
    checkout's own data/); without it the project root is searched.
    `include_runtime` adds terms read from the running machine (git e-mail, OS login name).

    Returns (source_counts, terms_by_category).
    """
    search_dirs: list[Path] = [Path(terms_dir).resolve()] if terms_dir else [Path(project_root).resolve()]

    source_counts: dict[str, int] = {}
    terms_by_cat: dict[str, set[str]] = {cat: set() for cat in CATEGORIES}

    def _resolve_file(relative_name: str) -> Path | None:
        for d in search_dirs:
            for cand in [
                d / "data" / "profile" / relative_name,
                d / "profile" / relative_name,
                d / relative_name,
            ]:
                if cand.is_file():
                    return cand
        return None

    def _load_yaml(path: Path) -> Any:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # 1. pii_terms.txt (explicit lines are trusted as written)
    pii_terms_file = _resolve_file("pii_terms.txt")
    if pii_terms_file:
        count = 0
        current_cat = "name"
        try:
            for line in pii_terms_file.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw:
                    continue
                if raw.startswith("#"):
                    m = re.match(r"^#\s*\[\s*([a-zA-Z_-]+)\s*\]", raw)
                    if m:
                        cat_cand = m.group(1).lower().replace("-", "_")
                        if cat_cand in terms_by_cat:
                            current_cat = cat_cand
                    continue
                if len(raw) >= 3:
                    terms_by_cat[current_cat].add(raw)
                    count += 1
                    if current_cat == "name":
                        for token in _name_tokens(raw):
                            if token not in terms_by_cat["name"]:
                                terms_by_cat["name"].add(token)
                                count += 1
            source_counts["pii_terms.txt"] = count
        except OSError:
            pass

    # 2. profile.yaml (person block + any contact-looking field anywhere)
    profile_file = _resolve_file("profile.yaml")
    if profile_file:
        count = 0
        try:
            data = _load_yaml(profile_file)
            person = data.get("person", {}) if isinstance(data, dict) else {}
            if isinstance(person, dict):
                for key in ("display_name", "name", "full_name"):
                    val = person.get(key)
                    if isinstance(val, str):
                        count += _add_name(terms_by_cat, val)
                for key in ("institution", "university"):
                    val = person.get(key)
                    if isinstance(val, str):
                        count += _add_derived(terms_by_cat, "university", val)
                val_city = person.get("city")
                if isinstance(val_city, str):
                    count += _add_derived(terms_by_cat, "city", val_city)
            count += _walk_contact_fields(data, terms_by_cat)
            source_counts["profile.yaml"] = count
        except Exception:
            pass

    # 3. application_answers.yaml (contact fields, university)
    answers_file = _resolve_file("application_answers.yaml")
    if answers_file:
        count = 0
        try:
            data = _load_yaml(answers_file)
            if isinstance(data, dict):
                val = data.get("university")
                if isinstance(val, str):
                    count += _add_derived(terms_by_cat, "university", val)
                count += _walk_contact_fields(data, terms_by_cat)
            source_counts["application_answers.yaml"] = count
        except Exception:
            pass

    # 4. terms.yaml
    terms_file = _resolve_file("terms.yaml")
    if terms_file:
        count = 0
        try:
            data = _load_yaml(terms_file)
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(val, str) and len(val.strip()) >= 3:
                        cat = "event_names" if any(x in str(key).lower() for x in ("event", "fest", "campus")) else "handles_projects"
                        count += _add_derived(terms_by_cat, cat, val)
                        for part in re.split(r"[\s_]+", val.strip()):
                            if len(part) >= 3 and not part.lower().startswith("team"):
                                count += _add_derived(terms_by_cat, cat, part)
            source_counts["terms.yaml"] = count
        except Exception:
            pass

    # 5. project_markers.txt (explicit lines are trusted)
    pm_file = _resolve_file("project_markers.txt")
    if pm_file:
        count = 0
        try:
            for line in pm_file.read_text(encoding="utf-8").splitlines():
                t = line.strip()
                if t and not t.startswith("#") and len(t) >= 3:
                    terms_by_cat["handles_projects"].add(t)
                    count += 1
            source_counts["project_markers.txt"] = count
        except OSError:
            pass

    # 6. config.local.yaml (contact used in the HTTP user agent)
    for d in search_dirs:
        local_cfg = d / "config.local.yaml"
        if local_cfg.is_file():
            count = 0
            try:
                data = _load_yaml(local_cfg)
                if isinstance(data, dict):
                    count += _add_email(terms_by_cat, str(data.get("user_agent_contact", "")))
                    count += _walk_contact_fields(data, terms_by_cat)
                source_counts["config.local.yaml"] = count
            except Exception:
                pass
            break

    # 7. the running machine (git e-mail, OS login name): opt-in, never part of library/test defaults
    if include_runtime:
        source_counts["runtime (git email, OS login)"] = _runtime_identity_terms(terms_by_cat)

    return source_counts, terms_by_cat


def _compile_term(term: str) -> re.Pattern[str]:
    """Word-bounded, case-insensitive; digits-only terms tolerate spaces / dashes between digits."""
    if term.isdigit() and len(term) >= 8:
        body = r"[\s.\-()]*".join(re.escape(ch) for ch in term)
        return re.compile(body)
    if "@" in term:
        return re.compile(re.escape(term), re.IGNORECASE)
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


def load_profile_patterns(
    project_root: Path,
    terms_dir: Path | None = None,
    print_summary: bool = False,
    include_runtime: bool = False,
) -> list[tuple[re.Pattern[str], str]]:
    """Load dynamic personal terms from git-ignored profile files at run time."""
    source_counts, terms_by_cat = load_profile_terms(project_root, terms_dir, include_runtime=include_runtime)

    all_terms: set[str] = set()
    for s in terms_by_cat.values():
        all_terms.update(s)

    if print_summary:
        print("Terms loaded by source file:")
        if source_counts:
            for src, cnt in source_counts.items():
                print(f"  - {src:<30}: {cnt} terms")
        else:
            print("  (none found)")

        print("Terms loaded by category:")
        for cat in CATEGORIES:
            print(f"  - {cat:<20}: {len(terms_by_cat[cat])} unique terms")
        print(f"Total unique terms: {len(all_terms)} across {len(CATEGORIES)} categories.\n")

    if not all_terms:
        if print_summary:
            print("[INFO] No dynamic personal profile terms loaded: running generic patterns only.")
        return []

    patterns: list[tuple[re.Pattern[str], str]] = []
    # Sort terms by length descending to match longer multi-word phrases first
    for term in sorted(all_terms, key=len, reverse=True):
        matched_cat = next((cat for cat, c_set in terms_by_cat.items() if term in c_set), "unknown")
        patterns.append((_compile_term(term), f"Profile personal term ({matched_cat})"))

    return patterns


def count_loaded_terms(
    project_root: Path | str,
    terms_dir: Path | str | None = None,
    include_runtime: bool = False,
) -> int:
    """Number of unique personal terms that would be loaded (used by the export gate: must be > 0)."""
    _, terms_by_cat = load_profile_terms(Path(project_root), Path(terms_dir) if terms_dir else None, include_runtime=include_runtime)
    return len({t for terms in terms_by_cat.values() for t in terms})


def scan_repo(
    project_root: Path | str,
    terms_dir: Path | str | None = None,
    print_summary: bool = False,
    include_runtime: bool = False,
) -> list[tuple[str, int, str, str]]:
    """Return list of findings: (filepath, line_no, rule_name, matched_text)."""
    project_root = Path(project_root).resolve()
    terms_dir = Path(terms_dir).resolve() if terms_dir else None
    findings: list[tuple[str, int, str, str]] = []
    patterns = list(GENERIC_PATTERNS) + load_profile_patterns(
        project_root, terms_dir, print_summary=print_summary, include_runtime=include_runtime
    )

    tracked_files: list[str] = []
    try:
        res = subprocess.run(["git", "ls-files"], cwd=str(project_root), capture_output=True, text=True, check=True)
        tracked_files = [f.strip() for f in res.stdout.splitlines() if f.strip()]
    except Exception:
        # Fallback to directory walk if not a git repository
        for root, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules", "data", "__pycache__"}]
            for file_name in files:
                p = Path(root) / file_name
                tracked_files.append(str(p.relative_to(project_root)))

    for rel_path in tracked_files:
        norm_path = rel_path.replace("\\", "/")
        if norm_path in ALLOWED_PATHS:
            continue

        file_path = project_root / rel_path
        if not file_path.is_file():
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line_idx, line in enumerate(content.splitlines(), start=1):
            for regex, rule_name in patterns:
                match = regex.search(line)
                if match:
                    findings.append((norm_path, line_idx, rule_name, match.group(0)))

    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan repository for secrets and personal PII.")
    parser.add_argument(
        "--terms-from",
        dest="terms_from",
        type=Path,
        default=None,
        help="Directory containing profile files (pii_terms.txt, profile.yaml, etc.)",
    )
    parser.add_argument(
        "--path",
        dest="scan_path",
        type=Path,
        default=None,
        help="Repository or directory to scan (defaults to repository root)",
    )
    args = parser.parse_args()

    repo_root = (args.scan_path or Path(__file__).resolve().parent.parent).resolve()
    terms_dir = args.terms_from.resolve() if args.terms_from else None

    # An explicit --terms-from means "scan someone else's tree with my identity": include runtime identity terms.
    findings = scan_repo(repo_root, terms_dir=terms_dir, print_summary=True, include_runtime=terms_dir is not None)

    if not findings:
        print("[OK] Secret/PII Scanner passed: 0 findings across tracked files.")
        sys.exit(0)

    print(f"[FAIL] Secret/PII Scanner detected {len(findings)} finding(s):")
    # Strictly adhere to safety: print path + line + kind ONLY; never print secret or personal text
    for path, line, rule, _ in findings:
        print(f"  - {path}:{line} [{rule}]")
    sys.exit(1)


if __name__ == "__main__":
    main()
