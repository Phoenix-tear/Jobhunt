"""Helpers shared by the aggregator / feed adapters."""

from __future__ import annotations

import re
from typing import Any

from jobhunt.models import EmploymentType, ExperienceLevel
from jobhunt.normalize import clean_text


def employment_from_label(label: Any) -> EmploymentType:
    text = (clean_text(label) or "").lower().replace("_", " ").replace("-", " ")
    if not text:
        return EmploymentType.UNKNOWN
    if "intern" in text or "praktikum" in text:
        return EmploymentType.INTERNSHIP
    if "part" in text:
        return EmploymentType.PART_TIME
    if any(word in text for word in ("contract", "freelance", "temporary", "temp")):
        return EmploymentType.CONTRACT
    if "full" in text or "permanent" in text:
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN


def level_from_labels(*labels: Any) -> ExperienceLevel:
    """Map seniority words from any number of source labels to our enum (first decisive word wins)."""
    text = " ".join((clean_text(label) or "") for label in labels).lower().replace("-", " ")
    if not text.strip():
        return ExperienceLevel.UNKNOWN
    if re.search(r"\b(intern|internship|trainee)\b", text):
        return ExperienceLevel.INTERN
    if re.search(r"\b(senior|sr|lead|principal|staff|manager|director|head|executive|expert)\b", text):
        return ExperienceLevel.SENIOR
    if re.search(r"\b(mid|midweight|intermediate|mid level|middle)\b", text):
        return ExperienceLevel.MID
    if re.search(r"\b(entry|junior|jr|graduate|associate|beginner)\b", text):
        return ExperienceLevel.ENTRY
    return ExperienceLevel.UNKNOWN


def comma_join(values: Any, separator: str = "; ") -> str | None:
    """['US', ' ', 'Canada'] -> 'US; Canada'; non-list input is treated as one string."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return None
    parts = [clean_text(value) for value in values]
    return separator.join(part for part in dict.fromkeys(parts) if part) or None
