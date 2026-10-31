"""v7 tailoring depth: weighted job concepts, relevance-driven selection (threshold, depth over breadth, reasons), skills
ordering by the job, the optional third base, tailoring strength and the skill-gaps report. Deterministic, no model."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from jobhunt.config import Config, ProfileSettings, TailoringRecipe
from jobhunt.matching.profiles import run_profile_build
from jobhunt.tailoring import changes, compose, strength
from jobhunt.tailoring import facts as fact_module
from jobhunt.tailoring.engine import Bases, Tailor, select_recipe
from jobhunt.tailoring.keywords import concepts, job_keyword_weights, job_keywords
from jobhunt.tailoring.summaries import CLOSING_CLAUSES, closing_key
from jobhunt.tailoring.texdoc import SkillLine, parse, plain
from tests.test_scoring import add_job
from tests.test_tailoring import FakeCompiler

# --- a resume with three projects of very different relevance ------------------------------------------------------

DEPTH_TEX = r"""\documentclass[10pt]{article}
\newcommand{\experienceitem}[4]{\textbf{#1} #2 #3 #4}
\newcommand{\projectitem}[4]{\textbf{#1} #2 #3 #4}
\begin{document}
\begin{center}
{\Large \textbf{JORDAN TAYLOR}}\\
{\normalsize \textcolor{mediumgray}{CS UNDERGRADUATE}}\\
\href{mailto:jordan@example.test}{jordan@example.test} \,|\, +91 9000000000
\end{center}
\section{Summary}
Final-year student who builds backend services in Java and Python.
\section{Education}
\noindent\textbf{Example Institute} \hfill \textit{Expected 2027}\\
\textit{B.Tech Computer Science} \hfill \textit{CGPA: 8.00}
\section{Professional Experience}
\experienceitem{Web Intern, Acme Pvt. Ltd}{Dec 2025 - Feb 2026}{}{
\begin{itemize}[leftmargin=12pt]
  \item Built React UI components for the platform with the core team.
\end{itemize}}
\section{Projects}
\projectitem{OrderDesk}{Java, Spring Boot, Hibernate, MySQL}{Jan 2026 - Present}{
\begin{itemize}[leftmargin=12pt]
  \item Built an order service in Java and Spring Boot with Hibernate and MySQL.
  \item Wrote 40 JUnit tests that run in GitHub Actions CI on every push.
  \item Packaged the service in a Docker container deployed on AWS EC2.
  \item Added a REST API with pagination and input validation for order search.
  \item Cut the slowest MySQL query from 900 ms to 120 ms with an index.
  \item Documented the service with an OpenAPI page for the frontend team.
\end{itemize}}
\projectitem{ChatBot}{Python, LangChain, ChromaDB}{Aug 2025 - Dec 2025}{
\begin{itemize}[leftmargin=12pt]
  \item Built a campus assistant with ChromaDB retrieval; correct answers rose from 65\% to 85\%.
  \item Wrote a prompt injection guard that blocked 9 of 10 attack prompts.
  \item Shipped it behind rate limiting for 500 students.
\end{itemize}}
\projectitem{PhotoWall}{React, TypeScript}{Mar 2025 - May 2025}{
\begin{itemize}[leftmargin=12pt]
  \item Built a photo gallery in React and TypeScript with lazy loading.
  \item Added drag-and-drop album sorting for 200 images.
\end{itemize}}
\section{Skills}
\noindent\textbf{AI \& LLMs:} RAG, LangChain, Embeddings\\
\textbf{Frontend:} React.js, TypeScript, Tailwind\\
\textbf{Backend:} Docker, REST APIs, SQL\\
\textbf{Other:} Agile/Scrum, C, Java, Git
\section{Additional Information}
\noindent\textbf{Certifications:} \href{https://example.test/c1}{AWS Certified Cloud Practitioner}\\
\end{document}
"""


class CountingCompiler:
    """Writes the tex into the 'pdf' and reports two pages once more than `capacity` list items are on the page."""

    engine = "fake"

    def __init__(self, capacity: int = 13) -> None:
        self.capacity = capacity
        self.calls = 0

    def compile(self, tex_path: Path, out_dir: Path):  # noqa: ANN201
        from jobhunt.tailoring.latex import CompileResult

        self.calls += 1
        text = tex_path.read_text(encoding="utf-8")
        pdf = out_dir / "resume.pdf"
        pdf.write_text(text, encoding="utf-8")
        return CompileResult(True, pdf, 1 if text.count("\\item") <= self.capacity else 2, "fake")


@pytest.fixture()
def depth_bases() -> Bases:
    doc = parse(DEPTH_TEX)
    per = {"ai": fact_module.extract("ai", doc)}
    merged = fact_module.merge(per)
    return Bases({"ai": doc}, per, merged, {f.id: True for f in merged})


@pytest.fixture()
def dcfg(config: Config, tmp_path: Path) -> Config:
    recipes = {"backend_swe": TailoringRecipe(base="ai", triggers=("java", "sql"), summary_variant="S4")}
    settings = dataclasses.replace(config.tailoring, recipes=recipes, llm_enabled=False)
    return dataclasses.replace(config, project_root=tmp_path, tailoring=settings)


STATE = {"capacity": 12}


@pytest.fixture(autouse=True)
def fill_from_item_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page is full (fill 1.0) at `capacity` list items; fill grows linearly from 0.45 with the items on the page."""
    from jobhunt.tailoring import engine

    def fake_fill(pdf: Path) -> float:
        return min(1.0, 0.45 + 0.55 * Path(pdf).read_text(encoding="utf-8").count("\\item") / STATE["capacity"])

    monkeypatch.setattr(engine, "content_fill", fake_fill)


JAVA_JOB = ("Requirements:\n- Core Java and Spring Boot\n- Hibernate and JPA\n- SQL (MySQL)\n- Docker and AWS\n"
            "- Unit testing with JUnit\nNice to have:\n- LLM agents and RAG\n")
REACT_ONLY_JOB = "Requirements:\n- React and TypeScript\n- HTML/CSS\n"


def run(connection, cfg, bases, description, *, capacity=12, title="Backend Engineer"):  # noqa: ANN001, ANN201
    STATE["capacity"] = capacity
    job = add_job(connection, 1, title, description)
    return Tailor(connection, cfg, CountingCompiler(capacity), None, bases=bases).tailor(job, use_llm=False)


def stored_summary(connection, result):  # noqa: ANN001, ANN201
    return json.loads(connection.execute("SELECT change_summary_json FROM tailored_resumes WHERE id = ?",
                                         (result.row_id,)).fetchone()[0])


def shown(result):  # noqa: ANN001, ANN201
    return {plain(p.args[0]): [plain(b) for b in p.bullets] for p in parse(result.tex).projects}


# --- weighted job concepts ------------------------------------------------------------------------------------------


BARCLAYS_LIKE = """Responsibilities
- Adherence to secure coding practices
- Implementation of effective unit testing practices to ensure proper code design
- Knowledge sharing and unit testing in code reviews
- Unit testing of every release
To be successful as a Full Stack Developer, you should have experience with:
- Core Java, Multi-Threading and Spring, Spring Boot.
- Spring, Spring Boot, Spring MVC, Spring Security, JPA, Hibernate.
- Strong knowledge on SQL(Oracle/ PostgreSql/ Mysql).
- Basic experience with ReactJS/Angular/typescript.
Some of Highly Valued Skills may include:
- Nice to have basic knowledge with large language models and agentic AI patterns: tool use, RAG, prompt engineering, guardrails.
- Experience building systems in trading, banking and/ or financial organization.
"""


def test_required_skills_outweigh_a_repeated_responsibility() -> None:
    weights = job_keyword_weights("Full Stack Developer", BARCLAYS_LIKE)
    order = list(weights)
    assert "java" in order[:8] and "spring boot" in order[:8] and "jpa/hibernate" in order[:8]
    assert weights["java"] > weights["rag"] > 0 and weights["spring boot"] > weights["finance"]
    assert weights["testing"] < weights["spring boot"] + 1          # four mentions did not push it past the Java stack
    assert weights["finance"] == weights["rag"] or weights["finance"] < weights["java"]
    assert "angular" in weights and "typescript" in weights and "react" in weights     # slash-joined lists are read
    assert list(job_keywords("Full Stack Developer", BARCLAYS_LIKE, limit=5)) == order[:5]


def test_optional_lines_weigh_less_and_ties_keep_first_mention_order() -> None:
    text = "Requirements:\n- Python\n- Docker\nNice to have:\n- Kubernetes\n- Redis\n"
    weights = job_keyword_weights("Engineer", text)
    assert list(weights) == ["python", "docker", "kubernetes", "redis"]
    assert weights["python"] > weights["kubernetes"] == weights["redis"]


def test_the_java_vocabulary_and_the_finance_domain_are_known_concepts() -> None:
    found = set(concepts("Spring Boot, JPA, Hibernate, JUnit, Mockito, Oracle, banking and trading, event-driven design"))
    assert {"spring boot", "jpa/hibernate", "junit", "oracle", "finance", "event-driven"} <= found


# --- relevance-driven selection --------------------------------------------------------------------------------------


def test_the_relevant_project_gets_depth_up_to_the_cap_and_irrelevant_ones_are_left_off(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    result = run(connection, dcfg, depth_bases, JAVA_JOB, capacity=8)
    pages = shown(result)
    assert list(pages)[0] == "OrderDesk"                             # most relevant first
    assert len(pages["OrderDesk"]) == 5                              # depth over breadth, capped at 5 of 6
    assert "PhotoWall" not in pages                                  # below the threshold, and the page filled without it
    assert result.pages == 1 and result.fill >= 0.92 and result.validation["ok"]
    reasons = " ".join(result.plan["selection"]["reasons"])
    assert "OrderDesk kept" in reasons and "PhotoWall dropped" in reasons and "below 12.0%" in reasons


def test_a_low_relevance_project_that_fills_the_page_keeps_only_its_two_best_bullets(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    result = run(connection, dcfg, depth_bases, JAVA_JOB, capacity=10)
    pages = shown(result)
    assert len(pages["OrderDesk"]) == 5
    low = [name for name in pages if name in {"ChatBot", "PhotoWall"}]
    assert low, "with room to spare the best remaining project should fill the page"
    for name in low:
        assert len(pages[name]) == 2, (name, pages[name])
        assert len(set(pages[name])) == 2                            # two different bullets, no duplicate
    assert "shortened to 2 bullets: low relevance to" in " ".join(result.plan["selection"]["reasons"])
    assert result.fill >= 0.92


def test_the_minimum_fill_beats_the_two_bullet_limit_and_the_reason_says_so(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    result = run(connection, dcfg, depth_bases, REACT_ONLY_JOB, capacity=12, title="Frontend Developer")
    pages = shown(result)
    assert result.fill >= 0.92 and result.pages == 1
    assert any(len(bullets) > 2 for name, bullets in pages.items() if name != "PhotoWall")
    assert "only to reach the 92% page fill" in " ".join(result.plan["selection"]["reasons"])


def test_the_job_decides_which_project_is_first(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    java = list(shown(run(connection, dcfg, depth_bases, JAVA_JOB)))[0]
    react = list(shown(run(connection, dcfg, depth_bases, REACT_ONLY_JOB, title="Frontend Developer")))[0]
    assert (java, react) == ("OrderDesk", "PhotoWall")


def test_a_bullet_is_never_repeated_and_the_page_is_one_page_at_least_92_percent_full(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    for description in (JAVA_JOB, REACT_ONLY_JOB, "Requirements:\n- LLM agents, RAG and prompt injection defence\n"):
        result = run(connection, dcfg, depth_bases, description, capacity=12)
        bullets = [b for p in parse(result.tex).projects for b in p.bullets]
        assert len(bullets) == len(set(bullets))
        assert result.pages == 1 and result.fill >= 0.92 and result.validation["ok"], (description, result.fill)
        names = list(shown(result))
        assert len(names) == len(set(names))


def test_a_project_exactly_at_the_threshold_is_shown_on_merit(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    strict = dataclasses.replace(dcfg, tailoring=dataclasses.replace(dcfg.tailoring, project_relevance_threshold=0.99))
    result = run(connection, strict, depth_bases, JAVA_JOB, capacity=12)
    selection = result.plan["selection"]
    assert [p["on_merit"] for p in selection["projects"]].count(True) == 1       # one project is always the anchor
    assert any("anchor" in reason for reason in selection["reasons"])
    lenient = dataclasses.replace(dcfg, tailoring=dataclasses.replace(dcfg.tailoring, project_relevance_threshold=0.0))
    assert all(p["on_merit"] for p in run(connection, lenient, depth_bases, JAVA_JOB)
               .plan["selection"]["projects"])


def test_the_reasons_reach_the_stored_change_summary(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    result = run(connection, dcfg, depth_bases, JAVA_JOB)
    summary = stored_summary(connection, result)
    assert summary["selection"]["reasons"] == result.plan["selection"]["reasons"]
    lines = changes.render_text(summary)
    assert any(line.startswith("Why: OrderDesk kept") for line in lines)
    assert summary["keywords"]["weighted"] is not None and summary["keywords"]["top_concepts"][0] in {"java", "spring boot", "jpa/hibernate", "docker", "sql", "aws", "junit"}


def test_a_dry_run_stores_nothing_and_writes_only_to_the_scratch_folder(connection, dcfg, depth_bases, tmp_path) -> None:  # noqa: ANN001
    job = add_job(connection, 1, "Backend Engineer", JAVA_JOB)
    scratch = tmp_path / "scratch"
    before = connection.execute("SELECT COUNT(*) FROM tailored_resumes").fetchone()[0]
    result = Tailor(connection, dcfg, CountingCompiler(), None, bases=depth_bases).tailor(job, use_llm=True, dry_run_dir=scratch)
    assert result.row_id is None and result.mode == "deterministic" and result.cost_usd == 0
    assert connection.execute("SELECT COUNT(*) FROM tailored_resumes").fetchone()[0] == before
    assert list(scratch.rglob("resume.tex")) and not (tmp_path / "data" / "tailored").exists()


# --- skills follow the job ---------------------------------------------------------------------------------------------


SKILLS = (SkillLine("AI \\& LLMs", ("RAG", "LangChain", "Embeddings")),
          SkillLine("Frontend", ("React.js", "TypeScript", "Tailwind")),
          SkillLine("Backend", ("Docker", "REST APIs", "SQL")),
          SkillLine("Other", ("Agile/Scrum", "C", "Java", "Git")))


def test_skill_groups_follow_the_job_and_matching_items_come_first() -> None:
    weights = job_keyword_weights("Backend Engineer", JAVA_JOB)
    ordered = compose.order_skills(SKILLS, weights)
    names = [plain(line.group) for line in ordered]
    assert names[0] in {"Backend", "Other"}                          # the groups the job's concepts live in lead
    assert names.index("Frontend") > names.index("Backend")
    backend = next(line for line in ordered if plain(line.group) == "Backend")
    assert backend.items[0] in {"SQL", "Docker"}
    other = next(line for line in ordered if plain(line.group) == "Other")
    assert other.items[0] == "Java"                                  # Java comes to the front of its own group


def test_ties_keep_the_base_order_and_nothing_is_invented_or_moved() -> None:
    nothing = compose.order_skills(SKILLS, {"cobol": 2.0})
    assert [l.group for l in nothing] == [l.group for l in SKILLS] and [l.items for l in nothing] == [l.items for l in SKILLS]
    weights = job_keyword_weights("Backend Engineer", JAVA_JOB)
    ordered = compose.order_skills(SKILLS, weights)
    before = {l.group: sorted(l.items) for l in SKILLS}
    after = {l.group: sorted(l.items) for l in ordered}
    assert before == after                                          # same groups, same items in the same groups


def test_the_closing_clause_follows_the_title_family() -> None:
    cases = {"Full Stack Developer": "fullstack", "Data Engineer": "data_engineering", "Machine Learning Engineer": "ml_data",
             "Backend Systems Engineer": "backend_swe", "Software Development Engineer I": "software_engineering",
             "Salesforce Engineer": "salesforce"}
    for title, key in cases.items():
        assert closing_key(title, "fintech_bank") == key
        assert key in CLOSING_CLAUSES
    assert closing_key("Java Developer", "java_backend") == "java_backend"
    assert closing_key("Java Developer", "backend_swe") == "backend_swe"
    assert closing_key("Analyst", "fintech_bank") == "fintech_bank"          # no family in the title: the recipe decides


# --- the optional third base ---------------------------------------------------------------------------------------------

BACKEND_TEX = DEPTH_TEX.replace("OrderDesk", "LedgerApi")


def three_bases() -> Bases:
    docs = {"ai": parse(DEPTH_TEX), "fullstack": parse(DEPTH_TEX.replace("ChatBot", "ChatBot2")),
            "backend": parse(BACKEND_TEX)}
    per = {name: fact_module.extract(name, doc) for name, doc in docs.items()}
    merged = fact_module.merge(per)
    return Bases(docs, per, merged, {f.id: True for f in merged})


def test_with_a_third_base_the_matcher_chooses_among_all_bases(connection, dcfg) -> None:  # noqa: ANN001
    recipes = {**dcfg.tailoring.recipes, "java_backend": TailoringRecipe(base="backend", triggers=("java", "spring boot"))}
    cfg = dataclasses.replace(dcfg, tailoring=dataclasses.replace(dcfg.tailoring, recipes=recipes))
    bases = three_bases()
    job = add_job(connection, 1, "Java Backend Developer", JAVA_JOB)
    tailor = Tailor(connection, cfg, CountingCompiler(), None, bases=bases)
    weights = job_keyword_weights("Java Backend Developer", JAVA_JOB)
    assert tailor.best_base(weights, "backend") == "backend"
    assert tailor.best_base(weights, "ai") in {"ai", "fullstack", "backend"}
    result = tailor.tailor(job, use_llm=False, dry_run_dir=cfg.project_root / "scratch")
    assert result.plan["recipe"] == "java_backend" and result.base == "backend"
    assert "Looking for a Java backend engineering role." in plain(parse(result.tex).summary) or result.plan["closing"] == "java_backend"


def test_without_the_backend_file_that_recipe_is_never_selected_and_nothing_breaks(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    recipes = {**dcfg.tailoring.recipes, "java_backend": TailoringRecipe(base="backend", triggers=("java", "spring boot"))}
    cfg = dataclasses.replace(dcfg, tailoring=dataclasses.replace(dcfg.tailoring, recipes=recipes))
    job = add_job(connection, 1, "Java Backend Developer", JAVA_JOB)
    from jobhunt.tailoring.engine import job_from_db

    job_input = job_from_db(connection, job)
    assert select_recipe(cfg, job_input, list(job_keyword_weights(job_input.title, job_input.description)), {"ai"}) == "backend_swe"
    result = Tailor(connection, cfg, CountingCompiler(), None, bases=depth_bases).tailor(job, use_llm=False)
    assert result.base == "ai" and result.plan["recipe"] == "backend_swe" and result.status == "ok"
    with pytest.raises(LookupError, match="resume_backend.tex"):
        Tailor(connection, cfg, CountingCompiler(), None, bases=depth_bases).tailor(job, base="backend", use_llm=False)


def _profile_cfg(config: Config, root: Path, with_backend: bool) -> Config:
    (root / "resumes").mkdir()
    (root / "resumes" / "ai.txt").write_text("Jordan Taylor\n" + "Builds RAG assistants in Python and FastAPI. " * 8 + "\nSkills: Python, FastAPI", encoding="utf-8")
    (root / "resumes" / "fs.txt").write_text("Jordan Taylor\n" + "Builds React apps with TypeScript. " * 8 + "\nSkills: React, TypeScript", encoding="utf-8")
    if with_backend:
        (root / "resumes" / "be.txt").write_text("Jordan Taylor\n" + "Builds Spring Boot services in Java. " * 8 + "\nSkills: Java, Spring Boot", encoding="utf-8")
    files = {"ai": "ai.txt", "fullstack": "fs.txt", "backend": "be.txt"}
    settings = ProfileSettings(resume_dir="resumes", profile_dir="profile", files=files)
    return dataclasses.replace(config, profiles=settings, project_root=root)


def test_profile_build_skips_an_absent_optional_base_silently(connection: sqlite3.Connection, config: Config, tmp_path: Path) -> None:
    summary = run_profile_build(connection, _profile_cfg(config, tmp_path, with_backend=False))
    assert {p.name for p in summary.profiles} == {"ai", "fullstack"} and summary.skipped_optional == ["backend"]


def test_profile_build_builds_a_present_optional_base_without_touching_the_matching_tables(
    connection: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    summary = run_profile_build(connection, _profile_cfg(config, tmp_path, with_backend=True))
    assert {p.name for p in summary.profiles} == {"ai", "fullstack", "backend"} and summary.skipped_optional == []
    assert {r[0] for r in connection.execute("SELECT name FROM profiles")} == {"ai", "fullstack"}


# --- tailoring strength --------------------------------------------------------------------------------------------------


def summary_of(*, summary=False, projects=False, bullets=False, skills=False, coverage=0.2, missing=None, dropped=False):  # noqa: ANN001, ANN201
    return {"recorded": True, "summary": {"changed": summary},
            "projects": {"removed": ["X"] if dropped else [], "added_from_pool": [], "reordered": projects},
            "bullets": [{"entry": "A", "dropped": [{"fact": "T-1"}] if dropped else [], "added": [], "status": "changed"}] if bullets else [],
            "skills": {"group_order_changed": skills, "lines": []},
            "keywords": {"weighted": coverage, "missing": missing or [], "total": 10, "coverage_output": 2},
            "selection": {}}


def test_strength_is_low_when_nothing_changed_and_says_why_and_what_helps(dcfg) -> None:  # noqa: ANN001
    result = strength.tailoring_strength(summary_of(coverage=0.2, missing=["spring boot", "jpa/hibernate", "junit"]), dcfg.tailoring)
    assert result["level"] == "Low" and result["adaptation"] == 0.0
    assert any("fit on one page" in line for line in result["why"])
    assert any("Spring Boot" in line and "JPA/Hibernate" in line and "not on your resume" in line for line in result["why"])
    assert "resume_backend.tex" in result["help"] and "Skill gaps" in result["help"]
    assert "20% of the job's weighted concepts covered" in result["text"]


def test_strength_levels_follow_coverage_first(dcfg) -> None:  # noqa: ANN001
    high = strength.tailoring_strength(summary_of(summary=True, projects=True, bullets=True, skills=True, coverage=0.9), dcfg.tailoring)
    assert high["level"] == "High" and high["score"] >= 0.75 and high["help"] is None
    medium = strength.tailoring_strength(summary_of(summary=True, projects=True, coverage=0.6, dropped=True), dcfg.tailoring)
    assert medium["level"] == "Medium"
    busy_but_blind = strength.tailoring_strength(summary_of(summary=True, projects=True, bullets=True, skills=True, coverage=0.25,
                                                            missing=["java"]), dcfg.tailoring)
    assert busy_but_blind["level"] == "Low" and busy_but_blind["help"]       # everything moved, but only 25% of the job is covered
    unchanged = strength.tailoring_strength(summary_of(coverage=0.62, missing=["spring boot"]), dcfg.tailoring)
    assert unchanged["level"] == "Low" and unchanged["adaptation"] == 0.0     # covers 62% but nothing differs from the base
    adapted = strength.tailoring_strength(summary_of(summary=True, projects=True, skills=True, coverage=0.62,
                                                     missing=["spring boot"]), dcfg.tailoring)
    assert adapted["level"] == "Medium"


def test_strength_is_unknown_when_the_comparison_was_not_recorded(dcfg) -> None:  # noqa: ANN001
    assert strength.tailoring_strength({"recorded": False, "reason": "gone"}, dcfg.tailoring)["level"] == "Unknown"
    assert strength.tailoring_strength({"recorded": False, "partial": True}, dcfg.tailoring)["level"] == "Unknown"
    assert strength.tailoring_strength(None, dcfg.tailoring)["level"] == "Unknown"


def test_a_real_tailored_run_gets_a_strength(connection, dcfg, depth_bases) -> None:  # noqa: ANN001
    result = run(connection, dcfg, depth_bases, JAVA_JOB)
    got = strength.tailoring_strength(stored_summary(connection, result), dcfg.tailoring)
    assert got["level"] in {"Low", "Medium", "High"} and got["coverage"] is not None and got["coverage"] > 0


# --- skill gaps ---------------------------------------------------------------------------------------------------------


def add_relevant(connection: sqlite3.Connection, n: int, title: str, description: str, *, gate: str = "target", status: str = "open") -> int:
    job_id = add_job(connection, n, title, description, status=status)
    connection.execute(
        """INSERT INTO job_relevance (job_id, computed_at, rules_hash, input_hash, gate_status, gate_rule, gate_reason, final_score)
           VALUES (?, '2026-10-12T00:00:00Z', 'r', 'i', ?, 'x', 'x', ?)""", (job_id, gate, 90 - n))
    return job_id


def test_skill_gaps_count_the_jobs_asking_for_what_my_facts_do_not_cover(connection: sqlite3.Connection) -> None:
    padding = " " * 0
    spring = "Requirements:\n- Core Java and Spring Boot\n- Hibernate\n- Docker\n" + "We build banking software. " * 12 + padding
    for n in range(1, 4):
        add_relevant(connection, n, f"Java Developer {n}", spring)
    add_relevant(connection, 4, "Platform Engineer", "Requirements:\n- Kubernetes\n- Docker\n" + "We run clusters every day. " * 12)
    add_relevant(connection, 5, "Closed Java Job", spring, status="closed")
    add_relevant(connection, 6, "Excluded Java Job", spring, gate="excluded")
    add_job(connection, 7, "No relevance row", spring)
    before = connection.total_changes
    facts = ["Built a service with Docker and REST APIs", "Python, SQL"]
    result = strength.skill_gaps(connection, facts, use_cache=False)
    assert connection.total_changes == before                       # read-only
    by = {item["concept"]: item for item in result["items"]}
    assert result["jobs_checked"] == 4
    assert by["spring boot"]["jobs"] == 3 and by["java"]["jobs"] == 3 and by["jpa/hibernate"]["jobs"] == 3
    assert by["kubernetes"]["jobs"] == 1
    assert "docker" not in by and "sql" not in by                    # already on my facts
    assert by["spring boot"]["label"] == "Spring Boot" and by["spring boot"]["examples"][0].startswith("Java Developer")
    assert result["items"][0]["jobs"] >= result["items"][-1]["jobs"]
    assert len(strength.skill_gaps(connection, facts, top=2, use_cache=False)["items"]) == 2
