"""My voice: optional writing samples (`data/profile/writing_samples.md`, git-ignored) used as a style reference.

The file holds one to three of my own paragraphs or letters. Before anything is sent to a model, contact details are
removed (emails, phone numbers, URLs, handles, and my name), the same stripping the profile text goes through. The
prompt tells the model to match tone and sentence rhythm only: facts come from the cited ticked facts, never from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from jobhunt.matching.profiles import strip_pii

SAMPLES_FILE = "data/profile/writing_samples.md"
MAX_SAMPLE_CHARS = 3600
TEMPLATE = """\
<!--
  Your own writing, used ONLY as a style reference for cover letters and answers (tone and sentence rhythm).
  Paste one to three of your own paragraphs or letters below the line, separated by a line with three dashes (---).
  Nothing here is used as a source of facts: facts come from your ticked resume facts.
  Emails, phone numbers, links and your name are removed before anything is sent to a model.
  This file is git-ignored. Delete these comment lines or leave them: they are never sent.
-->

"""
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class VoiceSamples:
    text: str                                   # stripped, comment-free, capped; empty when there are no samples
    removed: dict[str, int] = field(default_factory=dict)
    present: bool = False


def ensure_template(path: Path) -> bool:
    """Create the template when the file is missing (never overwrites). True when it was created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return True


def clean_samples(raw: str, *, person_name: str | None = None) -> VoiceSamples:
    """Remove comments and contact details; cap the length. Pure function (no file access)."""
    body = _COMMENT.sub("", raw).strip()
    if not body:
        return VoiceSamples("", {}, False)
    # a name inside the samples is removed too (a signature line would otherwise leak it)
    stripped = strip_pii(body, person_name=person_name if person_name else "")
    text = re.sub(r"\n{3,}", "\n\n", stripped.text).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    if len(text) > MAX_SAMPLE_CHARS:
        text = text[:MAX_SAMPLE_CHARS].rsplit(" ", 1)[0]
    return VoiceSamples(text, dict(stripped.removed), bool(text))


def load_voice(path: Path, *, person_name: str | None = None) -> VoiceSamples:
    """The cleaned samples, creating the commented template on first use."""
    ensure_template(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return VoiceSamples()
    return clean_samples(raw, person_name=person_name)


__all__ = ["SAMPLES_FILE", "VoiceSamples", "clean_samples", "ensure_template", "load_voice"]
