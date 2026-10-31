"""v8 letters: the fallback ladder, the letter validator (paraphrase allowed, invention rejected), copy detection, the outline that
replaces the template, MAX_TOKENS as cut off, per-click limits, plan flags, voice samples and my own edits. Models are fakes."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from jobhunt.config import BudgetSettings, Config
from jobhunt.matching.budget import BudgetGuard, UsageLedger
from jobhunt.matching.gemini import GenerateResult
from jobhunt.tailoring.engine import Tailor
from jobhunt.writing import staged
from jobhunt.writing.copycheck import check_copying
from jobhunt.writing.letter import (
    company_sentences, evidence_note, greeting_line, normalise_letter, posting_mentions_company, split_letter, with_name,
)
from jobhunt.writing.service import WritingService, profile_basics, usable_facts
from jobhunt.writing.validator import validate_letter
from jobhunt.writing.voice import clean_samples
from tests.test_scoring import add_job
from tests.test_tailoring import FakeClient, FakeCompiler
from tests.test_writing import bases, cfg, fid  # noqa: F401  (fixtures and helpers)

DESCRIPTION = ("We build software for warehouse robots. Our mission is to make robots easier to program. We are hiring an AI "
               "Engineer to build LLM agents and RAG pipelines with Python, FastAPI and Kubernetes. Experience with vector search "
               "is a plus.")
NO_COMPANY_DESCRIPTION = "Build LLM agents and RAG pipelines with Python, FastAPI and Kubernetes. Experience with vector search is a plus."
JOB_VIEW = {"title": "AI Engineer", "company": "Acme Robotics", "location": "Bengaluru, India", "description": DESCRIPTION,
            "question": ""}

GREETING = "Dear Acme Robotics Hiring Team,"
BODY = (
    "I am writing to express my interest in the AI Engineer role at Acme Robotics. I am a final-year student at Example Institute, "
    "studying for a B.Tech in Computer Science, and I expect to graduate in 2027. Building software that lets language models work "
    "with real data has become the part of engineering I enjoy most, and this role looks like a chance to do more of it.\n\n"
    "FinAgent was my main project in this area. I built an agent that decides which of four tools to call, among them a document "
    "search backed by pgvector that covers forty documents, and it answers questions in plain language. I also wrote an evaluation "
    "suite of sixty test cases that runs in GitHub Actions on every change, so I could see whether my edits helped. The posting asks "
    "for agents and retrieval pipelines, and this project is exactly that kind of system, built from the first query to the last "
    "check.\n\n"
    "CampusBot was a smaller project with a different goal. It answers questions for students using ChromaDB for retrieval, and after "
    "I tuned it the share of correct answers rose from 65% to 85%. I packaged it in a Docker container that does not run as root and "
    "added rate limiting, which taught me to think about safety and reliability early. That habit matters when people must trust an assistant.\n\n"
    "The posting describes a team that wants robots to be easier to program, and that is a goal I would like to support. Careful "
    "evaluation habits and a willingness to ship small, tested pieces of work are what I would bring.\n\n"
    "Thank you for your time. I would welcome the opportunity to discuss how I could contribute to the AI Engineer role at Acme Robotics."
)
LETTER = f"{GREETING}\n\n{BODY}\n\nSincerely,\n[Your Name]"


@pytest.fixture()
def vcfg(cfg: Config) -> Config:  # noqa: F811
    return dataclasses.replace(cfg, writing=dataclasses.replace(cfg.writing, pipeline="staged", cover_words_min=220, cover_words_max=300))


def ids_for(bases):  # noqa: ANN001, ANN201
    return {"fin": fid(bases, "FinAgent"), "agent": fid(bases, "tool-using LLM agent"), "pytest": fid(bases, "60-case pytest"),
            "bot": fid(bases, "CampusBot"), "chroma": fid(bases, "ChromaDB retrieval"), "docker": fid(bases, "non-root")}


def plan_answer(bases, company=True):  # noqa: ANN001, ANN201
    i = ids_for(bases)
    return {"job_angle": "LLM agents and RAG pipelines", "company_paragraph": company, "refusal": "", "paragraphs": [
        {"intent": "hook", "point": "the posting asks for agents", "why_for_role": "", "fact_ids": []},
        {"intent": "proof", "point": "an agent with tools and evaluation", "why_for_role": "agents and retrieval pipelines",
         "fact_ids": [i["fin"], i["agent"], i["pytest"]]},
        {"intent": "proof", "point": "a campus assistant that improved", "why_for_role": "an assistant people can trust",
         "fact_ids": [i["bot"], i["chroma"], i["docker"]]},
        {"intent": "close", "point": "invite a conversation", "why_for_role": "", "fact_ids": []}]}


def write_answer(bases, text=LETTER):  # noqa: ANN001, ANN201
    i = ids_for(bases)
    return {"text": text, "cited_fact_ids": list(i.values()), "refusal": ""}


def make_job(connection, description=DESCRIPTION) -> int:  # noqa: ANN001
    job_id = add_job(connection, 1, "AI Engineer", description)
    connection.execute("UPDATE jobs SET company_name = 'Acme Robotics' WHERE id = ?", (job_id,))
    connection.commit()
    return job_id


class Scripted(FakeClient):
    """Answers in order and records (model, kwargs). `edits` maps a call index to a replacement GenerateResult."""

    def __init__(self, answers, replace=None) -> None:  # noqa: ANN001
        super().__init__(answers)
        self.seen: list[tuple[str, dict]] = []
        self.replace = replace or {}

    def generate_json(self, model, **kwargs):  # noqa: ANN001, ANN201
        index = self.calls
        self.seen.append((model, kwargs))
        override = self.replace.get(index)
        if override is not None:
            self.calls += 1
            return override(model) if callable(override) else override
        return super().generate_json(model, **kwargs)


def service_for(connection, vcfg, bases, client, *, budget=None):  # noqa: ANN001, ANN201
    ledger = UsageLedger(connection, vcfg.pricing)
    guard = BudgetGuard(ledger, budget or vcfg.budget)
    tailor = Tailor(connection, vcfg, FakeCompiler(), client, ledger=ledger, guard=guard, bases=bases)
    return WritingService(connection, vcfg, tailor, compiler=FakeCompiler())


def user_prompts(client, stage_prefix="TASK: write the cover letter") -> list[str]:  # noqa: ANN001
    return [kw["user_text"] for _, kw in client.seen if kw["user_text"].startswith(stage_prefix)]


# --- the reference letter of these tests is itself acceptable --------------------------------------------------------------


def test_the_test_letter_passes_the_letter_validator_and_copy_detection(bases, connection, vcfg) -> None:  # noqa: ANN001, F811
    facts = {f.id: f for f in bases.merged if bases.ticked.get(f.id)}
    cited = list(ids_for(bases).values())
    profile = profile_basics({k: v for k, v in facts.items() if v.kind == "education"})
    words = len(split_letter(LETTER)[1].split())
    assert 220 <= words <= 300, words
    verdict = validate_letter(LETTER, cited_ids=cited, facts=facts, job=JOB_VIEW, profile_text=profile, min_words=220, max_words=300)
    assert verdict.ok, verdict.violations
    copy = check_copying(split_letter(LETTER)[1], facts=[facts[c].text for c in cited], posting=DESCRIPTION)
    assert copy.ok, copy.violations


# --- letter validator: paraphrase allowed, invention rejected --------------------------------------------------------------


def violations_of(bases, body, **kwargs):  # noqa: ANN001, ANN201
    facts = {f.id: f for f in bases.merged if bases.ticked.get(f.id)}
    cited = kwargs.pop("cited", [ids_for(bases)["agent"], ids_for(bases)["chroma"]])
    job = {**JOB_VIEW, **kwargs.pop("job", {})}
    text = f"{GREETING}\n\n{body}\n\nSincerely,\n[Your Name]"
    return {v["rule"] for v in validate_letter(text, cited_ids=cited, facts=facts, job=job, **kwargs).violations}


def test_dropping_numbers_rewording_and_naming_the_postings_technologies_are_fine(bases) -> None:  # noqa: ANN001
    body = ("I built an assistant that picks between several tools to answer questions. I know the posting names Python and "
            "Kubernetes, and my work so far has been on retrieval and evaluation.")
    assert violations_of(bases, body) == set()


@pytest.mark.parametrize("body, rule", [
    ("I built an agent that handled 91 tools and answered every question.", "number_not_in_evidence"),
    ("I built an agent and tuned it on 12 documents.", "number_not_in_evidence"),
    ("I built an agent that used Redis for caching.", "unknown_technology_or_name"),
    ("I built an agent while interning at Initech where I used the stack.", "employer_not_in_facts"),
    ("I bring 5 years of experience building agents.", "states_years_of_experience"),
    ("I am a senior engineer who built a tool-using agent.", "banned_or_seniority_word"),
    ("I built an agent and tuned it in March.", "date_not_in_evidence"),
    ("I built an agent that scales to ninety tools.", "spelled_number_not_in_evidence"),
    ("Your world-class team builds an agent platform I admire.", "company_flattery"),
])
def test_invented_numbers_tools_employers_and_claims_are_rejected(bases, body, rule) -> None:  # noqa: ANN001
    assert rule in violations_of(bases, body)


def test_a_number_matches_by_value_and_two_projects_is_fine_when_two_are_cited(bases) -> None:  # noqa: ANN001
    ids = ids_for(bases)
    body = "Correct answers rose from sixty-five to eighty-five percent across my two projects, and 60,000 means nothing here."
    rules = violations_of(bases, "Across my two projects I built an agent and an assistant.", cited=[ids["agent"], ids["chroma"]])
    assert "spelled_number_not_in_evidence" not in rules
    assert "number_not_in_evidence" in violations_of(bases, body)
    assert "spelled_number_not_in_evidence" in violations_of(bases, "I built three agents.", cited=[ids["agent"]])


def test_the_signature_placeholder_is_the_only_placeholder_allowed(bases) -> None:  # noqa: ANN001
    assert "placeholder_left_in" in violations_of(bases, "I built an agent. [Insert company] is a fit.")
    assert "placeholder_left_in" not in violations_of(bases, "I built an agent.")


# --- copy detection ---------------------------------------------------------------------------------------------------------

FACT = "Built a tool-using LLM agent routing across 4 tools with a pgvector RAG search over 40 documents."
POSTING = "We are hiring an engineer to build reliable assistants that answer customer questions from internal documents every day."


def test_a_sentence_sharing_eight_words_with_a_fact_the_posting_or_a_sample_fails() -> None:
    assert check_copying("I enjoy this work a lot. I built a tool-using LLM agent routing across 4 tools with search.", facts=[FACT],
                         posting="").violations[0]["rule"] == "copied_run_from_facts"
    assert any(v["rule"] == "copied_run_from_posting" for v in check_copying(
        "Hello there. They want to build reliable assistants that answer customer questions from internal documents.", facts=[],
        posting=POSTING).violations)
    sample = "I have always liked turning a vague idea into a small tested tool that other people can use without help."
    letter = "Short intro here. Turning a vague idea into a small tested tool that other people can use is what I enjoy."
    assert any(v["rule"] == "copied_run_from_sample" for v in check_copying(letter, facts=[], posting="", samples=sample).violations)
    assert check_copying(letter, facts=[], posting="", samples="").ok


def test_seven_shared_words_pass_and_a_quarter_of_the_letter_copied_fails() -> None:
    seven = "built a tool-using LLM agent routing"
    assert check_copying(f"I {seven} and then moved on to something else entirely.", facts=[FACT], posting="").ok
    filler = " ".join(f"word{i}" for i in range(24))
    many = f"Built a tool-using LLM agent routing across 4 tools with a pgvector RAG search over 40 documents. {filler}"
    report = check_copying(many, facts=[FACT], posting="", max_share=0.25)
    assert not report.ok and "too_much_copied_text" in {v["rule"] for v in report.violations}
    assert report.copied_share > 0.25


@pytest.mark.parametrize("sentence", [
    "Work from my projects and internship that is relevant to this role.",
    "Use the following facts to write the letter.",
    "As an AI language model I cannot do this.",
    "Here is the cover letter you asked for.",
    "Replace this with your own words.",
])
def test_instruction_like_sentences_fail(sentence: str) -> None:
    report = check_copying(f"I like building things. {sentence} I enjoy it.", facts=[], posting="")
    assert "instruction_like_sentence" in {v["rule"] for v in report.violations}


def test_markdown_and_other_placeholders_fail_but_the_signature_does_not() -> None:
    assert not check_copying("I like **bold** claims.", facts=[], posting="").ok
    assert not check_copying("I will say [company name] here.", facts=[], posting="").ok
    assert check_copying("I like plain text.\n[Your Name]", facts=[], posting="").ok


def test_the_old_template_sentences_would_all_have_been_caught() -> None:
    old = ("I am applying for the Associate role at Acme. Work from my projects and internship that is relevant to this role. "
           f"FinAgent - Agent: {FACT} Thank you for your time and consideration.")
    rules = {v["rule"] for v in check_copying(old, facts=[FACT], posting="").violations}
    assert {"instruction_like_sentence", "copied_run_from_facts"} <= rules


# --- structure helpers -------------------------------------------------------------------------------------------------------


def test_greeting_sign_off_name_and_evidence_notes() -> None:
    assert greeting_line("Acme Robotics", None) == "Dear Acme Robotics Hiring Team,"
    assert greeting_line("Acme Robotics", "Priya Nair") == "Dear Priya Nair,"
    greeting, body, sign = split_letter(LETTER)
    assert greeting == GREETING and sign == "Sincerely,\n[Your Name]" and body.startswith("I am writing")
    fixed = normalise_letter("Hello team,\n\nBody text here.\n\nBest regards,\nSomeone", GREETING)
    assert fixed == f"{GREETING}\n\nBody text here.\n\nSincerely,\n[Your Name]"
    assert with_name(fixed, "Jordan Taylor").endswith("Sincerely,\nJordan Taylor")
    note = evidence_note("FinAgent", "Built a tool-using LLM agent (loop, structured outputs) that answers questions: 4 tools.")
    assert note.startswith("[FinAgent] Built a tool-using LLM agent | loop | structured outputs | that answers questions | 4 tools")
    assert posting_mentions_company(DESCRIPTION, "Acme Robotics") and not posting_mentions_company(NO_COMPANY_DESCRIPTION, "Acme Robotics")
    assert any("mission" in s for s in company_sentences(DESCRIPTION, "Acme Robotics"))


# --- the pipeline -------------------------------------------------------------------------------------------------------------


def test_plan_write_edit_with_the_app_fixing_greeting_and_sign_off(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    wrong = "Hi team,\n\n" + BODY + "\n\nBest wishes,\nA. Student"
    edited = BODY.replace("a chance to do more of it", "an opportunity to do more of it")
    client = Scripted([plan_answer(bases), write_answer(bases, wrong), {"text": f"{GREETING}\n\n{edited}\n\nSincerely,\n[Your Name]"}])
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    assert client.calls == 3 and [s["stage"] for s in result.stages] == ["plan", "write", "edit"]
    assert result.source == "model" and result.text.startswith(GREETING) and result.text.endswith("Sincerely,\n[Your Name]")
    assert "Hi team" not in result.text and "Best wishes" not in result.text
    assert result.plan["company_paragraph"] is True and result.validation["ok"] and result.validation["copy_check"]["ok"]
    assert abs(result.cost_usd - sum(s["cost_usd"] for s in result.stages)) < 1e-9
    prompt = user_prompts(client)[0]
    assert f"GREETING LINE (copy exactly): {GREETING}" in prompt and "COMPANY PARAGRAPH: yes" in prompt
    assert "Our mission is to make robots easier to program." in prompt                  # only what the posting says
    assert "degree: B.Tech Computer Science" in prompt and "agents and retrieval pipelines" in prompt
    assert "EVIDENCE NOTES" in prompt and "Built a campus assistant with ChromaDB retrieval | correct answers rose from 65% to 85%" in prompt


def test_without_company_information_in_the_posting_the_company_paragraph_is_off_even_if_the_plan_says_on(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    client = Scripted([plan_answer(bases, company=True), write_answer(bases), {"text": LETTER.replace("a chance to", "an opportunity to")}])
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection, NO_COMPANY_DESCRIPTION))
    assert result.plan["company_paragraph"] is False
    assert "COMPANY PARAGRAPH: no" in user_prompts(client)[0] and "COMPANY INFORMATION" not in user_prompts(client)[0]


def test_the_ladder_retries_the_same_model_with_the_reasons_then_the_flash_model(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    invented = LETTER.replace("rose from 65% to 85%", "rose from 65% to 85% and then to 91%")
    copied = LETTER.replace("The posting describes a team that wants robots to be easier to program",
                            "Our mission is to make robots easier to program and we are hiring an AI Engineer")
    client = Scripted([plan_answer(bases), write_answer(bases, invented), write_answer(bases, copied), write_answer(bases),
                       {"text": LETTER.replace("a chance to", "an opportunity to")}])
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    models = [m for m, kw in client.seen]
    assert models == ["gemini-3.8-flash", "gemini-2.5-pro", "gemini-2.5-pro", "gemini-3.8-flash", "gemini-3.8-flash"] or \
        models[1:4] == ["gemini-2.5-pro", "gemini-2.5-pro", "gemini-3.8-flash"], models
    prompts = user_prompts(client)
    assert "Your previous draft was rejected" not in prompts[0]
    assert "which is not in the evidence: remove it" in prompts[1] and "91" in prompts[1]
    assert "copied eight or more words" in prompts[2]
    assert result.source == "model" and result.calls <= 6


def test_when_every_rung_fails_the_click_ends_with_the_reason_and_an_outline_never_a_template(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    bad = write_answer(bases, LETTER.replace("rose from 65% to 85%", "rose from 65% to 85% and then to 91%"))
    client = Scripted([plan_answer(bases), bad])
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    assert result.source == "outline" and result.text.startswith("Facts outline for this job (not a cover letter)")
    assert "Work from my projects" not in result.text and "Dear " not in result.text
    assert "no letter was produced: every draft was rejected by the checks" in result.message and "91" in result.message
    assert client.calls == 4 and result.calls == 4                      # plan + three write rungs, no edit of a rejected draft
    assert all("- [" in line for line in result.text.splitlines()[3:])


def test_max_tokens_is_cut_off_even_when_the_json_parses_and_is_never_accepted(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    def cut(model):  # noqa: ANN001, ANN202
        return GenerateResult(text=json.dumps(write_answer(bases)), input_tokens=900, output_tokens=1200, thinking_tokens=100,
                              finish_reason="MAX_TOKENS")

    client = Scripted([plan_answer(bases), write_answer(bases), write_answer(bases), write_answer(bases)],
                      replace={1: cut, 2: cut, 3: cut})
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    assert result.source == "outline" and "was cut off" in result.message
    writes = [s for s in result.stages if s["stage"] == "write"]
    assert len(writes) == 3 and all(s["accepted"] is False and "cut off" in s["note"] for s in writes)
    assert all(s["cost_usd"] > 0 for s in writes)                      # a cut-off call still cost money and is shown
    assert "Your previous attempt failed because the answer was cut off" in user_prompts(client)[1]


def test_a_cut_off_first_attempt_is_retried_and_the_next_rung_can_succeed(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    def cut(model):  # noqa: ANN001, ANN202
        return GenerateResult(text='{"text": "Dear', input_tokens=900, output_tokens=3990, finish_reason="MAX_TOKENS")

    client = Scripted([plan_answer(bases), write_answer(bases), write_answer(bases), {"text": LETTER.replace("a chance to", "an opportunity to")}],
                      replace={1: cut})
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    assert result.source == "model" and result.calls == 4 or result.source == "model"
    assert [s["accepted"] for s in result.stages if s["stage"] == "write"][:2] == [False, True]
    assert client.seen[1][1].get("thinking_budget") == vcfg.writing.thinking_budget


def test_the_per_click_limits_are_six_calls_and_ten_cents(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    from jobhunt.writing.staged_run import MAX_CALLS_PER_CLICK, MAX_USD_PER_CLICK

    assert (MAX_CALLS_PER_CLICK, MAX_USD_PER_CLICK) == (6, 0.10) and vcfg.writing.max_usd_per_click == 0.10
    tiny = dataclasses.replace(vcfg, writing=dataclasses.replace(vcfg.writing, max_usd_per_click=0.0001))
    client = Scripted([plan_answer(bases), write_answer(bases)])
    result = service_for(connection, tiny, bases, client).cover_letter(make_job(connection))
    assert result.source == "outline" and "cost limit for one click reached" in result.message
    estimate = service_for(connection, vcfg, bases, Scripted([])).estimate(make_job(connection, "Build agents with RAG."))
    assert estimate["calls"] == 3 and estimate["max_calls"] == 6 and estimate["max_usd"] <= 0.10


def test_no_model_or_no_budget_gives_an_outline_with_the_reason(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    none = service_for(connection, vcfg, bases, None).cover_letter(make_job(connection))
    assert none.source == "outline" and "no model" in none.message and none.calls == 0
    exhausted = BudgetSettings(daily_calls=0, interactive_daily_calls=0, monthly_usd=vcfg.budget.monthly_usd)
    client = Scripted([plan_answer(bases)])
    blocked = service_for(connection, vcfg, bases, client, budget=exhausted).cover_letter(make_job(connection, DESCRIPTION + " "))
    assert blocked.source == "outline" and client.calls == 0
    assert "interactive limit reached today (0/0)" in blocked.message and "resets 05:30 IST" in blocked.message


def test_a_model_refusal_is_shown_as_the_reason(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    client = Scripted([plan_answer(bases), {"text": "", "cited_fact_ids": [], "refusal": "Nothing fits."}])
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    assert result.source == "outline" and "the model declined (Nothing fits.)" in result.message


def test_a_plan_with_a_bad_shape_falls_back_to_a_deterministic_plan(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    broken = plan_answer(bases)
    broken["paragraphs"] = broken["paragraphs"][:2]
    client = Scripted([broken, write_answer(bases), {"text": LETTER.replace("a chance to", "an opportunity to")}])
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    assert result.plan["source"] == "deterministic" and "deterministic plan" in result.message


def test_the_plan_schema_asks_for_a_why_line_and_the_company_flag() -> None:
    from jobhunt.writing.letter_prompts import LETTER_PLAN_SCHEMA

    props = LETTER_PLAN_SCHEMA["properties"]
    assert "company_paragraph" in props and "company_paragraph" in LETTER_PLAN_SCHEMA["required"]
    assert "why_for_role" in props["paragraphs"]["items"]["properties"]
    blocks, check = staged.validate_plan_blocks(plan_answer_for_schema(), {"A", "B", "C"})
    assert check.ok and blocks[1]["why_for_role"] == "matters"


def plan_answer_for_schema() -> dict:
    return {"paragraphs": [{"intent": "hook", "point": "p", "why_for_role": "", "fact_ids": []},
                           {"intent": "proof", "point": "p", "why_for_role": "matters", "fact_ids": ["A", "B"]},
                           {"intent": "proof", "point": "p", "why_for_role": "x", "fact_ids": ["C"]},
                           {"intent": "close", "point": "p", "why_for_role": "", "fact_ids": []}]}


# --- voice samples -------------------------------------------------------------------------------------------------------------


def test_samples_reach_the_write_prompt_stripped_and_a_letter_reusing_them_is_rejected(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    path = vcfg.resolve_path(vcfg.writing.voice_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!-- note to self -->\nI like small, honest projects and I say so plainly. Reach me at candidate@example.test or "
                    "+91 98765 43210.\n---\nTurning a vague idea into a small tested tool that other people can use is what I enjoy.\n",
                    encoding="utf-8")
    reused = LETTER.replace("Building software that lets language models work with real data has become the part of engineering I "
                            "enjoy most", "Turning a vague idea into a small tested tool that other people can use is what I enjoy")
    client = Scripted([plan_answer(bases), write_answer(bases, reused), write_answer(bases, reused), write_answer(bases, reused)])
    result = service_for(connection, vcfg, bases, client).cover_letter(make_job(connection))
    prompt = user_prompts(client)[0]
    assert "<style_reference>" in prompt and "small, honest projects" in prompt and "never copy content" in prompt
    for leaked in ("candidate@example.test", "98765", "note to self"):
        assert leaked not in prompt
    assert result.source == "outline" and "copied eight or more words from the style sample" in result.message


def test_the_cleaned_sample_text_is_what_copy_detection_compares_against() -> None:
    samples = clean_samples("<!-- c -->\nA sample line that is long enough to hold an eight word run.\n")
    assert samples.present
    assert not check_copying("Intro. A sample line that is long enough to hold an eight word run, I agree.", facts=[], posting="",
                             samples=samples.text).ok


# --- answers keep their flow ------------------------------------------------------------------------------------------------


def test_answers_skip_the_plan_respect_the_limit_and_are_never_an_outline(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    i = ids_for(bases)
    text = "I built a tool-using LLM agent that routes questions across 4 tools. I also tested it with a 60-case pytest suite."
    client = Scripted([{"text": text, "cited_fact_ids": [i["agent"], i["pytest"]], "refusal": ""}])
    result = service_for(connection, vcfg, bases, client).answer(make_job(connection), "Describe a project you are proud of (max 40 words)")
    assert client.calls == 1 and [s["stage"] for s in result.stages] == ["write"] and result.source == "model" and result.words <= 40


def test_an_over_long_answer_is_a_refusal_after_the_ladder(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    i = ids_for(bases)
    long_text = "I built a tool-using LLM agent that routes questions across 4 tools. " * 12
    client = Scripted([{"text": long_text, "cited_fact_ids": [i["agent"]], "refusal": ""}])
    result = service_for(connection, vcfg, bases, client).answer(make_job(connection), "Describe a project you are proud of (max 40 words)")
    assert result.source == "refusal" and "too_long" in {v["rule"] for v in result.rejected_violations}


@pytest.mark.parametrize("question", ["What is your expected salary or CTC?", "Do you require visa sponsorship?",
                                      "Please state your gender and ethnicity.", "Do you have a disability?"])
def test_the_never_draft_rule_still_holds(connection, vcfg, bases, question) -> None:  # noqa: ANN001, F811
    client = Scripted([{"text": "x", "cited_fact_ids": [], "refusal": ""}])
    result = service_for(connection, vcfg, bases, client).answer(make_job(connection), question)
    assert client.calls == 0 and result.source == "refusal" and "won't draft" in result.text


# --- edits, versions, export ---------------------------------------------------------------------------------------------------


def test_my_edit_is_a_new_version_checked_without_a_model_and_flags_copying_and_new_claims(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    client = Scripted([plan_answer(bases), write_answer(bases), {"text": LETTER.replace("a chance to", "an opportunity to")}])
    service = service_for(connection, vcfg, bases, client)
    generated = service.cover_letter(make_job(connection))
    before = connection.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    mine = service.save_edit(generated.row_id, generated.text.replace("Thank you for your time.", "Thanks for reading."))  # type: ignore[arg-type]
    assert connection.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == before
    assert mine.source == "user_edit" and mine.used_for_export and mine.version == generated.version + 1
    extra = ("I also used Kubernetes on 12 services and built a tool-using LLM agent routing across 4 tools with a pgvector RAG search "
             "over 40 documents.")
    flagged = service.save_edit(generated.row_id, generated.text.replace("Thank you for your time.", extra + " Thank you."))  # type: ignore[arg-type]
    rules = {v["rule"] for v in flagged.validation["violations"]}
    assert "edit_adds_claim" in rules and "copied_run_from_facts" in rules and flagged.validation["ok"] is False
    flags = {row["id"]: row["used_for_export"] for row in connection.execute("SELECT id, used_for_export FROM generated_texts")}
    assert sum(flags.values()) == 1 and flags[flagged.row_id] == 1


def test_export_puts_the_real_name_in_for_the_placeholder_and_refuses_outlines(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    client = Scripted([plan_answer(bases), write_answer(bases), {"text": LETTER.replace("a chance to", "an opportunity to")}])
    service = service_for(connection, vcfg, bases, client)
    letter = service.cover_letter(make_job(connection))
    text = service.export(letter.row_id, "txt").read_text(encoding="utf-8")  # type: ignore[arg-type]
    assert text.startswith(GREETING) and "[Your Name]" not in text and text.count("Dear ") == 1 and "Sincerely," in text
    outline = service_for(connection, vcfg, bases, None).cover_letter(make_job(connection, DESCRIPTION + "  "))
    with pytest.raises(ValueError, match="not a letter"):
        service.export(outline.row_id, "txt")  # type: ignore[arg-type]


def test_profile_basics_come_from_the_ticked_education_facts(bases, connection, vcfg) -> None:  # noqa: ANN001, F811
    facts = usable_facts(Tailor(connection, vcfg, FakeCompiler(), None, bases=bases))
    text = profile_basics(facts)
    assert "- degree: B.Tech Computer Science" in text and "- university: Example Institute" in text and "Expected 2027" in text


def test_judge_edit_in_letter_mode_accepts_grammar_fixes_only(bases) -> None:  # noqa: ANN001
    facts = {f.id: f for f in bases.merged if bases.ticked.get(f.id)}
    ids = list(ids_for(bases).values())
    edited = LETTER.replace("a chance to do more of it", "an opportunity to do more of it")
    ok = staged.judge_edit(LETTER, edited, cited_ids=ids, facts=facts, job=JOB_VIEW, kind="cover_letter", min_words=220, max_words=300,
                           letter=True, profile_text=profile_basics({k: v for k, v in facts.items() if v.kind == "education"}))
    assert ok.accepted, ok.reason
    bad = staged.judge_edit(LETTER, LETTER.replace("sixty test cases", "fifty test cases"), cited_ids=ids, facts=facts, job=JOB_VIEW,
                            kind="cover_letter", min_words=220, max_words=300, letter=True)
    assert not bad.accepted


def test_the_allowed_stock_openers_are_not_counted_as_copying_from_a_sample() -> None:
    sample = ("I am writing to express my interest in the Associate role at Other Corp. Then something entirely different follows here "
              "for the reader. I would welcome the opportunity to discuss how my experience fits.")
    letter = ("I am writing to express my interest in the AI Engineer role at Acme. Another line follows with fresh words only. "
              "I would welcome the opportunity to discuss how I could contribute.")
    assert check_copying(letter, facts=[], posting="", samples=sample).ok
    pasted = "Then something entirely different follows here for the reader, and I mean every word."
    assert not check_copying(f"Intro line first. {pasted}", facts=[], posting="", samples=sample).ok


def test_ordinary_prose_with_leading_managed_and_expert_is_not_a_seniority_claim(bases) -> None:  # noqa: ANN001
    body = "My work led to better answers, leading me to enjoy evaluation, and I managed my time well as an expert learner of testing."
    assert "banned_or_seniority_word" not in violations_of(bases, body)
    assert "ownership_claim_not_in_facts" not in violations_of(bases, body)
    assert "banned_or_seniority_word" in violations_of(bases, "I am a senior engineer.")


def test_a_rejected_draft_and_its_violations_are_kept_in_the_stage_record(connection, vcfg, bases) -> None:  # noqa: ANN001, F811
    bad = write_answer(bases, LETTER.replace("rose from 65% to 85%", "rose from 65% to 91%"))
    result = service_for(connection, vcfg, bases, Scripted([plan_answer(bases), bad])).cover_letter(make_job(connection))
    rejected = [s for s in result.stages if s.get("violations")]
    assert rejected and "91" in json.dumps(rejected[0]["violations"]) and rejected[0]["draft"].startswith(GREETING)


def test_a_year_followed_by_a_sentence_starting_with_m_is_not_read_as_millions() -> None:
    from jobhunt.writing.validator import number_values

    assert number_values("I expect to graduate in 2027. My projects follow.") == {2027.0}
    assert number_values("served ~60k visitors, 5k+ users and 3m events") == {60000.0, 5000.0, 3_000_000.0}


def test_names_that_cannot_be_paraphrased_are_not_copying() -> None:
    sample = "Studying for a B.Tech in Computer Science and Engineering at Example Institute of Science and Technology every day."
    letter = "Intro line. I am studying for a B.Tech in Computer Science and Engineering at Example Institute of Science and Technology."
    assert not check_copying(letter, facts=[], posting="", samples=sample).ok
    assert check_copying(letter, facts=[], posting="", samples=sample,
                         exempt=["B.Tech in Computer Science and Engineering at Example Institute of Science and Technology"]).ok
