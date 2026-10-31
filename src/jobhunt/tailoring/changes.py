"""What changed in a tailored resume versus the base resume (deterministic, no model).

`compute_change_summary` compares the tailored document with the unmodified base `.tex` and returns plain JSON:
summary variant, projects (kept / removed / added from the other resume's pool / reordered), bullets per entry
(kept / dropped / moved to top / added from the pool, identified by fact id and the first 8 words, never invented
text), skills lines, keyword coverage and page fit. `render_text` turns it into readable lines, and
`compare_versions` gives the one-line diff against the previous version of the same job
("v2 vs v1: swapped BetaPortal for AlphaAgent, +2 bullets").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jobhunt.tailoring import facts as fact_module
from jobhunt.tailoring.texdoc import Entry, ResumeDoc, plain

SCHEMA = 1
NOT_RECORDED = {"recorded": False, "reason": "not recorded: the files of this run were overwritten by a later run"}


def words(text: str, count: int = 8) -> str:
    return " ".join(plain(text).split()[:count])


def _bullet(raw: str, raw_to_fact: Mapping[str, str]) -> dict[str, str]:
    return {"fact": raw_to_fact.get(raw) or fact_module.fact_id("bullet", plain(raw)), "words": words(raw)}


def _entries(doc: ResumeDoc, kind: str) -> list[Entry]:
    return list(doc.projects if kind == "projects" else doc.experience)


def _entry_map(doc: ResumeDoc) -> dict[str, Entry]:
    return {plain(e.args[0]): e for e in (*doc.experience, *doc.projects)}


def snapshot(doc: ResumeDoc, raw_to_fact: Mapping[str, str]) -> dict[str, Any]:
    """The comparable shape of one document (kept inside the stored summary so versions can be diffed later)."""
    return {
        "projects": [plain(e.args[0]) for e in doc.projects],
        "experience": [plain(e.args[0]) for e in doc.experience],
        "bullets": {plain(e.args[0]): [_bullet(b, raw_to_fact) for b in e.bullets]
                    for e in (*doc.experience, *doc.projects)},
        "skills": [{"group": plain(line.group), "items": [plain(i) for i in line.items]} for line in doc.skills],
        "summary": plain(doc.summary),
    }


def _closing_clause(text: str, clauses: Sequence[str]) -> str | None:
    lowered = text.lower()
    for clause in clauses:
        if clause and clause.lower().strip(" .") in lowered:
            return clause
    return None


def compute_change_summary(
    base_doc: ResumeDoc, new_doc: ResumeDoc, plan: Mapping[str, Any], raw_to_fact: Mapping[str, str], *,
    base_name: str, closing_clauses: Sequence[str] = (), dropped_to_fit: Sequence[str] = (), pages: int = 0,
) -> dict[str, Any]:
    base, new = snapshot(base_doc, raw_to_fact), snapshot(new_doc, raw_to_fact)

    # summary
    summary_changed = base["summary"] != new["summary"]
    summary = {
        "base": base_name, "variant": plan.get("summary_variant") or ("base" if not summary_changed else "custom"),
        "changed": summary_changed, "new_text": new["summary"] if summary_changed else "",
        "closing_clause": _closing_clause(new["summary"], closing_clauses) if summary_changed else None,
    }

    # projects
    base_projects, new_projects = base["projects"], new["projects"]
    kept_in_order = [p for p in new_projects if p in base_projects]
    projects = {
        "base_order": base_projects, "new_order": new_projects,
        "removed": [p for p in base_projects if p not in new_projects],
        "added_from_pool": [p for p in new_projects if p not in base_projects],
        "reordered": kept_in_order != [p for p in base_projects if p in new_projects],
    }

    # bullets, per entry
    bullets: list[dict[str, Any]] = []
    for name in dict.fromkeys([*new["bullets"], *base["bullets"]]):
        old_list, new_list = base["bullets"].get(name, []), new["bullets"].get(name, [])
        if name not in new["bullets"]:
            bullets.append({"entry": name, "status": "entry_removed", "kept": 0, "dropped": old_list, "added": [],
                            "moved_to_top": None, "base_count": len(old_list), "new_count": 0})
            continue
        old_ids, new_ids = [b["fact"] for b in old_list], [b["fact"] for b in new_list]
        dropped = [b for b in old_list if b["fact"] not in new_ids]
        added = [b for b in new_list if b["fact"] not in old_ids]
        kept = [b for b in new_list if b["fact"] in old_ids]
        moved = None
        if new_list and old_ids and new_ids[0] != old_ids[0] and new_ids[0] in old_ids:
            moved = new_list[0]
        entry_order_changed = [i for i in new_ids if i in old_ids] != [i for i in old_ids if i in new_ids]
        if dropped or added or moved or entry_order_changed or name not in base["bullets"]:
            bullets.append({
                "entry": name, "status": "added_entry" if name not in base["bullets"] else "changed",
                "kept": len(kept), "dropped": dropped, "added": added, "moved_to_top": moved,
                "reordered": entry_order_changed, "base_count": len(old_list), "new_count": len(new_list)})

    # skills
    skill_changes: list[dict[str, Any]] = []
    base_skills = {s["group"]: s["items"] for s in base["skills"]}
    for line in new["skills"]:
        old_items = base_skills.get(line["group"])
        if old_items is None:
            skill_changes.append({"group": line["group"], "status": "new_line", "added": line["items"]})
        elif line["items"] != old_items:
            added = [i for i in line["items"] if i not in old_items]
            removed = [i for i in old_items if i not in line["items"]]
            front = [i for i in line["items"][:3] if i in old_items and old_items.index(i) >= 3]
            skill_changes.append({"group": line["group"], "status": "changed", "moved_to_front": front,
                                  "added": added, "removed": removed})
    skills = {
        "group_order_changed": [s["group"] for s in new["skills"] if s["group"] in base_skills]
        != [g for g in base_skills if g in {s["group"] for s in new["skills"]}],
        "lines": skill_changes,
    }

    # keywords and page fit come from the stored plan
    keywords = {
        "matched": [{"keyword": m.get("keyword"), "fact": m.get("fact"), "fact_text": m.get("fact_text", "")}
                    for m in plan.get("matched", []) if isinstance(m, Mapping)],
        "missing": list(plan.get("missing", [])),
        "coverage_base": len(plan.get("base_coverage", [])), "coverage_output": len(plan.get("output_coverage", [])),
        "total": len(plan.get("keywords", [])),
        "weighted": plan.get("coverage_weighted"), "weighted_base": plan.get("coverage_weighted_base"),
        "top_concepts": list((plan.get("weights") or {}))[:8],
    }
    page_fit = {"fill": plan.get("fill"), "pages": pages, "dropped_to_fit": list(dropped_to_fit),
                "notes": list(plan.get("notes", []))}

    identical = (not summary_changed and not projects["removed"] and not projects["added_from_pool"]
                 and not projects["reordered"] and not bullets and not skill_changes and not skills["group_order_changed"])
    return {"schema": SCHEMA, "recorded": True, "identical_to_base": identical, "summary": summary,
            "projects": projects, "bullets": bullets, "skills": skills, "keywords": keywords, "page_fit": page_fit,
            "selection": plan.get("selection") or {},
            "snapshot": {"projects": new["projects"], "bullets": {k: [b["fact"] for b in v] for k, v in new["bullets"].items()},
                         "summary_variant": summary["variant"], "skills": new["skills"]}}


def from_plan_only(plan: Mapping[str, Any], *, base_name: str, pages: int, reason: str) -> dict[str, Any]:
    """Partial summary for old runs whose files are gone: only what the stored plan still says."""
    shown = list(plan.get("projects_shown", []))
    return {
        "schema": SCHEMA, "recorded": False, "partial": True, "reason": reason, "identical_to_base": None,
        "summary": {"base": base_name, "variant": plan.get("summary_variant"), "changed": None, "new_text": "",
                    "closing_clause": None},
        "projects": {"base_order": [], "new_order": shown, "removed": [], "added_from_pool": [], "reordered": None},
        "bullets": [], "skills": {"group_order_changed": None, "lines": []},
        "keywords": {"matched": [{"keyword": m.get("keyword"), "fact": m.get("fact"), "fact_text": m.get("fact_text", "")}
                                 for m in plan.get("matched", []) if isinstance(m, Mapping)],
                     "missing": list(plan.get("missing", [])), "coverage_base": len(plan.get("base_coverage", [])),
                     "coverage_output": len(plan.get("output_coverage", [])), "total": len(plan.get("keywords", []))},
        "page_fit": {"fill": plan.get("fill"), "pages": pages, "dropped_to_fit": [], "notes": list(plan.get("notes", []))},
        "snapshot": {"projects": shown, "bullets": {}, "summary_variant": plan.get("summary_variant"), "skills": []},
    }


# --- readable text ------------------------------------------------------------------------------------------------


def _items(entries: Sequence[Mapping[str, str]]) -> str:
    return "; ".join(f"{e['fact']} \"{e['words']}...\"" for e in entries)


def render_text(summary: Mapping[str, Any]) -> list[str]:
    """Plain-language lines, one per change. Never contains model text."""
    if not summary.get("recorded") and not summary.get("partial"):
        return [str(summary.get("reason") or "not recorded")]
    lines: list[str] = []
    if summary.get("partial"):
        lines.append(f"Partly recorded only: {summary.get('reason')}")
    if summary.get("identical_to_base"):
        lines.append("Identical to the base resume: nothing was changed, only the page fit was checked.")
    s = summary["summary"]
    if s.get("changed") is True:
        clause = f", closing clause: \"{s['closing_clause']}\"" if s.get("closing_clause") else ""
        lines.append(f"Summary: base {s['base']} summary replaced by approved variant {s['variant']}{clause}.")
    elif s.get("changed") is False:
        lines.append(f"Summary: the base {s['base']} summary, unchanged.")
    elif s.get("variant"):
        lines.append(f"Summary variant: {s['variant']}.")
    p = summary["projects"]
    if p.get("base_order") or p.get("new_order"):
        parts = []
        if p.get("removed"):
            parts.append("removed " + ", ".join(p["removed"]))
        if p.get("added_from_pool"):
            parts.append("added from the other resume " + ", ".join(p["added_from_pool"]))
        if p.get("reordered"):
            parts.append("reordered")
        lines.append("Projects: " + ("; ".join(parts) if parts else "same as base") + ". Order now: "
                     + ", ".join(p["new_order"]) + ".")
    for b in summary["bullets"]:
        if b["status"] == "entry_removed":
            lines.append(f"Bullets, {b['entry']}: whole entry removed ({b['base_count']} bullets).")
            continue
        bits = [f"kept {b['kept']} of {b['base_count']}"] if b["status"] == "changed" else ["entry added from the other resume"]
        if b["dropped"]:
            bits.append("dropped " + _items(b["dropped"]))
        if b["added"]:
            bits.append(("added from the pool " if b["status"] == "changed" else "all from the pool ") + _items(b["added"]))
        if b.get("moved_to_top"):
            bits.append(f"moved to top {_items([b['moved_to_top']])}")
        elif b.get("reordered"):
            bits.append("reordered")
        lines.append(f"Bullets, {b['entry']}: " + "; ".join(bits) + ".")
    chosen = (summary.get("selection") or {}).get("reasons") or []
    for reason in chosen:
        lines.append("Why: " + reason)
    k = summary["skills"]
    if k.get("group_order_changed"):
        lines.append("Skills: line order changed.")
    for line in k.get("lines", []):
        bits = []
        if line.get("moved_to_front"):
            bits.append("moved to front " + ", ".join(line["moved_to_front"]))
        if line.get("added"):
            bits.append("added " + ", ".join(line["added"]))
        if line.get("removed"):
            bits.append("removed " + ", ".join(line["removed"]))
        lines.append(f"Skills, {line['group']}: " + ("; ".join(bits) or "reordered") + ".")
    kw = summary["keywords"]
    if kw.get("total"):
        lines.append(f"Keywords: {len(kw['matched'])} of {kw['total']} matched (base resume matched "
                     f"{kw['coverage_base']}, this one {kw['coverage_output']}). Missing: "
                     + (", ".join(kw["missing"]) or "none") + ".")
    f = summary["page_fit"]
    fill = f"{round(float(f['fill']) * 100)}%" if f.get("fill") is not None else "unknown"
    dropped = list(f.get("dropped_to_fit") or [])
    shown = "; ".join(dropped[:5]) + (f"; and {len(dropped) - 5} more" if len(dropped) > 5 else "")
    lines.append(f"Page: {f.get('pages') or '?'} page, {fill} filled" + (f"; dropped to fit: {shown}" if dropped else "") + ".")
    return lines


def compare_versions(previous: Mapping[str, Any] | None, current: Mapping[str, Any], *, previous_version: int,
                     current_version: int) -> str:
    """"v2 vs v1: swapped BetaPortal for AlphaAgent, +2 bullets, summary S3 -> S2" (empty snapshot data is skipped)."""
    head = f"v{current_version} vs v{previous_version}: "
    if not previous or not previous.get("snapshot") or not current.get("snapshot"):
        return head + "no comparison recorded for the earlier version."
    a, b = previous["snapshot"], current["snapshot"]
    parts: list[str] = []
    gone = [p for p in a.get("projects", []) if p not in b.get("projects", [])]
    came = [p for p in b.get("projects", []) if p not in a.get("projects", [])]
    for old, new in zip(gone, came):
        parts.append(f"swapped {old} for {new}")
    for extra in came[len(gone):]:
        parts.append(f"added project {extra}")
    for extra in gone[len(came):]:
        parts.append(f"removed project {extra}")
    if not gone and not came and a.get("projects") and a["projects"] != b.get("projects"):
        parts.append("reordered projects")
    if a.get("bullets") and b.get("bullets"):
        old_ids = {i for ids in a["bullets"].values() for i in ids}
        new_ids = {i for ids in b["bullets"].values() for i in ids}
        gained, lost = len(new_ids - old_ids), len(old_ids - new_ids)
        if gained:
            parts.append(f"+{gained} bullets")
        if lost:
            parts.append(f"-{lost} bullets")
    if a.get("summary_variant") != b.get("summary_variant"):
        parts.append(f"summary {a.get('summary_variant') or 'base'} -> {b.get('summary_variant') or 'base'}")
    partial = " (the earlier version is only partly recorded)" if previous.get("partial") else ""
    return head + (", ".join(parts) if parts else "no change in what was recorded") + partial
