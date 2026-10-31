"""Compose a tailored resume from a base: ordering, stale-content repair, trim-to-fit, fill, validation.

Start full, then trim: the document begins as the complete base (ordered by deterministic, alias-aware relevance
with minimal reshuffling); only if it overflows one page are the least relevant items dropped, one at a time;
if it leaves more than a few percent of the page blank, the best dropped (or other-resume) items are put back
while it still fits. Everything here is deterministic. No model writes resume text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Callable

from jobhunt.config import TailoringRecipe
from jobhunt.profile_terms import EVENT_NAME
from jobhunt.tailoring.keywords import concepts, relevance_score
from jobhunt.tailoring.plan import Validation, numbers_in
from jobhunt.tailoring.texdoc import Entry, ResumeDoc, SkillLine, escape_latex, plain, split_top_level

_EDITION = re.compile(re.escape(EVENT_NAME) + r"\s*'\s*(\d{2})", re.IGNORECASE)
_UPTIME = re.compile(r"(\d+(?:\.\d+)?)\s*\\?%\s*uptime|uptime of (\d+(?:\.\d+)?)\s*\\?%", re.IGNORECASE)
_METRIC = re.compile(r"\d[\d,.]*\s?(?:k|m|%)\+?|\d[\d,]{2,}\+?")
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_STOP = frozenset("the a an of and for to in on with by from as at is are project platform student portal".split())


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# --- stale content (resolved conflicts) ------------------------------------------------------------------------------


@dataclass
class Repairs:
    notes: list[str] = field(default_factory=list)


def repair_against_authoritative(doc: ResumeDoc, authoritative: ResumeDoc, repairs: Repairs) -> ResumeDoc:
    """Apply resolved conflicts to a non-authoritative base: role, edition, project stack header, reworded bullets,
    and remove projects that duplicate something the authoritative resume says once."""
    # roles: same organisation, different role text -> the authoritative header
    experience = list(doc.experience)
    auth_by_org = {_org_key(e): e for e in authoritative.experience}
    for index, entry in enumerate(experience):
        match = auth_by_org.get(_org_key(entry))
        if match is not None and plain(match.args[0]) != plain(entry.args[0]):
            repairs.notes.append(f"role/header: '{plain(entry.args[0])}' -> '{plain(match.args[0])}'")
            experience[index] = replace(entry, args=(match.args[0], entry.args[1], entry.args[2]))
    editions = {m.group(1) for text in _all_text(authoritative) for m in _EDITION.finditer(text)}
    if len(editions) == 1:
        (edition,) = tuple(editions)

        def fix_edition(raw: str) -> str:
            return _EDITION.sub(lambda m: f"{EVENT_NAME} '{edition}" if m.group(1) != edition else m.group(0), raw)

        experience = [replace(e, args=tuple(fix_edition(a) for a in e.args), bullets=tuple(fix_edition(b) for b in e.bullets))
                      for e in experience]
    else:
        fix_edition = lambda raw: raw  # noqa: E731

    # projects: stack header from the authoritative project of the same name
    auth_projects = list(authoritative.projects)
    projects: list[Entry] = []
    for entry in doc.projects:
        twin = _twin(entry, auth_projects)
        if twin is not None:
            if plain(twin.args[1]).lower() != plain(entry.args[1]).lower():
                repairs.notes.append(f"stack of '{plain(entry.args[0])}': '{plain(entry.args[1])}' -> "
                                     f"'{plain(twin.args[1])}'")
            entry = replace(entry, args=(entry.args[0], twin.args[1], entry.args[2]))
            # reworded bullet: a clearly-similar bullet takes the authoritative wording
            bullets = []
            for bullet in entry.bullets:
                best = max(twin.bullets, key=lambda t: _jaccard(_tokens(plain(t)), _tokens(plain(bullet))), default=None)
                if best is not None and 0.6 <= _jaccard(_tokens(plain(best)), _tokens(plain(bullet))) < 0.999:
                    repairs.notes.append(f"reworded bullet restored to the authoritative text: {plain(bullet)[:50]}...")
                    bullets.append(best)
                else:
                    bullets.append(bullet)
            entry = replace(entry, bullets=tuple(bullets))
        projects.append(replace(entry, args=tuple(fix_edition(a) for a in entry.args),
                                bullets=tuple(fix_edition(b) for b in entry.bullets)))
    projects = _drop_duplicate_projects(projects, experience, authoritative, repairs)
    return doc.with_(experience=tuple(experience), projects=tuple(projects))


def _org_key(entry: Entry) -> str:
    head = plain(entry.args[0])
    return head.split(",", 1)[1].strip().lower() if "," in head else head.lower()


def _all_text(doc: ResumeDoc) -> list[str]:
    out = [plain(doc.summary), plain(doc.header)]
    for entry in (*doc.experience, *doc.projects):
        out += [plain(" ".join(entry.args)), *[plain(b) for b in entry.bullets]]
    return out


def _twin(entry: Entry, candidates: list[Entry]) -> Entry | None:
    name = _tokens(plain(entry.args[0]))
    best = max(candidates, key=lambda c: _jaccard(name, _tokens(plain(c.args[0]))), default=None)
    if best is not None and _jaccard(name, _tokens(plain(best.args[0]))) >= 0.5:
        return best
    return None


def _drop_duplicate_projects(projects: list[Entry], experience: list[Entry], authoritative: ResumeDoc,
                             repairs: Repairs) -> list[Entry]:
    """A project that is the same thing as an experience entry's platform (same metric, or the same organisation's
    platform under another edition) is a duplicate: it is never rendered as a separate project."""
    seen: list[tuple[str, str]] = [(plain(b), plain(e.args[0])) for e in experience for b in e.bullets]
    kept: list[Entry] = []
    for entry in projects:
        text = " ".join(plain(b) for b in entry.bullets)
        shared = sorted({owner for bullet in entry.bullets for other, owner in seen if _repeats(plain(bullet), other)})
        seen_metrics = {m: o for m in _metrics(text) for _, o in seen[:1]}
        edition = _EDITION.search(plain(entry.args[0]))
        auth_edition = {m.group(1) for t in _all_text(authoritative) for m in _EDITION.finditer(t)}
        wrong_edition = bool(edition and auth_edition and edition.group(1) not in auth_edition)
        org_platform = bool(re.search(re.escape(EVENT_NAME.lower()) + r".*(platform|website)", plain(entry.args[0]).lower())
                            and any(EVENT_NAME.lower() in plain(e.args[0]).lower() for e in experience))
        if shared or wrong_edition or org_platform:
            why = (f"repeats claims already made under '{shared[0]}'" if shared else
                   "another edition of a platform already described under experience")
            repairs.notes.append(f"duplicate project removed: '{plain(entry.args[0])}' ({why})")
            continue
        seen += [(plain(b), plain(entry.args[0])) for b in entry.bullets]
        _ = seen_metrics
        kept.append(entry)
    return kept


def _metrics(text: str) -> set[str]:
    return {re.sub(r"[\s,+]", "", m.group(0)).lower().rstrip(".") for m in _METRIC.finditer(text)
            if len(re.sub(r"\D", "", m.group(0))) >= 2}


def _repeats(first: str, second: str) -> bool:
    """Two bullets repeat each other: two shared metrics, or one shared metric in a clearly similar sentence."""
    shared = _metrics(first) & _metrics(second)
    if len(shared) >= 2:
        return True
    return bool(shared) and _jaccard(_tokens(first), _tokens(second)) >= 0.35


# --- master pool: any ticked project may appear on either base --------------------------------------------------------


def pool_projects(doc: ResumeDoc, sources: list[ResumeDoc], project_ticked: Callable[[Entry], bool],
                  bullet_ticked: Callable[[str], bool], notes: list[str]) -> ResumeDoc:
    """Add every ticked project the other resumes have and this one lacks (matched by name, never duplicated).
    Only ticked bullets come along; authoritative values are applied afterwards by `repair_against_authoritative`."""
    projects = list(doc.projects)
    for source in sources:
        for entry in source.projects:
            if _twin(entry, projects) is not None or not project_ticked(entry):
                continue
            bullets = tuple(b for b in entry.bullets if bullet_ticked(b))
            if bullets:
                projects.append(replace(entry, bullets=bullets))
                notes.append(f"master pool: added project '{plain(entry.args[0])}' from the other resume")
    return doc.with_(projects=tuple(projects)) if len(projects) != len(doc.projects) else doc


def project_match_score(entry: Entry, keywords: list[str], title: str) -> float:
    title_concepts = set(concepts(title))
    bullet_scores = sorted((relevance_score(plain(b), keywords, title_concepts) for b in entry.bullets), reverse=True)
    head = relevance_score(plain(entry.args[0]), keywords, title_concepts) +         0.5 * relevance_score(plain(entry.args[1]), keywords, title_concepts)
    return head + sum(bullet_scores[:2])


def apply_project_order(doc: ResumeDoc, recipe: TailoringRecipe, keywords: list[str], title: str) -> ResumeDoc:
    """Recipe order wins: each `recipe.projects` slot names a project fragment, or `a|b` meaning "whichever of these
    matches the job's keywords better goes first, the other falls back to the unpinned rest". Unpinned projects keep
    the relevance order they already have."""
    remaining = list(doc.projects)
    pinned: list[Entry] = []
    for slot in recipe.projects:
        options = [o.strip().lower() for o in slot.split("|") if o.strip()]
        found: list[tuple[int, Entry]] = []
        for rank, option in enumerate(options):
            for entry in remaining:
                if option in plain(entry.args[0]).lower() and all(entry is not f for _, f in found):
                    found.append((rank, entry))
                    break
        if not found:
            continue
        chosen = max(found, key=lambda item: (project_match_score(item[1], keywords, title), -item[0]))[1]
        pinned.append(chosen)
        remaining = [e for e in remaining if e is not chosen]
    if not pinned:
        return doc
    return doc.with_(projects=tuple(pinned + remaining))


# --- ordering --------------------------------------------------------------------------------------------------------


def order_skills(skills: tuple[SkillLine, ...], weights: dict[str, float]) -> tuple[SkillLine, ...]:
    """Skill GROUPS by how much of the job they match (ties keep the base order), and inside each group the matching items
    first, heaviest concept first. Nothing is added, removed or moved to another group: a token such as Java that only
    exists in a low-ranked group comes to the front of its own group."""
    def item_weight(item: str) -> float:
        return sum(weights.get(c, 0.0) for c in set(concepts(plain(item))))

    ordered: list[tuple[float, int, SkillLine]] = []
    for index, line in enumerate(skills):
        items = list(line.items)
        scored = [(item_weight(item), i, item) for i, item in enumerate(items)]
        front = sorted((t for t in scored if t[0] > 0), key=lambda t: (-t[0], t[1]))
        rest = [t for t in scored if t[0] <= 0]
        ordered.append((sum(t[0] for t in scored), index, SkillLine(line.group, tuple(t[2] for t in front + rest))))
    ordered.sort(key=lambda t: (-round(t[0], 6), t[1]))
    return tuple(line for _, _, line in ordered)


def order_document(doc: ResumeDoc, keywords: list[str], title: str, recipe: TailoringRecipe,
                   boosts: dict[str, float] | None = None, similarity: dict[str, float] | None = None,
                   weights: dict[str, float] | None = None) -> ResumeDoc:
    """Minimal reshuffling: authored order everywhere, except (1) projects, most relevant first, (2) at most one
    bullet moved to the top of an entry when it clearly matches the job, (3) matching skill items to the front of their
    group, (4) a skills group first only when the match is clear (else the recipe's lead group)."""
    boosts = boosts or {}
    similarity = similarity or {}
    title_concepts = set(concepts(title))

    def score(raw: str) -> float:
        text = plain(raw)
        return relevance_score(text, keywords, title_concepts) + boosts.get(text, 0.0) + 0.5 * similarity.get(text, 0.0)

    def reorder_bullets(entry: Entry) -> Entry:
        if len(entry.bullets) < 2:
            return entry
        scores = [score(b) for b in entry.bullets]
        best = max(range(len(scores)), key=lambda i: (scores[i], -i))
        if best != 0 and scores[best] >= max(2.0, 2 * scores[0]):
            bullets = list(entry.bullets)
            bullets.insert(0, bullets.pop(best))
            return replace(entry, bullets=tuple(bullets))
        return entry

    experience = tuple(reorder_bullets(e) for e in doc.experience)        # entries keep my order (newest first)

    def project_score(entry: Entry) -> float:
        bullet_scores = sorted((score(b) for b in entry.bullets), reverse=True)
        head = score(entry.args[0]) + 0.5 * score(entry.args[1])
        return head + sum(bullet_scores[:2])

    index = {id(p): i for i, p in enumerate(doc.projects)}
    preferred = {frag.lower(): len(recipe.projects) - i for i, frag in enumerate(
        part for slot in recipe.projects for part in slot.split("|"))}

    def recipe_bonus(entry: Entry) -> float:
        name = plain(entry.args[0]).lower()
        return max((rank for frag, rank in preferred.items() if frag in name), default=0) * 0.01

    projects = sorted(doc.projects, key=lambda p: (-(project_score(p) + recipe_bonus(p)), index[id(p)]))
    projects = tuple(reorder_bullets(p) for p in projects)
    projects = apply_project_order(doc.with_(projects=projects), recipe, keywords, title).projects

    lines: list[SkillLine] = []
    scores_by_group: dict[str, float] = {}
    for line in doc.skills:
        items = list(line.items)
        hits = [(i, score(item)) for i, item in enumerate(items)]
        front = [items[i] for i, s in hits if s > 0]
        rest = [items[i] for i, s in hits if s <= 0]
        lines.append(SkillLine(line.group, tuple(front + rest)))
        scores_by_group[plain(line.group)] = sum(s for _, s in hits)
    ranked = sorted(scores_by_group.values(), reverse=True)
    lead = None
    if ranked and ranked[0] >= 2 and (len(ranked) < 2 or ranked[0] >= 2 * max(ranked[1], 0.01)):
        lead = max(scores_by_group, key=lambda g: scores_by_group[g])
    else:
        lead = next((g for want in recipe.lead_skill_groups for g in scores_by_group if g.lower() == want.lower()), None)
    if lead is not None:
        lines.sort(key=lambda l: 0 if plain(l.group) == lead else 1)       # stable: the others keep their order
    if weights:                               # v7: skills follow the job's weighted concepts, not the recipe's lead group
        lines = list(order_skills(doc.skills, weights))
    return doc.with_(experience=experience, projects=projects, skills=tuple(lines))


def move_certifications_first(doc: ResumeDoc) -> ResumeDoc:
    """Salesforce recipe: certifications line first in Additional Information, Agentforce first inside it."""
    additional = doc.additional
    lines = re.split(r"\\\\\s*\n?", additional)
    certs = [l for l in lines if "Certifications:" in l]
    others = [l for l in lines if "Certifications:" not in l and l.strip()]
    if not certs:
        return doc
    certs_line = certs[0]
    links = re.findall(r"\\href\{[^}]*\}\{[^}]*\}", certs_line)
    if len(links) > 1:
        links.sort(key=lambda link: 0 if "agentforce" in link.lower() or "salesforce" in link.lower() else 1)
        prefix = certs_line[: certs_line.index("\\href")]
        certs_line = prefix + ",\n".join(links)
    return doc.with_(additional="\\\\\n".join([certs_line, *others]))


# --- the working set: drop / re-add -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    kind: str            # bullet | skill | project
    group: str           # entry key (bullet), skill group (skill), project name (project)
    position: int        # original index inside its parent
    raw: str
    rank: float
    protected: bool = False


def inventory(doc: ResumeDoc, rank: Callable[[str], float], recipe: TailoringRecipe) -> list[Item]:
    items: list[Item] = []
    phrases = [str(p).lower() for p in recipe.must_keep_phrases]
    for entries in (doc.projects, doc.experience):
        for entry in entries:
            for position, bullet in enumerate(entry.bullets):
                text = plain(bullet).lower()
                keep = position == 0 or any(p in text for p in phrases)
                items.append(Item("bullet", plain(entry.args[0]), position, bullet, rank(bullet), keep))
    for line in doc.skills:
        for position, item in enumerate(line.items):
            items.append(Item("skill", plain(line.group), position, item, 0.5 * rank(item)))
    return items


def repeats_existing(doc: ResumeDoc, item: Item) -> bool:
    """A bullet to (re-)add that says what a bullet already in the document says: it shares a metric with a bullet of
    its own entry, repeats a claim anywhere (see `_repeats`), or is nearly the same sentence."""
    if item.kind != "bullet":
        return False
    text = plain(item.raw)
    for entry in (*doc.projects, *doc.experience):
        for bullet in entry.bullets:
            other = plain(bullet)
            if other == text:
                continue
            if plain(entry.args[0]) == item.group and _metrics(text) & _metrics(other):
                return True
            if _repeats(text, other) or _jaccard(_tokens(text), _tokens(other)) >= 0.5:
                return True
    return False


def remove_item(doc: ResumeDoc, item: Item) -> ResumeDoc:
    if item.kind == "bullet":
        def strip(entries: tuple[Entry, ...]) -> tuple[Entry, ...]:
            return tuple(replace(e, bullets=tuple(b for b in e.bullets if b != item.raw)) if plain(e.args[0]) == item.group
                         else e for e in entries)
        return doc.with_(projects=strip(doc.projects), experience=strip(doc.experience))
    lines = tuple(SkillLine(l.group, tuple(i for i in l.items if i != item.raw)) if plain(l.group) == item.group else l
                  for l in doc.skills)
    return doc.with_(skills=lines)


def add_item(doc: ResumeDoc, item: Item) -> ResumeDoc:
    if item.kind == "bullet":
        def put(entries: tuple[Entry, ...]) -> tuple[Entry, ...]:
            out = []
            for e in entries:
                if plain(e.args[0]) == item.group and item.raw not in e.bullets:
                    bullets = list(e.bullets)
                    bullets.insert(min(item.position, len(bullets)), item.raw)
                    e = replace(e, bullets=tuple(bullets))
                out.append(e)
            return tuple(out)
        return doc.with_(projects=put(doc.projects), experience=put(doc.experience))
    lines = []
    for l in doc.skills:
        if plain(l.group) == item.group and item.raw not in l.items:
            items = list(l.items)
            items.insert(min(item.position, len(items)), item.raw)
            l = SkillLine(l.group, tuple(items))
        lines.append(l)
    return doc.with_(skills=tuple(lines))


# --- validation of the final document ------------------------------------------------------------------------------------


def validate_document(doc: ResumeDoc, authoritative: ResumeDoc, approved_summary: Callable[[str], bool],
                      allowed_raw: set[str], base_summary_plain: str) -> Validation:
    report = Validation()
    summary = plain(doc.summary)
    if summary != base_summary_plain and not approved_summary(summary):
        report.add("summary_not_approved", summary[:160], "summary must be an approved variant or the base summary")
    auth_by_name = {tuple(sorted(_tokens(plain(p.args[0])))): p for p in authoritative.projects}
    editions = {m.group(1) for t in _all_text(authoritative) for m in _EDITION.finditer(t)}
    earlier: list[tuple[str, str]] = [(plain(b), plain(e.args[0])) for e in doc.experience for b in e.bullets]
    for project in doc.projects:
        name = plain(project.args[0])
        twin = _twin(project, list(authoritative.projects))
        if twin is not None:
            stack = {w.strip().lower() for w in split_top_level(plain(project.args[1]))}
            allowed = {w.strip().lower() for w in split_top_level(plain(twin.args[1]))}
            extra = stack - allowed
            if extra:
                report.add("project_stack_not_authoritative", f"{name}: {sorted(extra)}", plain(twin.args[1]))
        for match in _EDITION.finditer(" ".join(plain(a) for a in project.args) + " ".join(plain(b) for b in project.bullets)):
            if editions and match.group(1) not in editions:
                report.add("stale_edition", match.group(0), f"authoritative edition: {sorted(editions)}")
        text = " ".join(plain(b) for b in project.bullets)
        if re.search(r"zero downtime", text, re.IGNORECASE):
            report.add("unsupported_uptime_claim", "zero downtime", "only 99.9% uptime is stated on the resume")
        for match in _UPTIME.finditer(text):
            value = match.group(1) or match.group(2)
            if value != "99.9":
                report.add("unsupported_uptime_claim", match.group(0), "only 99.9% uptime is stated on the resume")
        for bullet in project.bullets:
            for other, owner in earlier:
                if owner != name and _repeats(plain(bullet), other):
                    report.add("repeated_metric", f"'{name}' repeats a claim of '{owner}': {plain(bullet)[:70]}")
        earlier += [(plain(b), name) for b in project.bullets]
    for entries in (doc.experience, doc.projects):
        for entry in entries:
            for bullet in entry.bullets:
                if bullet not in allowed_raw:
                    report.add("bullet_not_from_a_ticked_fact", plain(bullet)[:120], plain(entry.args[0]))
    for entry in (*doc.experience, *doc.projects):
        for raw in entry.bullets:
            for match in _UPTIME.finditer(plain(raw)):
                value = match.group(1) or match.group(2)
                if value != "99.9":
                    report.add("unsupported_uptime_claim", match.group(0), plain(entry.args[0]))
    _ = auth_by_name
    return report


__all__ = ["Item", "Repairs", "order_skills", "add_item", "apply_project_order", "pool_projects", "project_match_score", "escape_latex", "inventory", "move_certifications_first", "numbers_in",
           "order_document", "remove_item", "repair_against_authoritative", "validate_document"]
