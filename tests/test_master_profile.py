"""v3 master profile: any ticked project may appear on either base; recipe order; the summary-support rule."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from jobhunt.config import Config, TailoringRecipe
from jobhunt.tailoring import compose
from jobhunt.tailoring import facts as fact_module
from jobhunt.tailoring.engine import Bases, Tailor
from jobhunt.tailoring.summaries import Variant, rank_variants, read_variants, render_file, unsupported_mentions
from jobhunt.tailoring.texdoc import parse, plain
from tests.test_scoring import add_job
from tests.test_tailoring import BASE_TEX, FakeCompiler

PORTAL_PROJECT = r"""\projectitem{PortalApp - Student Portal}{Python, FastAPI, MongoDB, React}{Aug 2025-Present}{
\begin{itemize}[leftmargin=12pt]
  \item A unified student portal for grades and attendance, now used by 1000+ students.
  \item Designed the MongoDB schema and REST API layer in FastAPI with indexing for query performance.
\end{itemize}}
"""
# a fullstack resume: the portal and CampusBot, but no FinAgent
_with_portal = BASE_TEX.replace(r"\projectitem{FinAgent}", PORTAL_PROJECT + r"\projectitem{FinAgent}", 1)
FULLSTACK_TEX = _with_portal[: _with_portal.index(r"\projectitem{FinAgent}")] + \
    _with_portal[_with_portal.index(r"\projectitem{CampusBot}"):]


@pytest.fixture()
def two_bases() -> Bases:
    docs = {"ai": parse(BASE_TEX), "fullstack": parse(FULLSTACK_TEX)}
    per = {name: fact_module.extract(name, doc) for name, doc in docs.items()}
    merged = fact_module.merge(per)
    return Bases(docs, per, merged, {f.id: True for f in merged})


@pytest.fixture()
def cfg(config: Config, tmp_path: Path) -> Config:
    recipes = {
        "ai_agents_rag": TailoringRecipe(base="ai", triggers=("rag", "agents", "llm"), summary_variant="S1",
                                         projects=("finagent", "campusbot")),
        "fullstack": TailoringRecipe(base="fullstack", triggers=("react", "next.js"), summary_variant="S3",
                                     projects=("portalapp|finagent", "campusbot")),
    }
    settings = dataclasses.replace(config.tailoring, recipes=recipes, llm_enabled=False)
    return dataclasses.replace(config, project_root=tmp_path, tailoring=settings)


def headings(tex: str) -> list[str]:
    return [plain(p.args[0]) for p in parse(tex).projects]


def tailor_ai(connection, cfg, bases, base="ai"):  # noqa: ANN001, ANN201
    job = add_job(connection, 1, "AI Engineer", "Build LLM agents with RAG, ChromaDB retrieval and FastAPI.")
    return Tailor(connection, cfg, FakeCompiler(), None, bases=bases).tailor(job, use_llm=False, base=base)


def test_a_project_only_the_other_resume_has_shows_up_on_either_base(connection, cfg, two_bases) -> None:  # noqa: ANN001
    result = tailor_ai(connection, cfg, two_bases, "ai")
    assert "PortalApp - Student Portal" in headings(result.tex)
    assert result.status == "ok"
    full = tailor_ai(connection, cfg, two_bases, "fullstack")
    assert {"FinAgent", "CampusBot"} <= set(headings(full.tex))        # FinAgent arrives from the ai resume


def test_nothing_is_duplicated(connection, cfg, two_bases) -> None:  # noqa: ANN001
    result = tailor_ai(connection, cfg, two_bases, "ai")
    names = headings(result.tex)
    assert len(names) == len(set(names))
    bullets = [b for p in parse(result.tex).projects for b in p.bullets]
    assert len(bullets) == len(set(bullets))


def test_an_unticked_project_never_joins_the_pool(connection, cfg, two_bases) -> None:  # noqa: ANN001
    portal = next(f for f in two_bases.merged if f.kind == "project" and f.text.startswith("PortalApp"))
    two_bases.ticked[portal.id] = False
    result = tailor_ai(connection, cfg, two_bases, "ai")
    assert "PortalApp - Student Portal" not in headings(result.tex)


def test_recipe_order_pins_projects_and_picks_the_better_match_first() -> None:
    doc = parse(BASE_TEX.replace(r"\projectitem{CampusBot}", PORTAL_PROJECT + r"\projectitem{CampusBot}", 1))
    recipe = TailoringRecipe(projects=("campusbot", "portalapp|finagent"))
    ordered = compose.apply_project_order(doc, recipe, ["react", "mongodb", "fastapi"], "Full Stack Engineer")
    names = [plain(p.args[0]) for p in ordered.projects]
    assert names[0] == "CampusBot"                  # pinned first
    assert names[1] == "PortalApp - Student Portal"  # better keyword match of the alternatives
    assert names[2] == "FinAgent"                   # the other alternative falls back with the rest
    backend = compose.apply_project_order(doc, recipe, ["pytest", "pgvector", "xgboost"], "Backend Engineer")
    assert [plain(p.args[0]) for p in backend.projects][1] == "FinAgent"


def test_summary_is_flagged_when_it_mentions_a_project_that_is_not_on_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobhunt.tailoring import summaries

    monkeypatch.setitem(summaries.PROJECT_MARKERS, "GammaBot", ("campus assistant", "rag assistant"))
    page = "Orbit agent. Built a 60-case pytest suite. 58% accuracy vs a 44% baseline."
    text = "Builds tool-using LLM agents: a portfolio agent with a 60-case suite and a campus assistant 65% to 85%."
    missing = unsupported_mentions(text, page, ["Orbit - AI Portfolio Agent"])
    assert "project:GammaBot" in missing and "number:65%" in missing


def test_a_fully_supported_summary_has_no_missing_items() -> None:
    page = "Fest GammaBot raised correct answers from 65% to 85%."
    text = "A campus assistant that raised correct answers from 65% to 85%."
    assert unsupported_mentions(text, page, ["Fest GammaBot"]) == []


def test_ranking_prefers_the_advised_then_the_recipe_variant() -> None:
    variants = [Variant(f"S{i}", "x", f"text {i}", True) for i in range(1, 4)]
    assert [v.id for v in rank_variants(variants, "S3", "S2", [])][:2] == ["S3", "S2"]
    assert [v.id for v in rank_variants(variants, "", "S2", [])][0] == "S2"


def test_end_to_end_unsupported_variant_falls_back_and_supported_is_used(connection, cfg, two_bases) -> None:  # noqa: ANN001
    path = cfg.resolve_path(cfg.tailoring.variants_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    texts = {
        "S1": "A student portal used by 1000+ students and a campus assistant from 65% to 85%.",
        "S4": "A 60-case pytest suite in CI and answers from 65% to 85%.",
    }
    path.write_text(render_file({"S1": True, "S4": True}, texts), encoding="utf-8")
    portal = next(f for f in two_bases.merged if f.kind == "project" and f.text.startswith("PortalApp"))
    two_bases.ticked[portal.id] = False         # the portal cannot be shown, so S1 ("student portal") is unsupported
    result = tailor_ai(connection, cfg, two_bases, "ai")
    assert result.plan["summary_variant"] == "S4"
    assert any("S1" in note for note in result.plan["notes"])
    assert result.validation["ok"] is True


def test_no_approved_variant_means_the_base_summary_verbatim(connection, cfg, two_bases) -> None:  # noqa: ANN001
    result = tailor_ai(connection, cfg, two_bases, "ai")
    assert result.plan["summary_variant"] == "base"
    assert plain(parse(result.tex).summary) == plain(two_bases.docs["ai"].summary)


def test_variants_file_round_trip_still_parses(tmp_path: Path) -> None:
    path = tmp_path / "s.md"
    path.write_text(render_file({"S1": True}), encoding="utf-8")
    assert [v.id for v in read_variants(path)] == ["S1", "S2", "S3", "S4", "S5", "S6"]
