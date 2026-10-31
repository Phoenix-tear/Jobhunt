"""Relevance-driven selection of projects and bullets (instead of "start full, trim, fill").

Everything here is deterministic and works on the resume document model; nothing is invented. A project is scored by
the share of the job's weighted concepts its own text covers (alias-aware, same concept table as the keyword logic),
plus a small bonus when it matches the job's domain. Projects are ordered by that score. A project is shown on merit
when its score reaches `tailoring.project_relevance_threshold`; a lower-scoring one is only brought in to fill the
page, and then keeps just its `low_relevance_bullets` best bullets. Spare space goes first to more bullets of the most
relevant entries (depth over breadth, at most `max_bullets_per_project`), and only then to another project.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobhunt.tailoring.keywords import concepts
from jobhunt.tailoring.texdoc import Entry, plain

DOMAIN_CONCEPTS = frozenset({"finance"})
START_BULLETS = 3                      # bullets a project on merit starts with, before the fill pass adds depth


@dataclass
class ProjectScore:
    name: str
    relevance: float                   # 0..1 share of the job's weighted top concepts, plus any domain bonus
    covers: list[str]                  # job concepts this project's text contains, most important first
    domain_bonus: float = 0.0
    on_merit: bool = False


@dataclass
class SelectionPlan:
    """What the selection decided, and why (reasons are shown in the version's change summary)."""

    scores: list[ProjectScore] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    job_concepts: list[str] = field(default_factory=list)       # the job's top concepts (for the "low relevance to ..." text)
    threshold: float = 0.0
    low_bullets: int = 2
    cap: int = 5

    def as_dict(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "job_concepts": self.job_concepts[:8],
                "projects": [{"name": s.name, "relevance": round(s.relevance, 3), "covers": s.covers[:8],
                              "domain_bonus": round(s.domain_bonus, 3), "on_merit": s.on_merit} for s in self.scores],
                "reasons": self.reasons}


@dataclass
class SelectionState:
    """Everything the fit pass needs to grow the page after the first selection."""

    plan: SelectionPlan
    weights: dict[str, float]
    title_concepts: set[str]
    pool: dict[str, list[str]]                 # project name -> every ticked bullet it could show (authored order first)
    order: list[str]                           # project names, most relevant first
    excluded: list[Entry]                      # projects below the threshold, most relevant first
    protected: Callable[[str], bool]
    boosts: dict[str, float] = field(default_factory=dict)
    relaxed: bool = False                      # low-relevance projects may go past their cap to reach the minimum fill


def short_name(name: str) -> str:
    return plain(name).split(" - ")[0].strip()


def entry_text(entry: Entry) -> str:
    return " ".join([plain(entry.args[0]), plain(entry.args[1]), *[plain(b) for b in entry.bullets]])


def concept_mass(text: str, weights: dict[str, float], top: int = 16) -> tuple[float, list[str]]:
    """(share of the top concepts' weight the text covers, the covered concepts best first)."""
    wanted = dict(list(weights.items())[:top])
    total = sum(wanted.values())
    if not total:
        return 0.0, []
    present = set(concepts(text))
    covered = [c for c in wanted if c in present]
    return sum(wanted[c] for c in covered) / total, covered


def bullet_relevance(raw: str, weights: dict[str, float], title_concepts: set[str],
                     boosts: dict[str, float] | None = None) -> float:
    present = set(concepts(plain(raw)))
    return (sum(weights.get(c, 0.0) for c in present) + 0.5 * len(present & title_concepts)
            + (boosts or {}).get(plain(raw), 0.0))


def score_projects(projects: Sequence[Entry], pool: dict[str, list[str]], weights: dict[str, float], *,
                   threshold: float, domain_bonus: float) -> list[ProjectScore]:
    """Score every project over everything it could show (its own bullets plus the ticked pool)."""
    domain_wanted = any(weights.get(c, 0.0) > 0 for c in DOMAIN_CONCEPTS)
    scores: list[ProjectScore] = []
    for entry in projects:
        name = plain(entry.args[0])
        text = " ".join([plain(entry.args[0]), plain(entry.args[1]), *[plain(b) for b in pool.get(name, entry.bullets)]])
        share, covered = concept_mass(text, weights)
        bonus = domain_bonus if domain_wanted and set(concepts(text)) & DOMAIN_CONCEPTS else 0.0
        scores.append(ProjectScore(name, share + bonus, covered, bonus, share + bonus >= threshold))
    return scores


def order_by_relevance(projects: Sequence[Entry], scores: dict[str, ProjectScore],
                       tie_break: Callable[[Entry], float]) -> list[Entry]:
    index = {id(p): i for i, p in enumerate(projects)}
    return sorted(projects, key=lambda p: (-round(scores[plain(p.args[0])].relevance, 6), -tie_break(p), index[id(p)]))


def pick_bullets(entry_name: str, candidates: Sequence[str], count: int, weights: dict[str, float],
                 title_concepts: set[str], protected: Callable[[str], bool],
                 boosts: dict[str, float] | None = None) -> list[str]:
    """The `count` best bullets by relevance (protected bullets always come along), kept in authored order, with a clearly
    better bullet moved to the top of the entry. Ties go to the authored order."""
    ranked = sorted(range(len(candidates)),
                    key=lambda i: (-bullet_relevance(candidates[i], weights, title_concepts, boosts), i))
    chosen = {i for i in ranked[:count]} | {i for i, raw in enumerate(candidates) if protected(raw)}
    order = [i for i in range(len(candidates)) if i in chosen]
    bullets = [candidates[i] for i in order]
    if len(bullets) >= 2:
        scores = [bullet_relevance(b, weights, title_concepts, boosts) for b in bullets]
        best = max(range(len(bullets)), key=lambda i: (scores[i], -i))
        if best != 0 and scores[best] >= max(2.0, 2 * scores[0]):
            bullets.insert(0, bullets.pop(best))
    _ = entry_name
    return bullets


def reason_for(score: ProjectScore, plan: SelectionPlan, shown: int, available: int, status: str) -> str:
    """One readable sentence about a project: why it is on the page the way it is."""
    percent = f"{100 * score.relevance:.1f}"
    if percent == f"{100 * plan.threshold:.1f}":
        percent = f"{100 * score.relevance:.2f}"
    asks = ", ".join(plan.job_concepts[:4]) or "the posting"
    covers = ", ".join(score.covers[:3]) or "none of its top concepts"
    if status == "kept" and score.relevance < plan.threshold:
        return (f"{score.name} kept as the page's anchor: no project reached {100 * plan.threshold:.0f}% relevance "
                f"(its own: {percent}%); {shown} of {available} bullets shown.")
    if status == "kept":
        return (f"{score.name} kept: relevance {percent}% (covers {covers}); {shown} of {available} bullets shown"
                + (" (domain match)" if score.domain_bonus else "") + ".")
    if status == "shortened":
        return (f"{score.name} shortened to {shown} bullet{'s' if shown != 1 else ''}: low relevance to {asks} "
                f"({percent}%, below {100 * plan.threshold:.1f}%); shown to fill the page.")
    return f"{score.name} dropped: relevance {percent}% is below {100 * plan.threshold:.1f}% and the page was full without it."


__all__ = ["DOMAIN_CONCEPTS", "ProjectScore", "SelectionPlan", "SelectionState", "START_BULLETS", "bullet_relevance", "concept_mass",
           "entry_text", "order_by_relevance", "pick_bullets", "reason_for", "score_projects", "short_name"]
