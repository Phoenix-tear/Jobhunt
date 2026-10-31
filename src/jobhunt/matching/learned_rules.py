"""Rules I approved from the suggestions (`data/profile/learned_rules.yaml`, git-ignored with the rest of `data/`).

Safety limits, enforced on load as well as on write:
* a rule needs evidence from at least `MIN_SUPPORT` jobs;
* a rule can only demote a `target` to `maybe` and/or subtract score points (at most `MAX_PENALTY`): never exclude;
* nothing is added automatically: `add_rule` is called only from the Approve button;
* every rule carries its evidence and date, and can be removed with one call.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobhunt.classify import normalize_for_matching

LEARNED_FILE = "data/profile/learned_rules.yaml"
MIN_SUPPORT = 5
MAX_PENALTY = 20.0
KINDS = ("title_phrase", "eligibility_phrase")
_ID = re.compile(r"^L-\d{4}$")


@dataclass(frozen=True)
class LearnedRule:
    id: str
    kind: str                          # title_phrase | eligibility_phrase
    phrase: str
    demote: bool = True                # target -> maybe
    penalty: float = 0.0               # score points subtracted (0..MAX_PENALTY)
    evidence_count: int = 0
    job_ids: tuple[int, ...] = ()
    reason: str = ""
    created: str = ""
    source_key: str = ""
    _pattern: re.Pattern[str] | None = field(default=None, compare=False, repr=False)

    def matches(self, title: str, description: str | None) -> bool:
        text = normalize_for_matching(title) if self.kind == "title_phrase" else normalize_for_matching((description or "")[:8000])
        return bool(self.phrase) and f" {normalize_for_matching(self.phrase)} " in f" {text} "

    def tooltip(self) -> str:
        return (f"Learned rule {self.id}: '{self.phrase}' ({self.evidence_count} of your decisions, e.g. jobs "
                f"{', '.join(str(i) for i in self.job_ids[:5])}); approved {self.created}")


def _from_dict(data: dict[str, Any]) -> LearnedRule | None:
    try:
        evidence = data.get("evidence") or {}
        rule = LearnedRule(
            id=str(data["id"]), kind=str(data["kind"]), phrase=str(data["phrase"]).strip(),
            demote=bool(data.get("demote", True)), penalty=float(data.get("penalty", 0.0) or 0.0),
            evidence_count=int(evidence.get("count", 0)), job_ids=tuple(int(i) for i in evidence.get("job_ids", [])),
            reason=str(evidence.get("reason", "")), created=str(data.get("created", "")),
            source_key=str(data.get("source_key", "")))
    except (KeyError, TypeError, ValueError):
        return None
    if not _ID.match(rule.id) or rule.kind not in KINDS or len(rule.phrase) < 3 or rule.evidence_count < MIN_SUPPORT:
        return None
    if not (0.0 <= rule.penalty <= MAX_PENALTY) or not (rule.demote or rule.penalty > 0):
        return None
    return rule


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return []
    rules = data.get("rules") if isinstance(data, dict) else None
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def load_rules(path: Path) -> list[LearnedRule]:
    """Valid rules only; a broken or hand-edited entry that breaks a limit is ignored, never an error."""
    return [rule for rule in (_from_dict(item) for item in _read(path)) if rule is not None]


def signature(rules: list[LearnedRule]) -> str:
    """Goes into the gate's rules hash so approving or removing a rule recomputes the verdicts."""
    material = "|".join(f"{r.id}:{r.kind}:{r.phrase}:{int(r.demote)}:{r.penalty}" for r in sorted(rules, key=lambda r: r.id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12] if rules else ""


def _write(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Rules you approved from the suggestions. Each needs evidence from 5+ jobs; they can only demote to\n"
              "# `maybe` or subtract score points, never exclude. Remove one in the app (Health > Suggested rules) or delete its entry.\n")
    path.write_text(header + yaml.safe_dump({"version": 1, "rules": items}, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")


def add_rule(path: Path, *, kind: str, phrase: str, evidence_count: int, job_ids: list[int], reason: str, created: str,
             source_key: str, demote: bool = True, penalty: float = 0.0) -> LearnedRule:
    """Append an approved rule. Raises ValueError when it would break a safety limit."""
    existing = _read(path)
    next_number = 1 + max((int(str(r.get("id", "L-0000"))[2:] or 0) for r in existing if _ID.match(str(r.get("id", "")))),
                          default=0)
    item = {"id": f"L-{next_number:04d}", "kind": kind, "phrase": phrase.strip(), "demote": demote, "penalty": penalty,
            "evidence": {"count": evidence_count, "job_ids": [int(i) for i in job_ids][:50], "reason": reason},
            "created": created, "source_key": source_key}
    rule = _from_dict(item)
    if rule is None:
        raise ValueError(f"a learned rule needs {MIN_SUPPORT}+ supporting jobs, a known kind, and only demotes or "
                         f"penalises (penalty 0..{MAX_PENALTY:g})")
    if any(r.get("source_key") == source_key for r in existing):
        raise ValueError("this suggestion is already an active rule")
    _write(path, [*existing, item])
    return rule


def remove_rule(path: Path, rule_id: str) -> bool:
    items = _read(path)
    kept = [r for r in items if r.get("id") != rule_id]
    if len(kept) == len(items):
        return False
    _write(path, kept)
    return True
